"""Event-driven backtester: strategies trade through the real matching engine against the
synthetic background market, so fills depend on actual queue position and book depth.

Money: cash is tracked in integer units of $0.0001 ("bp-cents"). 1 tick = $0.01 = 100 units.
PnL is decomposed exactly (integers) into:

    net = mid-marked PnL  +  spread capture / slippage  +  fees & rebates

* mid-marked PnL: as if every fill had executed at the prevailing mid with no fees.
  This is the "costs ignored" view a naive backtest reports.
* spread/slippage: (mid - fill price) * signed qty. Positive for a maker capturing the
  half-spread, negative for a taker crossing it and walking the book.
* fees/rebates: taker fee per share paid, maker rebate per share earned.

Mids are carried as ``mid_x2`` (bid + ask) so they stay integers.

    python -m exchange.backtest      prints the primary-seed summary as JSON
"""

from __future__ import annotations

import json
import math
import statistics
import sys
from dataclasses import dataclass, field
from typing import Any, Final

from .config import (
    BACKTEST_SEED,
    BACKTEST_STEPS,
    SERIES_POINTS,
    STEPS_PER_DAY,
    STRATEGY_LATENCY_STEPS,
    TRADING_DAYS_PER_YEAR,
)
from .engine import Engine
from .sim import BackgroundMarket, MarketConfig
from .strategies import Gateway, MarketMaker, Momentum, Strategy, View
from .types import Ack, Booked, Command, Event, Fill, Replaced, Side

UNITS_PER_TICK: Final = 100  # $0.01 tick / $0.0001 unit
UNITS_PER_HALF_TICK: Final = UNITS_PER_TICK // 2  # value of one unit of mid_x2
UNITS_PER_USD: Final = 10_000
VIEW_DEPTH: Final = 5  # L2 levels per side shown to strategies


@dataclass(frozen=True, slots=True)
class FeeSchedule:
    """Per-share fees in $0.0001 units."""

    taker_fee: int = 30  # $0.0030 per share
    maker_rebate: int = 20  # $0.0020 per share


@dataclass(slots=True)
class Ledger:
    """One strategy's books, kept twice: at execution prices and at the pre-trade mid."""

    fees: FeeSchedule
    position: int = 0
    cash: int = 0  # actual execution prices
    cash_mid: int = 0  # same fills marked at the pre-trade mid
    fee_pnl: int = 0  # +rebates - taker fees
    filled_qty: int = 0
    maker_qty: int = 0
    n_fills: int = 0

    def on_fill(self, buy: bool, price: int, qty: int, maker: bool, mid_x2: int) -> None:
        """Book one fill. ``buy`` sets the sign of the position change and ``maker`` selects
        the rebate over the taker fee. ``mid_x2`` is the pre-trade mid (bid + ask), used only
        for the frictionless ledger.
        """
        # Cash moves opposite to position: buying spends cash, selling collects it.
        sgn = 1 if buy else -1
        self.position += sgn * qty
        self.cash -= sgn * qty * price * UNITS_PER_TICK
        # The same fill valued at the pre-trade mid. The gap to cash is exactly the spread/slippage
        # component of the decomposition.
        self.cash_mid -= sgn * qty * mid_x2 * UNITS_PER_HALF_TICK
        # Makers earn a rebate and takers pay a fee. Both go into fee_pnl, keeping the decomposition
        # additive.
        self.fee_pnl += qty * self.fees.maker_rebate if maker else -qty * self.fees.taker_fee
        self.filled_qty += qty
        self.maker_qty += qty if maker else 0
        self.n_fills += 1

    def mark(self, mid_x2: int) -> int:
        """Value of the current position at the mid."""
        return self.position * mid_x2 * UNITS_PER_HALF_TICK

    def net(self, mid_x2: int) -> int:
        """Equity after spread, slippage, fees and rebates."""
        return self.cash + self.fee_pnl + self.mark(mid_x2)

    def frictionless(self, mid_x2: int) -> int:
        """Equity had every fill executed at the pre-trade mid with no fees."""
        return self.cash_mid + self.mark(mid_x2)


@dataclass(slots=True)
class BacktestResult:
    """Per-step series and counters from one ``run_backtest``; money in $0.0001 units."""

    strategy: str
    steps: int
    ledger: Ledger
    net_equity: list[int] = field(default_factory=list)
    frictionless_equity: list[int] = field(default_factory=list)
    positions: list[int] = field(default_factory=list)
    orders: list[tuple[int, Command]] = field(default_factory=list)  # (decision step, cmd)
    offered_qty: int = 0
    queue_ahead_samples: list[int] = field(default_factory=list)
    regime_steps: dict[str, int] = field(default_factory=dict)
    engine_commands: int = 0
    final_mid_x2: int = 0


