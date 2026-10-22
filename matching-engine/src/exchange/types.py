"""Commands (inputs) and events (outputs) of the matching engine.

Invariants: prices are integer ticks and quantities are integer shares everywhere; nothing
in the engine ever touches a float. Every event carries the engine's global, gap-free
sequence number as its first field. All messages are immutable value objects.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum


class Side(IntEnum):
    """Order side. The integer value indexes per-side arrays (``BUY`` = 0, ``SELL`` = 1)."""

    BUY = 0
    SELL = 1

    @property
    def opposite(self) -> Side:
        """The side that trades against this one."""
        return Side.SELL if self is Side.BUY else Side.BUY

    @property
    def sign(self) -> int:
        """+1 for a buy, -1 for a sell: the direction in which this side's position moves."""
        return 1 if self is Side.BUY else -1


class OrdType(IntEnum):
    """Order type; decides what happens to the quantity that does not match on arrival."""

    LIMIT = 0  # rests whatever does not match
    MARKET = 1  # no price; sweeps the book, remainder cancelled
    IOC = 2  # limit price; immediate-or-cancel
    FOK = 3  # limit price; fill completely right now or not at all
    POST_ONLY = 4  # limit price; rejected if it would take liquidity


# --------------------------------------------------------------------------- commands


@dataclass(frozen=True, slots=True)
class NewOrder:
    """Submit an order. ``price`` is required for every type except ``MARKET``."""

    order_id: int
    owner: int
    side: Side
    otype: OrdType
    qty: int
    price: int | None = None


@dataclass(frozen=True, slots=True)
class Cancel:
    """Pull a resting order; the whole open quantity is cancelled."""

    order_id: int


@dataclass(frozen=True, slots=True)
class Replace:
    """Cancel/replace. ``qty`` is the new *leaves* (open) quantity.

    Same price and qty <= leaves keeps time priority; anything else (price change or a
    size increase) is re-queued at the back and may match if it now crosses.
    """

    order_id: int
    price: int
    qty: int


@dataclass(frozen=True, slots=True)
class SnapshotRequest:
    """Ask for an L2 snapshot of the top ``depth`` levels per side."""

    depth: int = 10


Command = NewOrder | Cancel | Replace | SnapshotRequest


# --------------------------------------------------------------------------- events


@dataclass(frozen=True, slots=True)
class Ack:
    """A new order passed validation. ``price`` is ``None`` for a market order."""

    seq: int
    order_id: int
    owner: int
    side: Side
    otype: OrdType
    price: int | None
    qty: int


@dataclass(frozen=True, slots=True)
class Reject:
    """A command was refused and had no effect on the book."""

    seq: int
    order_id: int
    reason: str


@dataclass(frozen=True, slots=True)
class Fill:
    """One execution between an incoming taker and the resting maker at the queue head."""

    seq: int
    taker_id: int
    maker_id: int
    taker_owner: int
    maker_owner: int
    aggressor: Side  # side of the taker
    price: int  # always the maker's resting price
    qty: int
    maker_leaves: int
    taker_leaves: int


@dataclass(frozen=True, slots=True)
class Booked:
    """The (remainder of an) order now rests on the book at the back of its level."""

    seq: int
    order_id: int
    side: Side
    price: int
    qty: int


@dataclass(frozen=True, slots=True)
class Cancelled:
    """Open quantity was removed, either by the owner or by the order's own type rules."""

    seq: int
    order_id: int
    qty: int  # quantity removed
    reason: str  # user | ioc_unfilled | fok_unfilled | no_liquidity | self_trade


@dataclass(frozen=True, slots=True)
class Replaced:
    """A cancel/replace was applied. ``priority_kept`` is False when it was re-queued."""

    seq: int
    order_id: int
    old_price: int
    old_qty: int
    new_price: int
    new_qty: int
    priority_kept: bool


@dataclass(frozen=True, slots=True)
class BookDelta:
    """L2 increment: aggregate quantity now resting at (side, price). 0 = level removed."""

    seq: int
    side: Side
    price: int
    qty: int


@dataclass(frozen=True, slots=True)
class Snapshot:
    """L2 view of the book in response to a ``SnapshotRequest``."""

    seq: int
    bids: tuple[tuple[int, int], ...]  # (price, qty) best first
    asks: tuple[tuple[int, int], ...]


Event = Ack | Reject | Fill | Booked | Cancelled | Replaced | BookDelta | Snapshot
