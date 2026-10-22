# Fintech Portfolio

Five production-style fintech systems, each built end to end, tested against the ways it
can break, and measured. Every number in every README is produced by the code next to it:
run the project and the report regenerates.

**Live dashboard:** [ryanwien.github.io/FintechPortfolio](https://ryanwien.github.io/FintechPortfolio/)

## Motivation

Fintech software has an unusual property: being almost right is the same as being wrong.
A ledger that loses a cent per million transfers, a backtest that peeks one tick into the
future, or a credit model that quietly fails a fairness test is not 99.99% done. It is
broken, and in this industry the breakage shows up as money, regulators, or both.

A resume can claim to understand that, and a demo can hide not understanding it. Working
systems with tests, measurements, and stated limitations cannot. That is what this
repository is for.

### Goal

One system from each of the five lanes fintech companies actually hire for, each built the
way the production version would be, scaled down in size but not in rigor. In particular:

- Correctness you can check: 306 tests, property-based where it counts, run in CI on every push
- Money handled like money: integer cents or `Decimal`, never floats, conserved under concurrency
- No self-deception: strictly point-in-time features and backtests, out-of-time validation
- Numbers you can trust: every metric is written by the code it describes, never by hand
- Honest edges: each project documents what it cannot do and what I would build next
- Try before you read: every system has an interactive demo on the [live dashboard](https://ryanwien.github.io/FintechPortfolio/)

| # | System | Lane | What it proves |
|---|---|---|---|
| 01 | [`ledger/`](./ledger) | Payments infrastructure | Double-entry journal, idempotent payments API, authorization holds, reconciliation against a bank feed, money conserved under concurrent load |
| 02 | [`fraud-scoring/`](./fraud-scoring) | Risk & payments ML | Point-in-time streaming features (no train/serve skew), cost-sensitive thresholds priced in dollars, PSI drift monitoring, sub-millisecond scoring API |
| 03 | [`credit-risk/`](./credit-risk) | Lending & model risk | WoE scorecard vs. monotonic GBM challenger, out-of-time validation, fair-lending tests, adverse-action reason codes, generated model card |
| 04 | [`matching-engine/`](./matching-engine) | Trading & market infrastructure | Price-time priority limit order book, deterministic replay, property-tested invariants, event-driven backtester with fees and slippage |
| 05 | [`aml-monitoring/`](./aml-monitoring) | Financial crime & compliance | Typology rules engine, graph analytics for mule rings and round-tripping, case management with a hash-chained audit trail and SAR narratives |

## 🚀 Quick Start

Python 3.12+ is the only requirement: no API keys, no services, no network. Everything
runs locally on deterministic synthetic data.

### 1. Clone and install

```bash
git clone https://github.com/ryanwien/FintechPortfolio.git
cd FintechPortfolio
python -m venv .venv && .venv/Scripts/activate    # or: source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Run a system end to end

The credit model is the fastest, about 5 seconds:

```bash
pip install -e credit-risk
cd credit-risk
python -m credit.run      # trains, validates, writes results/ including the model card
pytest                    # 63 tests
```

### 3. Read the results you just produced

Open `credit-risk/results/REPORT.md`. Every project works the same way: `pip install -e
<project>`, then `python -m` the module named in its `.run` file regenerates its `results/`.

Prefer zero setup? The [live dashboard](https://ryanwien.github.io/FintechPortfolio/) has
every system's results and an interactive demo of each. To run all five systems, tests and
lint in one go, see [CONTRIBUTING.md](./CONTRIBUTING.md).

## Principles

- **Money is never a float.** Integer minor units or `Decimal`, everywhere.
- **No lookahead.** Features, backtests, and validation splits are strictly point-in-time.
- **Measured, not demoed.** Each project writes `results/metrics.json` and `results/REPORT.md`
  from a real run. Nothing is typed in by hand.
- **Honest limitations.** Every README ends with what the system cannot do and what I would
  build next.
- **Reproducible.** Deterministic seeds, synthetic data generated in code, no API keys, no
  network. One shared virtualenv, one command per project.

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