def run_backtest(
    strategy: Strategy,
    steps: int = BACKTEST_STEPS,
    cfg: MarketConfig | None = None,
    fees: FeeSchedule | None = None,
) -> BacktestResult:
    """Run ``strategy`` against the background market for ``steps`` steps.

    Each step: background flow, then the strategy orders decided last step, then mark to
    mid and ask the strategy for next step's orders.
    """
    cfg = cfg or MarketConfig()
    eng = Engine(journal=False)
    ledger = Ledger(fees or FeeSchedule())
    res = BacktestResult(strategy=strategy.name, steps=steps, ledger=ledger)
    me = strategy.owner
    # Mids are doubled (bid + ask) so this starting value has the same units as mid_now().
    mid_x2 = 2 * cfg.start_price
    owner_of: dict[int, int] = {}  # order id -> owner, for every acked order

    def mid_now() -> int:
        """Twice the current mid in ticks (best bid + best ask, doubled so it stays an integer)."""
        b, a = eng.best_bid(), eng.best_ask()
        return b + a if b is not None and a is not None else mid_x2

    def execute(cmd: Command) -> list[Event]:
        """Send ``cmd`` to the engine and book anything that concerns the strategy."""
        # Fills are marked against the mid just before this command: the frictionless benchmark.
        pre_mid = mid_now()
        evs = eng.process(cmd)
        for e in evs:
            t = type(e)
            if t is Fill:
                # A fill has one taker and one maker. Each side is booked only if it is ours: the
                # taker pays the fee, the maker earns the rebate.
                if e.taker_owner == me:
                    ledger.on_fill(e.aggressor is Side.BUY, e.price, e.qty, False, pre_mid)
                if e.maker_owner == me:
                    ledger.on_fill(e.aggressor is Side.SELL, e.price, e.qty, True, pre_mid)
            elif t is Ack and e.owner == me:
                res.offered_qty += e.qty
            # A requeued replace puts our new size back in the queue, so it is offered again. A
            # priority-kept size decrease offers nothing new.
            elif t is Replaced and not e.priority_kept and owner_of.get(e.order_id) == me:
                res.offered_qty += e.new_qty
            elif t is Booked and owner_of.get(e.order_id) == me:
                # Sample queue position at the moment we join: the shares ahead of us must trade
                # before we can fill.
                res.queue_ahead_samples.append(eng.queue_ahead(e.order_id))
            # Remember which strategy owns each order id, so later events can be attributed to us.
            if t is Ack:
                owner_of[e.order_id] = e.owner
        return evs

    market = BackgroundMarket(eng, cfg, execute)
    # Seed the book before the first decision so the strategy has a top of book at t=0.
    market.seed_book()
    gw = Gateway(eng)
    pending: list[Command] = []
    mids: list[int] = []
    regimes: dict[str, int] = {}
    fund = market.fund

    # Per step: background flow, then our orders decided last step (one step of latency), then mark
    # to mid and decide the next orders.
    for t in range(steps):
        market.step(t)
        regimes[fund.regime] = regimes.get(fund.regime, 0) + 1
        for cmd in pending:  # decided at t-1, arrive after this step's background flow
            execute(cmd)
        mid_x2 = mid_now()
        mids.append(mid_x2)
        res.net_equity.append(ledger.net(mid_x2))
        res.frictionless_equity.append(ledger.frictionless(mid_x2))
        res.positions.append(ledger.position)
        # The strategy sees only L2 up to this step, including its own resting orders.
        bids, asks = eng.l2(depth=VIEW_DEPTH)
        view = View(t, bids, asks, mids, ledger.position)
        pending = strategy.decide(view, gw)
        res.orders.extend((t, c) for c in pending)

    res.regime_steps = regimes
    # Background ids come from market.next_id and every strategy command is counted. Background
    # cancels are not, so this is an approximate command count.
    res.engine_commands = market.next_id - 1 + len(res.orders)
    res.final_mid_x2 = mid_x2
    return res


# ----------------------------------------------------------------------------- metrics


def _usd(units: int) -> float:
    """Convert $0.0001 units to dollars, rounded to the cent."""
    return round(units / UNITS_PER_USD, 2)


def _max_drawdown(eq: list[int]) -> int:
    """Largest peak-to-trough decline in an equity series, in the series' own units."""
    peak, mdd = eq[0], 0
    for x in eq:
        peak = max(peak, x)
        mdd = max(mdd, peak - x)
    return mdd


