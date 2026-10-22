"""Deterministic synthetic market: a regime-switching fundamental value plus background
participants that trade through the real matching engine.

Participants (all orders go through ``Engine.process``):

* makers   - post-only limit orders at a geometric distance from the fundamental value,
             random cancels, and periodic culling of quotes that drifted far from value
* noise    - uninformed market orders
* informed - pick off stale quotes: buy any ask below value (sell any bid above it) with an IOC

The informed flow is what makes passive liquidity provision costly (adverse selection),
so a market maker in this world has to earn spread + rebate faster than it gets picked off.

Determinism: the fundamental process and the participants draw from two separate seeded
RNG streams, so the value path does not depend on what anyone trades.
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from .config import BACKTEST_SEED
from .engine import Engine
from .types import Cancel, Command, Event, NewOrder, OrdType, Side

REGIMES: Final = ("calm", "trend_up", "trend_down", "volatile")
# Regime -> (drift ticks/step, vol ticks/step).
REGIME_PARAMS: Final[dict[str, tuple[float, float]]] = {
    "calm": (0.0, 0.30),
    "trend_up": (0.025, 0.35),
    "trend_down": (-0.025, 0.35),
    "volatile": (0.0, 0.90),
}
INITIAL_REGIME: Final = "calm"

MAKER_OWNERS: Final = range(100, 120)
NOISE_OWNER: Final = 200
INFORMED_OWNER: Final = 300

# Each RNG stream is derived from the scenario seed with its own multiplier and offset.
_FUNDAMENTAL_SEED_MULT: Final = 1_000_003
_FUNDAMENTAL_SEED_OFFSET: Final = 1
_PARTICIPANT_SEED_MULT: Final = 7_919
_PARTICIPANT_SEED_OFFSET: Final = 2

SEED_BOOK_ORDERS: Final = 60  # maker orders placed before the first step


@dataclass(frozen=True, slots=True)
class MarketConfig:
    """Background-market parameters. Prices in ticks ($0.01), sizes in shares."""

    seed: int = BACKTEST_SEED
    start_price: int = 10_000  # ticks => $100.00
    regime_stay_prob: float = 0.997
    maker_adds_per_step: int = 4
    maker_cancels_per_step: int = 3
    maker_mean_depth: float = 3.0  # mean extra ticks from value of a maker quote
    maker_max_lots: int = 5
    noise_prob: float = 0.30
    noise_max_lots: int = 3
    informed_prob: float = 0.60
    informed_edge: float = 1.0  # ticks of mispricing needed before informed flow acts
    informed_lots: int = 3
    cull_every: int = 25  # steps between culls of stale maker quotes
    cull_distance: int = 15  # ticks from value beyond which a maker quote is culled
    lot: int = 100


def random_side(rng: random.Random) -> Side:
    """Fair coin flip between buy and sell (consumes exactly one draw from ``rng``)."""
    return Side.BUY if rng.random() < 0.5 else Side.SELL


class FundamentalProcess:
    """Regime-switching random walk for the (unobservable) fundamental value.

    Uses its own RNG stream, so the path is identical no matter what anyone trades.
    """

    def __init__(self, cfg: MarketConfig) -> None:
        """Start at ``cfg.start_price`` in the initial regime, on a seed-derived RNG stream."""
        self.cfg = cfg
        self.rng = random.Random(cfg.seed * _FUNDAMENTAL_SEED_MULT + _FUNDAMENTAL_SEED_OFFSET)
        self.value = float(cfg.start_price)
        self.regime = INITIAL_REGIME

    def step(self) -> float:
        """Possibly switch regime, then move the value by drift + vol * N(0, 1)."""
        rng = self.rng
        if rng.random() > self.cfg.regime_stay_prob:
            self.regime = rng.choice([r for r in REGIMES if r != self.regime])
        drift, vol = REGIME_PARAMS[self.regime]
        self.value += drift + vol * rng.gauss(0.0, 1.0)
        return self.value


class BackgroundMarket:
    """The crowd the strategies trade against.

    ``execute`` is the backtester's command sink (it routes to ``Engine.process`` and
    books the strategy's own fills), so background orders and strategy orders share one
    engine and one sequence of events.
    """

    def __init__(
        self, engine: Engine, cfg: MarketConfig, execute: Callable[[Command], list[Event]]
    ) -> None:
        """Set up the crowd on ``engine``. Every order it sends goes through ``execute``."""
        self.eng = engine
        self.cfg = cfg
        self.exec = execute
        self.fund = FundamentalProcess(cfg)
        self.rng = random.Random(cfg.seed * _PARTICIPANT_SEED_MULT + _PARTICIPANT_SEED_OFFSET)
        self.next_id = 1
        self.maker_orders: list[int] = []  # possibly stale: pruned lazily
        self.maker_price: dict[int, int] = {}

    def _oid(self) -> int:
        """Return the next background order id from this market's counter."""
        oid = self.next_id
        self.next_id += 1
        return oid

    def _maker_add(self, v: float) -> None:
        """Post one post-only maker quote at a random depth outside value ``v``.

        The quote is tracked for later cancel or cull. Non-positive prices are skipped.
        """
        rng, cfg = self.rng, self.cfg
        side = random_side(rng)
        depth = 1 + int(rng.expovariate(1.0 / cfg.maker_mean_depth))
        price = math.ceil(v) - depth if side is Side.BUY else math.floor(v) + depth
        if price <= 0:
            return
        oid = self._oid()
        owner = MAKER_OWNERS[rng.randrange(len(MAKER_OWNERS))]
        qty = cfg.lot * rng.randint(1, cfg.maker_max_lots)
        self.exec(NewOrder(oid, owner, side, OrdType.POST_ONLY, qty, price))
        if self.eng.has_order(oid):
            self.maker_orders.append(oid)
            self.maker_price[oid] = price

    def _drop(self, i: int) -> int:
        """Swap-remove ``maker_orders[i]`` and forget its price; returns its id."""
        lst = self.maker_orders
        oid = lst[i]
        lst[i] = lst[-1]
        lst.pop()
        self.maker_price.pop(oid, None)
        return oid

    def _maker_cancel_random(self) -> None:
        """Cancel one random tracked maker quote that is still live.

        Stale ids drawn on the way are discarded. At most one cancel is sent.
        """
        lst = self.maker_orders
        while lst:
            oid = self._drop(self.rng.randrange(len(lst)))
            if self.eng.has_order(oid):
                self.exec(Cancel(oid))
                return

    def _cull(self, v: float) -> None:
        """Forget filled maker orders and cancel those far from the value ``v``."""
        i = 0
        lst = self.maker_orders
        while i < len(lst):
            oid = lst[i]
            if not self.eng.has_order(oid):
                self._drop(i)
                continue
            if abs(self.maker_price[oid] - v) > self.cfg.cull_distance:
                self._drop(i)
                self.exec(Cancel(oid))
                continue
            i += 1

    def seed_book(self) -> None:
        """Populate the book with maker quotes around the starting value."""
        v = self.fund.value
        for _ in range(SEED_BOOK_ORDERS):
            self._maker_add(v)

    def step(self, t: int) -> float:
        """Advance one step. Returns the new fundamental value (for analytics only)."""
        cfg, rng, eng = self.cfg, self.rng, self.eng
        v = self.fund.step()

        # Informed flow reacts first to the new value: picks off stale quotes.
        if rng.random() < cfg.informed_prob:
            ask, bid = eng.best_ask(), eng.best_bid()
            informed_qty = cfg.informed_lots * cfg.lot
            if ask is not None and ask <= v - cfg.informed_edge:
                px = math.floor(v - cfg.informed_edge / 2)
                self.exec(
                    NewOrder(self._oid(), INFORMED_OWNER, Side.BUY, OrdType.IOC, informed_qty, px)
                )
            elif bid is not None and bid >= v + cfg.informed_edge:
                px = math.ceil(v + cfg.informed_edge / 2)
                self.exec(
                    NewOrder(self._oid(), INFORMED_OWNER, Side.SELL, OrdType.IOC, informed_qty, px)
                )

        if rng.random() < cfg.noise_prob:
            side = random_side(rng)
            qty = cfg.lot * rng.randint(1, cfg.noise_max_lots)
            self.exec(NewOrder(self._oid(), NOISE_OWNER, side, OrdType.MARKET, qty))

        for _ in range(cfg.maker_cancels_per_step):
            self._maker_cancel_random()
        for _ in range(cfg.maker_adds_per_step):
            self._maker_add(v)
        if t % cfg.cull_every == 0:
            self._cull(v)
        return v
