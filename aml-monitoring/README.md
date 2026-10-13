# AML transaction monitoring

A transaction-monitoring system for a synthetic bank. A versioned rules engine and
networkx graph analytics raise alerts. Alerts become scored cases in a priority queue,
and analysts work the queue through a hash-chained audit trail that also holds SAR
narrative drafts. Everything is scored against ground truth.

## Why this matters in fintech

US banks and fintechs have to monitor transactions under the BSA. Teams usually struggle
with two numbers: false positives, which are commonly 90%+ of alerts and drive analyst
headcount, and missed typologies, which bring regulatory findings. The work is mostly
plumbing that reviewers can defend:

- thresholds that are versioned and can be tuned
- above/below-the-line evidence for every threshold change
- alerts merged into cases so one subject isn't reviewed five times
- an audit trail an examiner can verify
- narratives built from the evidence

This project builds that whole chain end to end and measures it.

## Architecture

```
 synth/ (seeded)                           config.py  MonitoringConfig v2026.10-r4 + sha256 fingerprint
 population.py  customers, KYC, txns─┐         │
 typologies.py  injected (labels)    │         ▼
 segments.py + lookalikes.py benign  │   ┌──────────── detect.py ────────────────────────────────┐
                                     │   │ activity.py per-customer index                         │
                                     └──▶│ rules.py   STR-01 structuring   VEL-01 velocity vs KYC │
                                         │            RMF-01 rapid movement GEO-01 geography       │
                                         │ graph.py   GRA-CYC temporal cycles (nx.simple_cycles)  │
                                         │            GRA-FIN/FOUT fan anomalies (MAD baselines)  │
                                         │            GRA-COM Louvain communities (mule rings)    │
                                         └──────────────┬────────────────────────────────────────┘
                                                        ▼  alerts.py dedupe (rule, customer, overlapping window)
                          scoring.py  1 - Π(1 - w_rule * signal) + corroboration bonus, x KYC multiplier
                                                        ▼
                cases.py  case per customer or per network ──▶ the line (score >= 40) ──▶ CaseQueue (heap)
                                  │ below line: SUPPRESSED            │ L1 analyst ─▶ L2 ─▶ QA (simulated)
                                  ▼                                   ▼
                       audit.py  append-only SHA-256 hash chain ◀── narrative.py SAR draft (templates)
                                                        ▼
                evaluate.py  per-typology detection, precision, FP rate, alerts/1k, line + rule sweeps
                                                        ▼
                run.py ─▶ report.py  results/metrics.json  REPORT.md  sample_case.md  site.json
```

## Run it

```bash
cd aml-monitoring
pip install -r requirements.txt -e .   # from the repo's virtualenv
ruff check . && pytest
python -m aml.run                  # ~20 s; regenerates everything in results/
python -m aml.run --help           # seed, population size, config overrides
```

You can override any threshold with JSON, for example
`{"structuring": {"band_low_cents": 700000}, "scoring": {"case_line": 50}}`. The config
fingerprint changes with it and is stamped on every alert and audit entry. A full run
takes about 15 s on a laptop.

## Results

These come from `results/` (seed 20261013, config `2026.10-r4+c082bd1d7176`). The world
has 4,080 customers and 235,439 transactions: a 31-day lookback followed by a 61-day
monitoring window. Ground truth is 80 suspicious customers in 41 typology instances.

| Metric | Value |
|---|---:|
| Typology instances with at least one alert | 100.0% |
| Typology instances with a case above the line (score >= 40) | **87.8%** |
| Alerts | 416 (**102.0 per 1k customers**) |
| Alert precision | **54.6%** |
| False-positive rate (benign customers alerted) | **3.7%** |
| Cases above / below the line | 75 / 114 (18.4 per 1k above) |
| Case precision above the line | 49.3% |
| Simulated SARs filed | 32 of 75 reviewed (31 on true cases, 1 on a benign case) |
| Audit chain | 1,096 entries, verified |

| Typology | Instances | Detected at the line | Misses at the line |
|---|---:|---:|---|
| structuring | 12 | 75.0% | all 3 "low-and-slow" ($6.0k-8.4k over 30 days) |
| rapid movement | 12 | 91.7% | 1 of 3 "slow" (out after 56-90h) |
| high-risk layering | 8 | 87.5% | the one "medium-tier only" variant |
| mule network | 4 | 100.0% | none; Louvain grouped every ring into one network case |
| round-tripping | 5 | 100.0% | none; 30 cycle alerts, 0 false |

Where the false positives come from: all 105 benign customers with a large one-off life
event alerted, mainly on velocity and rapid movement, and 37 of them reached a case above
the line. So did 1 of 36 contractors that received a one-off project payment and paid
subcontractors. Cash-intensive businesses (2 of 181 alerted), escrow agents, landlords,
charities, payroll-heavy staffing firms, importers, remitters and intercompany groups
produced no case above the line.

