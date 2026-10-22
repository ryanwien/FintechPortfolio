"""Strategies that trade through the engine.

No-lookahead contract: a strategy only sees a ``View`` built from data up to and including
step t, and its orders reach the engine at step t+1 *after* that step's background flow
(one step of latency). It never sees the fundamental value; the ``Gateway`` exposes only
its own working orders.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar, Final

from .engine import Engine, L2Levels
from .types import Cancel, Command, NewOrder, OrdType, Replace, Side

OrderState = tuple[Side, int, int] | None  # (side, price, leaves) of a working order

# Owner ids and order-id ranges, chosen so they never collide with background flow.
MARKET_MAKER_OWNER: Final = 10
MOMENTUM_OWNER: Final = 11
MARKET_MAKER_ID_BASE: Final = 50_000_000
MOMENTUM_ID_BASE: Final = 60_000_000


@dataclass(frozen=True, slots=True)
class View:
    """Everything a strategy may look at when deciding at step ``t``."""

    t: int
    bids: L2Levels  # L2, best first (includes our own orders)
    asks: L2Levels
    mids_x2: list[int]  # history of 2*mid up to and including t (index = step)
    position: int


class Gateway:
    """What a participant legitimately knows: its own working orders."""

    def __init__(self, engine: Engine) -> None:
        """Wrap ``engine`` as the only read path a strategy has into the book."""
        self._eng = engine

    def order_state(self, oid: int) -> OrderState:
        """(side, price, leaves) of a working order, or None if it is no longer resting."""
        return self._eng.order_state(oid)


class Strategy(ABC):
    """Base class: an owner id, a private order-id range and a ``decide`` policy."""

    name: ClassVar[str] = "base"

    def __init__(self, owner: int, id_base: int) -> None:
        """Set the owner id and the base of this strategy's private order-id range."""
        self.owner = owner
        self._next = id_base

    def new_id(self) -> int:
        """Return the next order id in this strategy's range."""
        self._next += 1
        return self._next

    @abstractmethod
    def decide(self, view: View, gw: Gateway) -> list[Command]:
        """Commands to send; they arrive at the engine one step later."""


class MarketMaker(Strategy):
    """Joins the best bid/ask of *other* participants with post-only orders, skews quotes
    against inventory, and keeps resting orders in place (to keep queue position) unless
    the desired price changes, in which case it cancel/replaces."""

    name: ClassVar[str] = "market_maker"

    def __init__(
        self,
        owner: int = MARKET_MAKER_OWNER,
        size: int = 100,
        max_pos: int = 500,
        skew_per: int = 100,  # shade quotes one tick per this many shares of inventory
    ) -> None:
        """Quote ``size`` shares per side. The adding side is pulled once ``max_pos`` is reached.

        Each ``skew_per`` shares of inventory shifts both quotes one tick against the position.
        """
        super().__init__(owner, MARKET_MAKER_ID_BASE)
        self.size = size
        self.max_pos = max_pos
        self.skew_per = skew_per
        self.bid_id: int | None = None
        self.ask_id: int | None = None

    @staticmethod
    def _best_other(levels: L2Levels, mine: OrderState) -> int | None:
        """Best price with liquidity from someone other than us (our own size netted out)."""
        for price, qty in levels:
            # Subtract our own resting size at this price so our quote is not read as competition.
            others_qty = qty - mine[2] if mine is not None and mine[1] == price else qty
            if others_qty > 0:
                return price
        return None

    def _side(
        self, oid: int | None, want: int | None, side: Side, gw: Gateway, cmds: list[Command]
    ) -> int | None:
        """Move our quote on one side to ``want`` (None = pull it); returns the live id."""
        st = gw.order_state(oid) if oid is not None else None
        # None means pull the quote (position limit). Cancel only if it is still live.
        if want is None:
            if st is not None:
                cmds.append(Cancel(oid))
            return None
        # Filled or never posted: post a fresh quote and track its new id.
        if st is None:
            nid = self.new_id()
            cmds.append(NewOrder(nid, self.owner, side, OrdType.POST_ONLY, self.size, want))
            return nid
        # Same price: leave it alone so the queue position is kept. Only a repriced quote is
        # replaced.
        if st[1] != want:
            cmds.append(Replace(oid, want, self.size))
        return oid

    def decide(self, view: View, gw: Gateway) -> list[Command]:
        """Requote both sides: join the best quote of the other participants, shaded by inventory.

        A side is pulled at the position limit and never quoted through the opposite touch.
        Commands are returned in order and reach the engine one step later.
        """
        bid_st = gw.order_state(self.bid_id) if self.bid_id is not None else None
        ask_st = gw.order_state(self.ask_id) if self.ask_id is not None else None
        # Join the best quote from other participants. Our own size at each price is ignored.
        bb = self._best_other(view.bids, bid_st)
        ba = self._best_other(view.asks, ask_st)
        cmds: list[Command] = []
        if bb is None or ba is None:
            return cmds
        # Inventory skew in whole ticks. int() truncates toward zero, so a small position does not
        # skew the quotes at all.
        k = int(view.position / self.skew_per)  # long => shade both quotes down
        # Clamp so the bid never reaches the best other ask and the ask never reaches the best other
        # bid: we never quote a crossing price.
        want_bid = min(bb - k, ba - 1)
        want_ask = max(ba - k, bb + 1)
        # At the position limit the side that would add inventory is pulled. The other side keeps
        # quoting so the position can unwind.
        if view.position >= self.max_pos:
            want_bid = None
        if view.position <= -self.max_pos:
            want_ask = None
        self.bid_id = self._side(self.bid_id, want_bid, Side.BUY, gw, cmds)
        self.ask_id = self._side(self.ask_id, want_ask, Side.SELL, gw, cmds)
        return cmds


