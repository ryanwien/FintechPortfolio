# credit-risk

A consumer-loan underwriting scorecard with the evidence a bank model-risk team or a lending
fintech's compliance team would ask for: out-of-time validation, fair-lending tests,
Reg B adverse action reasons, a profit-based cutoff and a generated model card.

## Why this matters in fintech

A credit model is only half the work. Before it can approve a single loan, a lender has to
show that it ranks risk and stays stable out of time (SR 11-7 / OCC 2011-12). It also has to
show that it does not produce a disparate impact on a protected class (ECOA, the four-fifths
rule as a screening test). Every declined applicant must be told the specific principal
reasons (Reg B, 12 CFR 1002.9), and the cutoff has to be set on economics, not on AUC.
This project covers each of those steps end to end and writes the documentation from the
same run, so the numbers in the docs cannot drift from the code.

## Architecture

```
 generate_applicants (seed)                          protected_group
 bureau-style features + latent default  ----------------------------------+
            |                                                              |
   split by application month                                              |
   dev 0-17 (train 70 / in-time holdout 30)    OOT 18-23                   |
            |                                     |                        |
   +--------+---------+                           |                        |
   |                  |                           |                        |
 WoE binning      HistGradientBoosting            |                        |
 (monotonic,      (monotonic constraints)         |                        |
  IV screen)          |  challenger               |                        |
   |                  |                           |                        |
 logistic reg         |                           |                        |
 sign screen          |                           |                        |
   |                  |                           |                        |
 points scaling       |                           |                        |
 600 @ 30:1, PDO 20   |                           |                        |
   |  champion        |                           |                        |
   +--------+---------+                           |                        |
            v                                     v                        v
   validation: AUC / Gini / KS, calibration by decile, PSI + CSI     fairness: approval
   strategy: cutoff table, profit cutoff (Decimal -> cents)          rates, AIR, EO gap,
   adverse action: top-4 points-lost reasons                         proxy-removal test
            |
            v
   results/metrics.json  REPORT.md  MODEL_CARD.md  site.json  scorecard_points.csv
```

Package layout (`src/credit/`): `config.py` seeds, score scaling, unit economics and policy
thresholds; `data.py` generator and OOT split; `woe.py` binning and IV; `scorecard.py` scaling
and points; `challenger.py` GBM; `metrics.py` AUC/KS/calibration/PSI; `fairness.py` AIR and
equal opportunity; `reasons.py` Reg B reason codes; `strategy.py` unit economics in cents;
`validation.py` candidate evaluation; `serialize.py` metrics rounding; `markdown.py` shared
tables; `report.py`, `model_card.py` and `site.py` rendering; `run.py` orchestration.

## Run it

```bash
cd credit-risk
pip install -r requirements.txt -e .   # from the repo's virtualenv
ruff check . && pytest
python -m credit.run               # ~5 s; regenerates everything in results/
```

## Results

All figures are from `results/metrics.json` (seed 20261013, 60,000 applicants; 44,852
development, 15,148 out-of-time; bad rate 11.81% dev and 14.51% OOT).

| Model | Sample | AUC | Gini | KS |
|---|---|---|---|---|
| WoE scorecard (champion) | in-time holdout | 0.7074 | 0.4149 | 0.3056 |
| WoE scorecard (champion) | out-of-time | 0.6997 | 0.3994 | 0.2958 |
| Monotonic GBM (challenger) | in-time holdout | 0.7020 | 0.4041 | 0.3025 |
| Monotonic GBM (challenger) | out-of-time | 0.6969 | 0.3937 | 0.2910 |

- **Champion/challenger:** the GBM loses 0.0028 OOT AUC (adoption needs +0.020), so the
  scorecard stays champion.
- **Stability:** score PSI dev vs OOT 0.0171 (stable). The largest characteristic shift is
  `inquiries_6m`, CSI 0.0643.
- **Calibration:** OOT mean predicted PD 12.83% vs observed 14.51% (obs/pred 1.131). Rank
  ordering holds in every decile, but the level is under-predicted in the stressed window.
  That is amber on the monitoring plan.