**Above/below the line.** These figures are from `REPORT.md`.

| Case-score line | Cases per 1k | Typology detection | Case precision |
|---:|---:|---:|---:|
| 20 | 35.3 | 92.7% | 27.1% |
| 30 | 20.8 | 87.8% | 43.5% |
| **40 (current)** | **18.4** | **87.8%** | **49.3%** |
| 50 | 12.7 | 87.8% | 71.2% |
| 60 | 7.8 | 70.7% | 93.8% |

On this world the line could move from 40 to 50 and cut workload by 31% without losing a
typology. Above 50, detection falls off quickly.

The rule sweeps show the same trade-off one rule at a time. For example, lowering the
structuring band from $8k to $6k nearly quadruples STR-01 alerts (12 to 45). It catches
only one more structurer, and precision falls from 75% to 22%. Widening the
rapid-movement window from 48h to 72h lifts target detection from 75% to 100% for 45%
more alerts.

`results/sample_case.md` is a complete case file for an 8-account mule ring. It has:

- the score breakdown and all 20 alerts
- the drafted SAR narrative with an evidence table
- the audit trail with the hash of each entry
- a live tamper check

## Design decisions

- **Integer cents and integer minutes everywhere.** Money is never a float; times are
  minutes since a fixed epoch.
- **Labels are never read by detectors.** `Txn.label` exists only for evaluation. A test
  reruns detection on `world.without_labels()` and checks that the alerts are identical.
- **Benign look-alikes are part of the design.** Each rule has a population built to
  fool it: laundromats for structuring, escrow agents for rapid movement, importers for
  geography, staffing payroll for velocity, intercompany rent loops for cycles, and
  charities and landlords for fan-in. Tests assert that each rule fires on its typology
  and not on its look-alike.
- **Thresholds are relative to the profile.** Velocity and rapid movement compare
  activity with the KYC-declared expected monthly activity, not just absolute amounts.
  That is why an escrow agent moving $400k in a day stays quiet and a student moving $8k
  does not.
- **Cycles must be temporal.** `nx.simple_cycles` finds static loops. Each loop then has
  to be confirmed as a time-ordered chain inside 21 days that keeps 80-105% of the value
  at every hop. The intercompany groups have a static cycle and are never flagged.
- **Fan-in/out uses three conditions.** Degree must exceed a robust baseline (median + 6
  x MAD from weekly `nx.DiGraph` snapshots, per customer kind). Most counterparties must
  be new. Funds must pass through. Degree alone flags every landlord and charity.
- **Network cases.** Louvain runs only around alerted customers. A community with at
  least 3 members and at least 2 independently alerted becomes a single case, so a mule
  ring is reviewed once instead of once per member (up to nine times).
- **The scoring is transparent.** `1 - Π(1 - w·s)` per rule, plus a bonus for
  corroboration across typology families, times the KYC multiplier. The breakdown is
  stored on each case.
- **The audit log is tamper-evident.** Each entry hashes its content and the previous
  hash. Editing, deleting, reordering or backdating an entry fails verification, and
  re-hashing a forged entry breaks the next link.
- **Determinism includes hash seeds.** Louvain runs on integer-relabelled nodes and
  cycles are canonically rotated, so results are byte-identical across
  `PYTHONHASHSEED` values. A test checks this.

## Limitations

- The world is synthetic, and the same author wrote the typologies and the rules. The
  100% any-alert detection is optimistic for that reason, and so is the 55% alert
  precision: production programs usually see single-digit precision. Treat the numbers
  as a demonstration of the mechanics, not a benchmark.
- Analyst dispositions are simulated from ground truth with fixed noise rates, so the
  SAR figures measure the workflow, not human judgement.
- One account per customer, no entity resolution, no sanctions or name screening, no
  adverse media, and no peer-group segmentation beyond individual vs business.
- Narratives are template drafts. They are deliberately not LLM-generated and still need
  analyst editing before filing.
- With 41 instances, each typology's detection rate moves in steps of 8-25 points.

## What I'd do next

- Test thresholds out of sample: tune on one seed, report on others, and add confidence
  intervals by bootstrapping instances.
- Add peer-group segmentation (by occupation and industry) and dynamic baselines in
  place of the declared expected activity.
- Add a learned prioritisation model (gradient boosting on alert and graph features)
  trained on dispositions, compared with the transparent scorecard.
- Add entity resolution across accounts and shared identifiers (device, address), plus
  sanctions screening.
- Persist cases and the audit log to an append-only store such as Postgres with
  row-level hashing, behind a small FastAPI case-management API.
