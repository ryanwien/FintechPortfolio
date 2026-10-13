"""The two ways to reconstruct engine state, both of which must reproduce the book hash.

* ``replay(journal)`` re-runs the *input* commands on a fresh engine; the event stream and
  book hash must be identical (determinism).
* ``BookBuilder`` rebuilds the L3 book from the *output* events alone, as a drop-copy, risk
  or market-data consumer would (event sourcing).
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from .engine import Engine, L3Level, hash_book
from .types import Ack, Booked, Cancelled, Command, Event, Fill, Replaced, Side


def replay(journal: Iterable[Command]) -> Engine:
    """Re-run a command journal on a fresh engine (events are kept for comparison)."""
    eng = Engine(journal=True, keep_events=True)
    for cmd in journal:
        eng.process(cmd)
    return eng


@dataclass(slots=True)
class _OrderRecord:
    """What a downstream consumer knows about one live order."""

    owner: int
    side: Side
    price: int
    leaves: int
    resting: bool = False


class BookBuilder:
    """Rebuilds the full L3 book purely from the engine's *output* event stream.

    Its hash must match ``Engine.book_hash()`` exactly; the property tests check this.
    """

    def __init__(self) -> None:
        self.orders: dict[int, _OrderRecord] = {}
        # Per side: price -> order ids in time priority.
        self.book: tuple[dict[int, list[int]], dict[int, list[int]]] = ({}, {})

    def _unrest(self, oid: int) -> None:
        rec = self.orders[oid]
        if rec.resting:
            queue = self.book[rec.side][rec.price]
            queue.remove(oid)
            if not queue:
                del self.book[rec.side][rec.price]
            rec.resting = False

    def best(self, side: Side) -> int | None:
        """Best resting price on ``side``, or None if that side is empty."""
        levels = self.book[side]
        if not levels:
            return None
        return max(levels) if side is Side.BUY else min(levels)

    def head(self, side: Side) -> int | None:
        """Order id at the front of the queue at the best price on ``side``."""
        price = self.best(side)
        return None if price is None else self.book[side][price][0]

    def apply(self, ev: Event) -> None:
        """Fold one engine event into the book."""
        t = type(ev)
        if t is Ack:
            self.orders[ev.order_id] = _OrderRecord(ev.owner, ev.side, ev.price or 0, ev.qty)
        elif t is Fill:
            for oid in (ev.maker_id, ev.taker_id):
                rec = self.orders[oid]
                rec.leaves -= ev.qty
                if rec.leaves == 0:
                    self._unrest(oid)
                    del self.orders[oid]
        elif t is Booked:
            rec = self.orders[ev.order_id]
            rec.price, rec.leaves, rec.resting = ev.price, ev.qty, True
            self.book[ev.side].setdefault(ev.price, []).append(ev.order_id)
        elif t is Cancelled:
            self._unrest(ev.order_id)
            del self.orders[ev.order_id]
        elif t is Replaced:
            rec = self.orders[ev.order_id]
            if not ev.priority_kept:
                self._unrest(ev.order_id)
                rec.price = ev.new_price
            rec.leaves = ev.new_qty

    def l3(self) -> Iterator[L3Level]:
        """Same shape and ordering as ``Engine.l3``."""
        for side in Side:
            levels = self.book[side]
            for price in sorted(levels, reverse=side is Side.BUY):
                orders = [
                    (oid, self.orders[oid].owner, self.orders[oid].leaves) for oid in levels[price]
                ]
                yield int(side), price, orders

    def book_hash(self) -> str:
        """Canonical hash of the rebuilt L3 book; must equal ``Engine.book_hash()``."""
        return hash_book(self.l3())
