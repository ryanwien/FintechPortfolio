from __future__ import annotations

import pytest

from exchange.backtest import FeeSchedule, Ledger, run_backtest, summarize
from exchange.bench import run_bench, synthetic_flow
from exchange.strategies import MarketMaker, Momentum


@pytest.mark.parametrize("make", [MarketMaker, Momentum])
def test_no_lookahead_prefix_invariance(make, market_cfg):
    """Extending the horizon (i.e. adding future data) must not change any decision
    taken earlier: orders and equity up to step N are identical for runs of N and 2N."""
    short = run_backtest(make(), 800, market_cfg)
    long = run_backtest(make(), 1600, market_cfg)
    assert short.orders == [o for o in long.orders if o[0] < 800]
    assert short.net_equity == long.net_equity[:800]
    assert short.orders, "strategy should trade in the test window"


@pytest.mark.parametrize("make", [MarketMaker, Momentum])
def test_backtest_deterministic(make, market_cfg):
    a = summarize(run_backtest(make(), 600, market_cfg))
    b = summarize(run_backtest(make(), 600, market_cfg))
    assert a == b


def test_pnl_decomposition_is_exact_integer_math():
    lg = Ledger(FeeSchedule(taker_fee=30, maker_rebate=20))
    lg.on_fill(buy=True, price=10_001, qty=100, maker=False, mid_x2=20_001)  # cross spread
    lg.on_fill(buy=False, price=10_003, qty=100, maker=True, mid_x2=20_004)  # sell at ask
    mid = 20_004
    assert lg.position == 0
    assert all(isinstance(v, int) for v in (lg.cash, lg.cash_mid, lg.fee_pnl))
    # 100 sh: bought 100.01, sold 100.03 => +$2.00 = 20_000 units, fee -3000 + 2000
    assert lg.cash == 20_000
    assert lg.fee_pnl == -1_000
    assert lg.net(mid) == lg.frictionless(mid) + (lg.cash - lg.cash_mid) + lg.fee_pnl


def test_market_maker_is_pure_maker_and_earns_rebates(market_cfg):
    s = summarize(run_backtest(MarketMaker(), 1500, market_cfg))
    assert s["maker_share"] == 1.0
    assert s["pnl_decomposition_usd"]["fees_and_rebates"] > 0
    assert 0 < s["fill_ratio"] < 1
    assert abs(s["inventory"]["max_abs"]) <= 500 + 100  # max_pos + one lot of overshoot


def test_momentum_is_pure_taker_and_pays_costs(market_cfg):
    m = summarize(run_backtest(Momentum(), 1500, market_cfg))
    assert m["maker_share"] == 0.0
    assert m["pnl_decomposition_usd"]["fees_and_rebates"] <= 0
    assert m["pnl_decomposition_usd"]["spread_and_slippage"] <= 0


def test_bench_flow_is_deterministic():
    assert synthetic_flow(2_000, seed=1) == synthetic_flow(2_000, seed=1)


def test_small_bench_runs_and_replay_matches():
    out = run_bench(5_000, seed=1, repeats=1)
    assert out["replay"]["hash_match"]
    lat = out["latency_ns"]
    assert lat["p50"] <= lat["p99"] <= lat["p99_9"] <= lat["max"]
    assert out["throughput_orders_per_sec"] > 0
