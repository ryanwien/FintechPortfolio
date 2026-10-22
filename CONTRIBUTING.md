# Contributing

This repo is a personal portfolio, so I keep final say on direction, but bug reports,
corrections and small improvements are genuinely welcome, and issues pointing out anything
dishonest-looking in the numbers get priority.

## Setup

```bash
git clone https://github.com/ryanwien/FintechPortfolio.git
cd FintechPortfolio
python -m venv .venv && .venv/Scripts/activate     # or: source .venv/bin/activate
pip install -r requirements.txt
pip install -e ledger -e fraud-scoring -e credit-risk -e matching-engine -e aml-monitoring
pre-commit install                                  # optional: run lint/format on commit
```

Python 3.12+ (CI tests 3.12, 3.13 and 3.14). No API keys, no network, no services.

## Before you open a PR

Everything CI enforces, runnable locally from the repo root:

```bash
ruff check . && ruff format --check .   # one standard for the whole repo (ruff.toml)
```

Then, in each project you touched:

```bash
cd <project>
pytest                                  # warnings are errors
python -m "$(cat .run)"                 # regenerates results/ end to end
```

Ground rules (the long form is in [CONVENTIONS.md](./CONVENTIONS.md)):

- **Never hand-edit anything in `results/` or `assets/data.js`.** Those files are written
  by the code. If a number should change, change the code and re-run.
- Money is never a float. No lookahead. Deterministic seeds.
- A behavior change needs a test; a bug fix needs a test that fails without it.
- If your change alters a project's results, run `python tools/build_site.py` and
  `python tools/build_onepager.py` so the dashboard and PDF stay in sync.

## Front end

Plain HTML/CSS/JS in `assets/js/`, no framework, no build step. Keep each module to its
one job (see the file-top docstrings), and check your change at phone width (375px) and in
both themes before pushing.

## Commit messages

Plain imperative subject, body explaining why. No attribution trailers or tool tags of
any kind.

## Security issues

Don't open a public issue; see [SECURITY.md](./SECURITY.md).
