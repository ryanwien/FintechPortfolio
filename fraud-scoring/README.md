# fraud-scoring: real-time card fraud scoring

This project scores card transactions in real time. It computes streaming point-in-time features, scores with gradient-boosted trees and maps each score to approve, review or decline bands chosen by expected dollar loss. A FastAPI service returns the decision with reason codes and the model version.

## Why this matters in fintech

Card fraud models usually fail for operational reasons rather than for lack of model accuracy:

- **Train/serve skew.** Features built in a SQL backfill don't match the ones computed in the authorization path.
- **Leakage.** A training feature can quietly use information from after the transaction.
- **Label delay.** Chargebacks arrive weeks later, so recent fraud looks "good" at training time.
- **Arbitrary thresholds.** A 0.5 cut-off ignores the cost difference between a missed $2,000 fraud and an annoyed good customer.
- **Silent drift.** Fraudsters change tactics.

Each of these issues is handled explicitly in this repo, and each fix is measured.

## Architecture

```
                     simulate.py (seeded)
      customers, merchants, MCCs, devices, trips, 5 fraud patterns
                 |                                  |
          transactions                    chargeback events (label_ts)
                 \________________  _______________/
                                  \/
                    iter_events(): one time-ordered stream
                                  |
                 +----------------v-----------------+
                 |  FeatureEngine.process(txn)       |  one code path, used by
                 |  read state -> features -> fold   |  training AND serving
                 |  per-customer / merchant / device |
                 +-------+--------------------+------+
          batch replay   |                    |  online, one request at a time
                         v                    v
            X (training matrix)          Scorer (lock + engine state)
                         |                    |
     time split: warm-up | train | valid | test | shifted
                         |                    |
      HGB  vs  logistic  vs  rules            |   CompiledForest (flat-array trees)
                         |                    |   perturbation reason codes
       PR-AUC, ROC-AUC, recall@alert-rate,    |
       Brier + reliability                    v
                         |              FastAPI  POST /score  POST /label  GET /health
       optimize_bands(): min expected $ loss  ->  approve / review / decline
                         |
       PSI drift: population (real time) + fraud-only (after labels mature)
                         |
       results/metrics.json, REPORT.md, site.json
```

| Module | Role |
|---|---|
| `config.py` | Every tunable in one place: seed and simulation sizes, cost model, HGB parameters, split lengths, alert rates, PSI thresholds, benchmark sizes, paths. |
| `catalog.py` | Static reference data: metros, MCCs and their ticket sizes, the legit spending mix, fraud-type names. |
| `world.py` | Customers, merchants and devices, plus the row accumulator and samplers shared by the generators. |
| `legit.py` | Legitimate behaviour, including deliberate fraud look-alikes (trips, new devices, big tickets, micro-bursts). |
| `fraud_patterns.py` | The five fraud injectors and chargeback delays. |
| `simulate.py` | Wires the above into a deterministic transaction stream. Amounts are integer cents. Every fraud has a `label_ts`. |
| `features.py` | Streaming point-in-time feature engine (19 features). `build_features` replays it over history. |
| `model.py` | `HistGradientBoostingClassifier`, the logistic-regression baseline, model versioning and reason codes. |
| `forest.py` | Copies the fitted trees into NumPy arrays for sub-millisecond single-request inference. Matches sklearn to 1e-12. Reads private sklearn attributes, so it is pinned to scikit-learn 1.9.1 (a test enforces the pin). |
| `rules.py` | Hand-written rules baseline (velocity, card testing, impossible travel, shared device, ...). |
| `metrics.py` | PR-AUC, ROC-AUC, recall at a fixed alert rate, Brier and reliability bins. |
| `costs.py` | Integer-cent cost model plus an exhaustive search for the review and decline thresholds, with a review-capacity limit. |
| `drift.py` | PSI with warn/alert thresholds. |
| `evaluation.py` | Time split, per-model reports, recall by fraud type, band optimisation and savings, drift report. |
| `service.py` / `api.py` | Stateful scorer and FastAPI app. |
| `replay.py` | Replays the stream through the serving `Scorer` and the HTTP API: train/serve skew and latency. |
| `report.py` / `site_feed.py` | Render `REPORT.md` and `site.json` from the metrics dict. |
| `run.py` | End-to-end pipeline that writes `results/`. |

## Run it

