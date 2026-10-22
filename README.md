# Fintech Portfolio

Five production-style fintech systems, each built end to end, tested against the ways it
can break, and measured. Every number in every README is produced by the code next to it:
run the project and the report regenerates.

**Live dashboard:** [ryanwien.github.io/FintechPortfolio](https://ryanwien.github.io/FintechPortfolio/)

## Why this exists

Fintech software has an unusual property: being almost right is the same as being wrong.
A ledger that loses a cent per million transfers, a backtest that peeks one tick into the
future, or a credit model that quietly fails a fairness test is not 99.99% done — it is
broken, and in this industry the breakage shows up as money, regulators, or both.

I built this portfolio because a resume can claim to understand that and a demo can hide
not understanding it. Working systems with tests, measurements, and stated limitations
can't. So instead of one toy app, this is one system from each of the five lanes fintech
companies actually hire for — moving money, catching fraud, approving loans, running
markets, and compliance — each built the way the production version would be, scaled down
in size but not in rigor.

It's also how I'd want to be evaluated: not "did the demo work," but "did the overdraft
race get handled, and is there a test proving it."

| # | System | Lane | What it proves |
|---|---|---|---|
| 01 | [`ledger/`](./ledger) | Payments infrastructure | Double-entry journal, idempotent payments API, authorization holds, reconciliation against a bank feed, money conserved under concurrent load |
| 02 | [`fraud-scoring/`](./fraud-scoring) | Risk & payments ML | Point-in-time streaming features (no train/serve skew), cost-sensitive thresholds priced in dollars, PSI drift monitoring, sub-millisecond scoring API |
| 03 | [`credit-risk/`](./credit-risk) | Lending & model risk | WoE scorecard vs. monotonic GBM challenger, out-of-time validation, fair-lending tests, adverse-action reason codes, generated model card |
| 04 | [`matching-engine/`](./matching-engine) | Trading & market infrastructure | Price-time priority limit order book, deterministic replay, property-tested invariants, event-driven backtester with fees and slippage |
| 05 | [`aml-monitoring/`](./aml-monitoring) | Financial crime & compliance | Typology rules engine, graph analytics for mule rings and round-tripping, case management with a hash-chained audit trail and SAR narratives |

## Principles

- **Money is never a float.** Integer minor units or `Decimal`, everywhere.
- **No lookahead.** Features, backtests, and validation splits are strictly point-in-time.
- **Measured, not demoed.** Each project writes `results/metrics.json` and `results/REPORT.md`
  from a real run. Nothing is typed in by hand.
- **Honest limitations.** Every README ends with what the system cannot do and what I would
  build next.
- **Reproducible.** Deterministic seeds, synthetic data generated in code, no API keys, no
  network. One shared virtualenv, one command per project.

## Run everything

```bash
python -m venv .venv && .venv/Scripts/activate      # or source .venv/bin/activate
pip install -r requirements.txt
for p in ledger fraud-scoring credit-risk matching-engine aml-monitoring; do
  pip install -e $p
  (cd $p && ruff check . && pytest -q && python -m $(cat .run))
done
python tools/build_site.py                           # refresh the dashboard data
```

CI lints and format-checks the whole repo with one shared `ruff.toml`, tests all five projects
on Python 3.12, 3.13 and 3.14, and runs each project end to end on every push. Repo-wide rules
are in [CONVENTIONS.md](./CONVENTIONS.md).

## Repository layout

```
.
├── ledger/              01 · double-entry ledger & payments API
├── fraud-scoring/       02 · real-time card fraud scoring
├── credit-risk/         03 · credit underwriting & model risk
├── matching-engine/     04 · limit order book + backtester
├── aml-monitoring/      05 · AML monitoring & case management
├── index.html           dashboard shell (views are <template>s, filled by JS)
├── assets/js/           front end, no build step: util, catalog, charts, sfx, app, demos
├── assets/data.js       generated: every project's results bundled for the dashboard
├── tools/build_site.py  bundles every project's results/ into assets/data.js
├── ruff.toml            one lint + format standard for all projects
└── .github/workflows/   CI: lint → test matrix → end-to-end runs
```

## Stack

Python · FastAPI · scikit-learn · pandas · NumPy · SQLite · networkx · Hypothesis · pytest · ruff · GitHub Actions

Front end: hand-written HTML/CSS/JS, no framework, no build step.
