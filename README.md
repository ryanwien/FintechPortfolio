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
lint in one go, see [Contributing](#-contributing).

## 📖 Usage

Each project installs with `pip install -e <project>` and is driven with `python -m`,
always from inside the project folder. Entry points:

| Command | What it does |
|---|---|
| `python -m ledger.bench` | Benchmarks the ledger and regenerates its `results/` |
| `python -m ledger.api` | Serves the payments API on `127.0.0.1:8000` (docs at `/docs`) |
| `python -m fraud.run` | Simulates, trains, evaluates and replays the fraud scorer |
| `python -m fraud.api` | Serves `POST /score` using the model `fraud.run` trained |
| `python -m credit.run` | Builds, validates and documents the credit scorecard |
| `python -m exchange.run` | Engine benchmark plus all backtests |
| `python -m exchange.bench` | Benchmark only: `--orders` (default 300000), `--seed` |
| `python -m aml.run` | Full monitoring pipeline: `--seed`, `--customers`, `--config`, `--out` |

Dashboard data and the recruiter PDF rebuild from whatever the runs produced:

```bash
python tools/build_site.py        # bundles every results/ into assets/data.js
python tools/build_onepager.py    # renders RyanWien-Fintech-Portfolio.pdf
```

## Examples

Take a payment through the ledger API, then retry it with the same idempotency key
(the retry replays the stored response instead of charging twice):

```bash
python -m ledger.api &            # database at $LEDGER_DB, default ledger.db
curl -s -X POST localhost:8000/v1/payments   -H 'Content-Type: application/json' -H 'Idempotency-Key: order-1042'   -d '{"source_account_id":"...","destination_account_id":"...","amount":1250,"currency":"USD","capture":false}'
```

Score a transaction and ask for the computed features back:

```bash
python -m fraud.api &
curl -s -X POST 'localhost:8000/score?explain=always&debug=true'   -H 'content-type: application/json'   -d '{"txn_id":1,"ts":23400000,"customer_id":42,"merchant_id":7,"mcc":5732,
       "amount_cents":189900,"lat":51.51,"lon":-0.13,"device_id":1234567,"card_present":false}'
```

`explain` takes `always`, `adverse` (default: reason codes only on review and decline) or
`never`; `debug=true` echoes every feature value the model saw.

Retune the AML monitor without touching code: thresholds live in versioned config, and
every alert carries the config fingerprint it was raised under:

```bash
echo '{"structuring": {"band_low_cents": 700000}, "scoring": {"case_line": 50}}' > overrides.json
python -m aml.run --config overrides.json --customers 8000 --seed 7
```

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

## 🤝 Contributing

Want to pull it down and play with it? The whole repo works offline.

### Clone and set up

```bash
git clone https://github.com/ryanwien/FintechPortfolio.git
cd FintechPortfolio
python -m venv .venv && .venv/Scripts/activate    # or: source .venv/bin/activate
pip install -r requirements.txt
pip install -e ledger -e fraud-scoring -e credit-risk -e matching-engine -e aml-monitoring
pre-commit install                                # optional: lint and format on commit
```

### Run the checks CI runs

```bash
ruff check . && ruff format --check .             # one standard, repo-wide
```

Then in any project: `pytest` (306 tests across the five), and `python -m $(cat .run)` to
regenerate its `results/` end to end. Or everything at once:

```bash
for p in ledger fraud-scoring credit-risk matching-engine aml-monitoring; do
  (cd $p && ruff check . && pytest -q && python -m $(cat .run))
done
python tools/build_site.py                        # refresh the dashboard data
```

### Submit a pull request

Fork, branch, and open a PR. Two rules above all: never hand-edit anything in `results/`
or `assets/data.js` (those files are written by the code), and a behavior change needs a
test. The full ground rules are in [CONVENTIONS.md](./CONVENTIONS.md); security reports
go through [SECURITY.md](./SECURITY.md), not public issues.

## Stack

Python · FastAPI · scikit-learn · pandas · NumPy · SQLite · networkx · Hypothesis · pytest · ruff · GitHub Actions

Front end: hand-written HTML/CSS/JS, no framework, no build step.