```bash
cd fraud-scoring
pip install -r requirements.txt -e .   # from the repo's virtualenv
ruff check . && pytest
python -m fraud.run                # ~40 s; regenerates everything in results/
python -m fraud.api               # serve the scorer trained by fraud.run
```

```bash
curl -s -X POST localhost:8000/score -H 'content-type: application/json' -d '{
  "txn_id": 1, "ts": 23400000, "customer_id": 42, "merchant_id": 7, "mcc": 5732,
  "amount_cents": 189900, "lat": 51.51, "lon": -0.13, "device_id": 1234567,
  "card_present": false}'
```

The response contains `score`, `decision`, up to three `reasons` (code, feature, direction-aware description, logit contribution) and `model_version`. `?explain=always|adverse|never` controls reason codes; the default `adverse` attaches them only to review and decline decisions. `?debug=true` echoes the computed features.

## Results

All numbers below are copied from [`results/REPORT.md`](results/REPORT.md) and [`results/metrics.json`](results/metrics.json) (seed 7). Latency depends on the machine; every other number is deterministic.

**Data.** 331,302 transactions from 1,800 customers. 5,075 are fraud (1.53%), split by type:

- 1,813 card testing
- 1,361 account takeover
- 822 high-value out-of-pattern
- 657 merchant compromise
- 422 friendly fraud

The data is split by time into 30-day months: warm-up 0-1, train 2-4, validation 5, test 6-7 and a shifted month 8. Label delay has a median of 10.9 days and a p90 of 33.1 days. At the training cut-off, 15.7% of training-window fraud had not been reported yet.

**Model comparison (test months 6-7, matured labels):**

| Model | PR-AUC | ROC-AUC | Recall @ 0.5% alerts | Precision @ 0.5% | Recall @ 2% alerts | Brier |
|---|---|---|---|---|---|---|
| HistGradientBoosting | **0.884** | 0.991 | 33.6% | 100.0% | **87.1%** | 0.00372 |
| Logistic regression | 0.720 | 0.975 | 31.4% | 93.7% | 75.1% | 0.00716 |
| Rules | 0.244 | 0.816 | 14.5% | 43.3% | 36.4% | n/a |

Fraud is 1.49% of test traffic, so a 0.5% alert budget can catch at most 33.6% of it. HGB hits that ceiling with 100% precision, and the 2% column shows the headroom beyond it. The base-rate Brier score is 0.01469. The reliability table in REPORT.md shows HGB under-predicting in the middle bins, because it was trained on labels as known at the cut-off.

**Cost-sensitive decisions.** The cost model:

- Missed fraud costs the full amount.
- A review costs $3 of analyst time, stops 90% of the fraud it sees, and adds $1 of friction for a good customer.
- A false decline costs 3% lost margin plus $10 of customer friction.
- Review capacity is at most 1% of traffic.

Bands are fitted on month 5 and frozen for months 6-7:

| Policy | Review band | Decline at | Reviews | Declines | False declines | Fraud $ stopped | Expected loss |
|---|---|---|---|---|---|---|---|
| HGB | [0.0179, 0.0585) | >= 0.0585 | 756 (0.95%) | 1,388 | 373 | 95.6% | **$55,448** |
| Logistic | [0.0623, 0.106) | >= 0.106 | 709 (0.89%) | 1,696 | 790 | 93.3% | $82,070 |
| Rules | (none) | >= 1 point | 0 | 3,313 | 2,531 | 90.0% | $155,768 |
| Approve all | - | - | 0 | 0 | 0 | 0% | $815,306 |

**HGB cuts expected loss by $100,320 compared with the rules baseline over the two test months, or $50,160 a month (64.4% lower).** It also beats logistic regression by $26,622.

**Drift (PSI against the training months; warn at 0.10, alert at 0.25).** Month 8 combines a seasonal shift in legitimate spend (bigger baskets, more online) with a change in account-takeover tactics: residential proxies, never-seen devices and normal-sized baskets.

- **Population PSI catches only the seasonal shift.** It is available in real time and flags `amount_log` at 0.140 and `amount_z` at 0.139 as warn, with model-score PSI at 0.118 (warn). It cannot see the tactic change, because fraud is about 1.5% of traffic.
- **Fraud-only PSI catches the tactic change.** It is available once chargebacks mature and flags `amount_log` at 0.351, `amount_vs_ema` at 0.335 and `amount_z` at 0.314 as ALERT.
- **The frozen model degrades on the shifted month.** HGB PR-AUC falls from 0.884 to 0.763, and ATO recall at 0.5% alerts falls from 69.4% to 33.0%.