def _sharpe(eq: list[int], steps_per_day: int) -> float:
    """Annualised Sharpe of daily equity changes (zero risk-free rate)."""
    # Sample equity once per simulated day, at the last step of each day.
    daily = [eq[i] for i in range(steps_per_day - 1, len(eq), steps_per_day)]
    # Daily PnL is the change between samples. The first day is measured from zero equity.
    pnl = [b - a for a, b in zip([0, *daily[:-1]], daily, strict=True)]
    if len(pnl) < 2:
        return 0.0
    sd = statistics.pstdev(pnl)
    if sd == 0:
        return 0.0
    return round(statistics.fmean(pnl) / sd * math.sqrt(TRADING_DAYS_PER_YEAR), 2)


def summarize(res: BacktestResult, steps_per_day: int = STEPS_PER_DAY) -> dict[str, Any]:
    """Headline metrics in USD, with the exact PnL decomposition checked."""
    lg = res.ledger
    m = res.final_mid_x2
    net = lg.net(m)
    fr = lg.frictionless(m)
    # Integer arithmetic makes net = frictionless + spread + fees an exact identity.
    spread = lg.cash - lg.cash_mid
    # A failure here is a ledger bug, not rounding.
    assert net == fr + spread + lg.fee_pnl  # exact integer decomposition
    pos = res.positions
    return {
        "strategy": res.strategy,
        "steps": res.steps,
        "days": res.steps // steps_per_day,
        "net_pnl_usd": _usd(net),
        "costs_ignored_pnl_usd": _usd(fr),
        "pnl_decomposition_usd": {
            "mid_marked": _usd(fr),
            "spread_and_slippage": _usd(spread),
            "fees_and_rebates": _usd(lg.fee_pnl),
            "net": _usd(net),
        },
        "sharpe_net": _sharpe(res.net_equity, steps_per_day),
        "sharpe_costs_ignored": _sharpe(res.frictionless_equity, steps_per_day),
        "max_drawdown_usd": _usd(_max_drawdown(res.net_equity)),
        "max_drawdown_costs_ignored_usd": _usd(_max_drawdown(res.frictionless_equity)),
        "fills": lg.n_fills,
        "filled_shares": lg.filled_qty,
        "offered_shares": res.offered_qty,
        "fill_ratio": round(lg.filled_qty / res.offered_qty, 4) if res.offered_qty else 0.0,
        "maker_share": round(lg.maker_qty / lg.filled_qty, 4) if lg.filled_qty else 0.0,
        "inventory": {
            "final": lg.position,
            "mean_abs": round(statistics.fmean(abs(p) for p in pos), 1),
            "max_abs": max(abs(p) for p in pos),
        },
        # Mean queue position over every booking of our own orders, in shares.
        "avg_queue_ahead_shares": (
            round(statistics.fmean(res.queue_ahead_samples), 1) if res.queue_ahead_samples else None
        ),
        "orders_sent": len(res.orders),
        "regime_steps": dict(sorted(res.regime_steps.items())),
        "engine_commands": res.engine_commands,
    }


def downsample[T](xs: list[T], n: int = SERIES_POINTS) -> tuple[list[int], list[T]]:
    """At most ``n`` evenly spaced (index, value) samples, always keeping both ends."""
    if len(xs) <= n:
        return list(range(len(xs))), list(xs)
    # Evenly spaced indices from first to last sample. Rounding keeps both ends exact.
    idx = [round(i * (len(xs) - 1) / (n - 1)) for i in range(n)]
    return idx, [xs[i] for i in idx]


def run_all(
    steps: int = BACKTEST_STEPS, seed: int = BACKTEST_SEED
) -> tuple[dict[str, Any], dict[str, BacktestResult]]:
    """Backtest every strategy on one seed: (summaries + ``config``, raw results by name)."""
    # Both strategies share one market seed and one fee schedule, so they are comparable.
    cfg = MarketConfig(seed=seed)
    fees = FeeSchedule()
    out: dict[str, Any] = {}
    raw: dict[str, BacktestResult] = {}
    for strat in (MarketMaker(), Momentum()):
        r = run_backtest(strat, steps, cfg, fees)
        raw[strat.name] = r
        out[strat.name] = summarize(r)
    out["config"] = {
        "steps": steps,
        "seed": seed,
        "steps_per_day": STEPS_PER_DAY,
        "tick_usd": UNITS_PER_TICK / UNITS_PER_USD,
        "taker_fee_usd_per_share": fees.taker_fee / UNITS_PER_USD,
        "maker_rebate_usd_per_share": fees.maker_rebate / UNITS_PER_USD,
        "strategy_latency_steps": STRATEGY_LATENCY_STEPS,
    }
    return out, raw


def main() -> None:
    """Print the primary-seed summary of every strategy as JSON on stdout."""
    sys.stdout.write(json.dumps(run_all()[0], indent=2) + "\n")


if __name__ == "__main__":
    main()
