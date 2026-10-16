# Conventions

These apply to all five projects. CI enforces everything marked *(CI)*.

## Layout

```
<project>/
  README.md          pitch, why it matters, architecture, run, results, decisions, limitations
  pyproject.toml     build, pinned deps, pytest config (no per-project lint config)
  requirements.txt   runtime + dev pins, same versions as pyproject
  .run               module to execute end to end, e.g. `ledger.bench`
  src/<package>/     the code, with a py.typed marker
  tests/             pytest; shared fixtures in conftest.py
  results/           committed outputs of the last real run
```

## Code

- Python 3.12+ *(CI: 3.12, 3.13, 3.14)*, following the scientific-Python support window
  (NumPy 2.5 and networkx 3.7 require 3.12).
- One lint and format standard: the root `ruff.toml`, line length 100 *(CI: `ruff check .`,
  `ruff format --check .`)*. `pre-commit install` runs the same checks locally.
- Type hints on every public function; frozen, slotted dataclasses for value objects; `Final`
  for module constants. No magic numbers in logic.
- Every module opens with a docstring saying what it owns and which invariants it keeps.
- Specific exception types ending in `Error`; no bare `except`, no silent swallowing.
- Entry points orchestrate; rendering (Markdown, JSON for the site) lives in its own module.

## Money and data

- Money is never a float: integer minor units (cents) or `decimal.Decimal`.
- Deterministic: fixed seeds, synthetic data generated in code, no network, no API keys.
- No lookahead: features, splits and backtests are strictly point-in-time.

## Results

- `python -m $(cat .run)` regenerates `results/metrics.json`, `results/REPORT.md` and
  `results/site.json` *(CI runs it on 3.14)*. Nothing in `results/` is written by hand.
- Re-running produces byte-identical output except wall-clock timings.
- README numbers are copied from `results/`. Every README ends with "Limitations" and
  "What I'd do next".
- Tests must pass with `filterwarnings = error` *(CI: `pytest`)*.

## Site feed

`results/site.json` feeds the dashboard (`python tools/build_site.py` bundles all five into
`assets/data.js`):

```json
{
  "slug": "ledger", "title": "...", "tagline": "one sentence",
  "lane": "Payments infrastructure", "stack": ["Python", "FastAPI"],
  "metrics": [{"label": "p99 posting latency", "value": "1.8", "unit": "ms", "note": "..."}],
  "highlights": ["3-5 short bullets of what it proves"],
  "limitations": ["2-3 honest bullets"],
  "series": {"name": "small numeric series for the chart", "x": [], "y": []}
}
```
