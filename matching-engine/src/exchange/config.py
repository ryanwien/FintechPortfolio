"""Run-level configuration: seeds, sizes and calendar constants in one place.

Market microstructure parameters live in ``sim.MarketConfig``, fees in
``backtest.FeeSchedule`` and strategy parameters in the strategy constructors; this module
holds the knobs that decide *what* gets run and how results are annualised.
"""

from __future__ import annotations

from typing import Final

# Engine benchmark.
BENCH_ORDERS: Final = 300_000
BENCH_SEED: Final = 42
BENCH_REPEATS: Final = 3  # throughput is reported as the best of this many runs

# Backtest.
BACKTEST_STEPS: Final = 30_000
BACKTEST_SEED: Final = 7  # the primary run reported in detail
ROBUSTNESS_SEEDS: Final = (BACKTEST_SEED, 11, 23, 42, 99)
STEPS_PER_DAY: Final = 500
TRADING_DAYS_PER_YEAR: Final = 252
# Strategy orders decided at step t reach the engine at t+1, behind that step's background
# flow. This is structural in ``backtest.run_backtest``; the constant documents it.
STRATEGY_LATENCY_STEPS: Final = 1

# Reporting.
SERIES_POINTS: Final = 200  # equity curves are downsampled to this many points