**Serving.**

- **No train/serve skew.** All 331,302 transactions replayed through the serving `Scorer` reproduce the batch features exactly (max abs difference 0). Online scores from the flat-array forest match the sklearn batch scores to within 3.7e-16. Sending 1,000 transactions as JSON through FastAPI also gives a feature difference of 0.
- **Feature engine throughput:** about 56k transactions per second for the batch replay on one core.

| Path | p50 | p95 | p99 |
|---|---|---|---|
| In-process `Scorer.score` (features + model + reason codes on review/decline) | 0.48 ms | 0.97 ms | 2.18 ms |
| Same, reason codes on every request | 2.00 ms | 2.82 ms | 3.29 ms |
| FastAPI `POST /score` via in-process TestClient (ASGI + JSON + validation) | 6.85 ms | 8.57 ms | 9.78 ms |

## Design decisions

- **One feature engine, two drivers.** `FeatureEngine.process` reads state, emits features, then folds the transaction into state. Training replays history through it; serving calls it once per request. Skew is therefore ruled out by construction, and tests check it at three levels: in-process, scores, and HTTP/JSON.
- **Point-in-time correctness is tested rather than assumed.**
  - One test truncates or scrambles the future (amounts, devices, geography, fraud flags and chargeback arrivals) and asserts the past features are bit-identical.
  - Another recomputes features for 300 rows with a brute-force pandas implementation that only looks backwards.
  - Further tests check that the current transaction is excluded from its own windows and that a chargeback is invisible until its `label_ts`.
- **Label delay is modelled, not ignored.** Training uses only labels that had arrived by the cut-off, and evaluation uses matured labels, which is how a backtest is run in practice. The merchant chargeback-rate feature consumes label events as they arrive.
- **Stationary features.** Early versions used whole-history state (max amount, all-time centroid, ever-seen merchant). Those features drift just because history accumulates, and population PSI raised false alerts on the test months. They were replaced with EMA state and a 60-day merchant lookback, and two warm-up months give every feature history. Test-month PSI is now below 0.002 on every feature.
- **Money is integer cents end to end.** Costs use integer basis points and floor division. The band optimiser does an exact search over (review, decline) threshold pairs with prefix sums, and a test checks it against brute force.
- **The rules get the same optimiser as the models**, so the dollar comparison is fair.
- **Fast inference without a new dependency.** sklearn's `predict_proba` costs about 5 ms per call at batch size 1, mostly overhead. `forest.py` copies the trees into flat arrays and walks all trees in lock-step. It is identical to sklearn to 1e-12, including categorical splits, unseen categories and NaN. Tree depth is capped at 8 to bound the walk.
- **Reason codes on adverse decisions only (by default).** They come from single-feature perturbation toward a "typical good transaction" in one batched tree walk. Descriptions are direction-aware, because a perturbation can show that a *low* value raised the score.
- **Honest data.** The simulation includes legitimate look-alikes (one-off devices, big-ticket buys, travel abroad, in-app micro-purchase bursts) and 422 friendly-fraud disputes that no behavioural model can catch. Recall on friendly fraud is 0%, and it is reported as such.

## Limitations

- **Synthetic data.** The fraud patterns were designed, so the separability (ROC-AUC 0.991) flatters the model compared with a real portfolio, where PR-AUC is typically far lower.
- **Single-process state.** Feature state lives in one process's memory. Production needs a shared low-latency feature store (Redis or Aerospike), idempotent and ordered event ingestion, and a state snapshot or restore story; `pickle` is used here only for convenience.
- **Approximate reason codes.** Perturbation codes are not exact SHAP values, and feature interactions are attributed to whichever single feature moves the score most.
- **Uncalibrated scores.** HGB is not recalibrated, which doesn't matter for score-threshold bands but would for amount-aware decisioning.
- **Latency numbers are local.** They come from one machine and exclude network and serialisation outside the process.

## What I'd do next

- Amount-aware decisioning: decline when `p * amount` exceeds the expected false-decline cost, which needs calibrated probabilities (isotonic regression fitted on a matured validation window).
- Retraining triggered by the fraud-only drift monitor, with champion/challenger evaluation on matured labels and a delay-aware label maturity curve.
- Graph features (device, card and merchant links) for ring detection, plus sequence models for ATO sessions.
- Move feature state behind a store interface, add load tests with a real ASGI server, and export Prometheus metrics for latency, decision mix and per-feature PSI.