class Momentum(Strategy):
    """Time-series momentum on the mid: go long/short ``target`` shares when the
    ``lookback``-step move exceeds ``entry`` ticks, flatten when it fades below ``exit``.
    Executes with marketable IOC orders capped ``slip_cap`` ticks through the touch."""

    name: ClassVar[str] = "momentum"

    def __init__(
        self,
        owner: int = MOMENTUM_OWNER,
        lookback: int = 60,
        entry: float = 4.0,
        exit: float = 1.0,
        target: int = 500,
        clip: int = 200,
        slip_cap: int = 2,
    ) -> None:
        """Set the signal and sizing parameters.

        ``entry`` and ``exit`` are in ticks of mid move over ``lookback`` steps.
        """
        super().__init__(owner, MOMENTUM_ID_BASE)
        self.lookback = lookback
        self.entry = entry
        self.exit = exit
        self.target = target
        self.clip = clip
        self.slip_cap = slip_cap
        self.want = 0

    def decide(
        self,
        view: View,
        gw: Gateway,  # noqa: ARG002 - interface arg; IOC-only, so no working orders to query
    ) -> list[Command]:
        """Trade toward a target position set by the lookback move.

        Go long above ``entry``, short below ``-entry``, flat inside ``exit``, and hold the
        previous target in between. Orders are IOCs of at most ``clip`` shares, priced up to
        ``slip_cap`` ticks through the touch.
        """
        m = view.mids_x2
        # Not enough history for the lookback, or a side is empty: no signal, no orders.
        if len(m) <= self.lookback or not view.bids or not view.asks:
            return []
        # mids_x2 are doubled, so halve the difference to get the mid move in ticks.
        signal = (m[-1] - m[-1 - self.lookback]) / 2  # ticks
        # Hysteresis: between exit and entry the previous target is held, so a signal near the
        # threshold does not flip the position every step.
        if signal > self.entry:
            self.want = self.target
        elif signal < -self.entry:
            self.want = -self.target
        elif abs(signal) < self.exit:
            self.want = 0
        delta = self.want - view.position
        if delta == 0:
            return []
        # Trade toward the target in clips of at most `clip` shares, limiting how far one IOC can
        # walk the book.
        qty = min(abs(delta), self.clip)
        if delta > 0:
            # Priced slip_cap ticks through the ask: it fills at the touch when there is size and
            # sweeps at most slip_cap deeper.
            px = view.asks[0][0] + self.slip_cap
            return [NewOrder(self.new_id(), self.owner, Side.BUY, OrdType.IOC, qty, px)]
        # Priced slip_cap ticks through the bid, the mirror of the buy case.
        px = view.bids[0][0] - self.slip_cap
        return [NewOrder(self.new_id(), self.owner, Side.SELL, OrdType.IOC, qty, px)]
