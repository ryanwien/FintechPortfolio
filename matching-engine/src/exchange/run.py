"""Run the benchmark and the backtests; write results/metrics.json, REPORT.md, site.json.

    python -m exchange.run [--orders N] [--steps N]      (from the matching-engine/ folder)

Pipeline: engine benchmark -> primary backtest -> the same backtest on every robustness
seed -> downsampled equity curves -> metrics dict -> rendered files (see ``report``).
Everything except timing values is deterministic.
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
import time
from pathlib import Path

from .backtest import UNITS_PER_USD, BacktestResult, downsample, run_all
from .bench import run_bench
from .config import BACKTEST_STEPS, BENCH_ORDERS, ROBUSTNESS_SEEDS
from .report import Metrics, report_md, site_json

ROOT = Path(__file__).resolve().parents[2]
RESULTS = ROOT / "results"
STRATEGIES = ("market_maker", "momentum")
ROBUSTNESS_FIELDS = ("net_pnl_usd", "costs_ignored_pnl_usd", "sharpe_net", "sharpe_costs_ignored")


def _seed_robustness(primary: Metrics, steps: int) -> Metrics:
    """Re-run the backtest on every robustness seed (reusing the primary run) and summarise."""
    rows_by_strategy: dict[str, list[dict[str, float]]] = {}
    for seed in ROBUSTNESS_SEEDS:
        out = primary if seed == ROBUSTNESS_SEEDS[0] else run_all(steps, seed)[0]
        for name in STRATEGIES:
            row = {"seed": seed} | {k: out[name][k] for k in ROBUSTNESS_FIELDS}
            rows_by_strategy.setdefault(name, []).append(row)
    return {
        name: {
            "seeds": len(rows),
            "net_positive_seeds": sum(r["net_pnl_usd"] > 0 for r in rows),
            "costs_ignored_positive_seeds": sum(r["costs_ignored_pnl_usd"] > 0 for r in rows),
            "mean_sharpe_net": round(statistics.fmean(r["sharpe_net"] for r in rows), 2),
            "mean_sharpe_costs_ignored": round(
                statistics.fmean(r["sharpe_costs_ignored"] for r in rows), 2
            ),
            "rows": rows,
        }
        for name, rows in rows_by_strategy.items()
    }


def _equity_series(raw: dict[str, BacktestResult]) -> Metrics:
    """Downsampled net and costs-ignored equity curves in USD, per strategy."""
    series = {}
    for name, r in raw.items():
        x, net = downsample(r.net_equity)
        _, fr = downsample(r.frictionless_equity)
        series[name] = {
            "step": x,
            "net_equity_usd": [round(v / UNITS_PER_USD, 2) for v in net],
            "costs_ignored_equity_usd": [round(v / UNITS_PER_USD, 2) for v in fr],
        }
    return series


def build(n_orders: int, steps: int) -> Metrics:
    """Run everything and return the full metrics dict (what metrics.json contains)."""
    t0 = time.perf_counter()
    bench = run_bench(n_orders)
    t_bench = time.perf_counter() - t0

    primary, raw = run_all(steps, ROBUSTNESS_SEEDS[0])
    robustness = _seed_robustness(primary, steps)

    return {
        "project": "matching-engine",
        "environment": {
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "platform": sys.platform,
        },
        "benchmark": bench,
        "backtest": primary,
        "seed_robustness": robustness,
        "series": _equity_series(raw),
        "runtime_seconds": {
            "benchmark": round(t_bench, 1),
            "total": round(time.perf_counter() - t0, 1),
        },
    }


def write_results(m: Metrics, out_dir: Path = RESULTS) -> str:
    """Write metrics.json, REPORT.md and site.json; returns the rendered report."""
    report = report_md(m)
    out_dir.mkdir(exist_ok=True)
    (out_dir / "metrics.json").write_text(json.dumps(m, indent=2) + "\n", encoding="utf-8")
    (out_dir / "REPORT.md").write_text(report, encoding="utf-8")
    (out_dir / "site.json").write_text(
        json.dumps(site_json(m), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description="benchmark + backtest, writes results/")
    ap.add_argument("--orders", type=int, default=BENCH_ORDERS)
    ap.add_argument("--steps", type=int, default=BACKTEST_STEPS)
    args = ap.parse_args()
    print(write_results(build(args.orders, args.steps)))


if __name__ == "__main__":
    main()