- **Variable selection:** `loan_amount` (IV 0.0372) was dropped because its coefficient came
  out wrong-signed. All final coefficients are negative.

Fair lending, OOT, at the profit cutoff (AIR = approval rate B / approval rate A):

| Model | OOT AUC | Approve A | Approve B | AIR | Four-fifths | EO gap |
|---|---|---|---|---|---|---|
| With `region` (geographic proxy) | 0.7106 | 69.7% | 52.5% | 0.753 | FAIL | 0.1582 |
| `region` removed (champion) | 0.6997 | 65.6% | 60.0% | 0.914 | pass | 0.0433 |
| GBM challenger, matched approval | 0.6969 | 65.9% | 59.3% | 0.900 | pass | 0.0530 |

Removing the proxy costs 0.0109 OOT AUC, which is inside the 0.015 tolerance, so the
mitigation was adopted.

Cutoff strategy (assumptions: lifetime margin 9% of principal, LGD 70%, EAD 75%, so
break-even PD is 14.63%, a score of about 552.7). Score 555 maximises expected profit on the
in-time holdout. On OOT applicants it approves 63.9% at a 9.05% bad rate, with a realised loss
rate of 4.63% of booked principal and a realised profit of $5,612,880.00. Approving everyone
would have earned $1,899,750.00 at a 14.51% bad rate.

Adverse action: 5,473 OOT declines, and every one received four reasons. The most common
primary reasons were revolving utilisation (2,083), income (1,644) and credit history length
(1,317).

## Design decisions

- **Scorecard as champion by policy, not by default.** A challenger has to beat it by 0.02
  OOT AUC *and* pass the four-fifths screen. Additive points give exact, auditable adverse
  action reasons. A GBM would need post-hoc attribution such as SHAP to do the same, and
  that has to earn its keep.
- **Monotonic WoE by construction.** Fine classes are merged until every bin holds at least
  5% of the population and WoE moves in the direction of the feature's rank correlation with
  default. That keeps points intuitive ("more utilisation never earns points") and reason
  codes defensible.
- **Wrong-sign screen.** A positive coefficient on a WoE feature reverses its univariate
  ordering, usually through collinearity (here `loan_amount` against `annual_income`). It is
  removed and the model refit, rather than shipping counter-intuitive points.
- **Protected attribute is quarantined.** Both model constructors raise
  `ProtectedAttributeError` if it is offered as an input. A test flips the attribute and
  checks that predictions do not change. It is joined back only after decisions are made.
- **Proxy mitigation is measured, not assumed.** Region is the realistic proxy (redlining
  risk), so the run fits with and without it and records the AUC cost against a stated
  tolerance.
- **Reason codes = points lost vs max attainable**, top four, with ties broken
  deterministically. Attributes where nothing was lost are never cited.
- **Cutoff chosen without peeking at OOT.** Expected profit uses model PD on the in-time
  holdout. OOT outcomes are only used to report what that choice actually earned.
- **Money in cents.** Per-loan economics are computed in `Decimal`, rounded half-even to
  integer cents, then summed as integers.

## Limitations

- Synthetic data with a logit-additive default process favours a logistic scorecard. Real
  bureau data has interactions, missing values and thin files, where a GBM may win.
- No reject inference: the development sample is treated as through-the-door. PD
  under-predicts by about 13% in the OOT window and would need recalibration before it is
  used for loss forecasting.
- Fairness covers one binary protected attribute with known labels. Real testing covers
  each ECOA basis, often through BISG proxies, plus a search for less discriminatory
  alternatives.
- Unit economics are flat (no term structure, prepayment, funding curve or pricing by risk).

## What I'd do next

- Add reject inference (parcelling or fuzzy augmentation) and an intercept recalibration step
  triggered by the observed/predicted monitor.
- Fit risk-based pricing so the cutoff and the APR are optimised together.
- Run a less-discriminatory-alternative search: sweep feature subsets and binning choices on
  the AUC-vs-AIR frontier instead of testing a single proxy removal.
- Add SHAP-based reason codes to the challenger and test their agreement with the
  scorecard's points-lost reasons.
