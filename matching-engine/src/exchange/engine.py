"""Price-time priority limit order book and matching engine.

Data structure
--------------
* Each side keeps ``levels: dict[price -> _Level]`` plus a sorted list of level keys.
  Keys are stored so that the best price is always at the *end* of the list
  (bids: key = price, asks: key = -price), so consuming the top of book is ``list.pop()``.
* Each level is an intrusive doubly linked FIFO of ``_Order`` objects, so a cancel is
  O(1) once the order is found, and ``_index: dict[order_id -> _Order]`` finds it in O(1).
  (Removing an emptied level costs a bisect + list delete on the key list, which is
  O(log n + levels) but the key list is short and the memmove is tiny.)

Every event carries a global, gap-free sequence number. Every processed command is
appended to ``journal`` (when enabled), and ``rebuild.replay(journal)`` reproduces the
exact event stream and book hash.

Performance note: ``process`` and ``_match`` are the hot path. They deliberately bind
attributes to locals, dispatch on ``type(x) is`` and avoid helper calls per fill; keep it
that way when editing them.
"""

from __future__ import annotations

import hashlib
from bisect import bisect_left, insort
from collections.abc import Iterable, Iterator
from typing import Final

from .types import (
    Ack,
    BookDelta,
    Booked,
    Cancel,
    Cancelled,
    Command,
    Event,
    Fill,
    NewOrder,
    OrdType,
    Reject,
    Replace,
    Replaced,
    Side,
    Snapshot,
    SnapshotRequest,
)

# Module-level aliases: a global lookup is cheaper than ``Side.BUY`` on the hot path.
BUY: Final = Side.BUY
SELL: Final = Side.SELL

L2Levels = tuple[tuple[int, int], ...]  # (price, aggregate qty), best first
L3Level = tuple[int, int, list[tuple[int, int, int]]]  # (side, price, [(oid, owner, qty)])


class _Order:
    """A resting order: a node in its price level's intrusive doubly linked FIFO."""

    __slots__ = ("level", "next", "oid", "otype", "owner", "prev", "price", "qty", "side")

    def __init__(
        self, oid: int, owner: int, side: Side, price: int, qty: int, otype: OrdType
    ) -> None:
        self.oid = oid
        self.owner = owner
        self.side = side
        self.price = price
        self.qty = qty
        self.otype = otype
        self.prev: _Order | None = None
        self.next: _Order | None = None
        self.level: _Level | None = None


class _Level:
    """All orders resting at one price, in time priority, plus their aggregate quantity."""

    __slots__ = ("count", "head", "price", "qty", "tail")

    def __init__(self, price: int) -> None:
        self.price = price
        self.head: _Order | None = None
        self.tail: _Order | None = None
        self.qty = 0
        self.count = 0

    def append(self, o: _Order) -> None:
        """Queue ``o`` at the back of the FIFO and add its quantity to the level total."""
        o.level = self
        o.next = None
        o.prev = self.tail
        if self.tail is None:
            self.head = o
        else:
            self.tail.next = o
        self.tail = o
        self.qty += o.qty
        self.count += 1

    def unlink(self, o: _Order) -> None:
        """Detach ``o`` from the queue in O(1) and subtract its quantity.

        The caller removes the level once it is empty. The order index is not touched.
        """
        if o.prev is None:
            self.head = o.next
        else:
            o.prev.next = o.next
        if o.next is None:
            self.tail = o.prev
        else:
            o.next.prev = o.prev
        self.qty -= o.qty
        self.count -= 1
        o.prev = o.next = None
        o.level = None

    def orders(self) -> Iterator[_Order]:
        """Orders in time priority (queue head first)."""
        o = self.head
        while o is not None:
            yield o
            o = o.next


class _BookSide:
    """One side of the book: levels by price plus a key list sorted so the best is last.

    Keys are ``price * sign`` (bids: +price, asks: -price), so ascending key order is
    worst-to-best for both sides and the top of book is always ``keys[-1]``.
    """

    __slots__ = ("keys", "levels", "side", "sign")

    def __init__(self, side: Side) -> None:
        self.side = side
        self.levels: dict[int, _Level] = {}
        self.keys: list[int] = []  # ascending; best level is keys[-1]
        self.sign = side.sign

    def best(self) -> int | None:
        """Best price on this side, or None if the side is empty."""
        return self.keys[-1] * self.sign if self.keys else None

    def level_for(self, price: int) -> _Level:
        """The level at ``price``, created (and its key inserted) if it does not exist."""
        lvl = self.levels.get(price)
        if lvl is None:
            # A new level's key goes into the sorted list at once, so best-price lookups stay valid.
            lvl = _Level(price)
            self.levels[price] = lvl
            # Keys are price * sign, so insort keeps the list best-last with no comparator.
            insort(self.keys, price * self.sign)
        return lvl

    def drop(self, price: int) -> None:
        """Remove an empty level; the common case (top of book) is a ``pop()``."""
        del self.levels[price]
        k = price * self.sign
        keys = self.keys
        if keys[-1] == k:
            keys.pop()
        else:
            del keys[bisect_left(keys, k)]

    def iter_levels(self) -> Iterator[_Level]:
        """Best to worst."""
        levels, sign = self.levels, self.sign
        for k in reversed(self.keys):
            yield levels[k * sign]


def hash_book(levels: Iterable[tuple[int, int, Iterable[tuple[int, int, int]]]]) -> str:
    """Canonical hash of an L3 book: (side, price, [(order_id, owner, qty), ...]) best-first."""
    h = hashlib.sha256()
    # Separators make the encoding unambiguous: '|' ends a header, ';' ends a level.
    for side, price, orders in levels:
        h.update(f"{side}|{price}|".encode())
        h.update(",".join(f"{a}:{b}:{c}" for a, b, c in orders).encode())
        h.update(b";")
    return h.hexdigest()


class Engine:
    """Single-instrument matching engine with strict price-time priority.

    Args:
        journal: append every processed command to ``self.journal`` (needed for replay).
        keep_events: also accumulate every emitted event in ``self.events``.
    """

    def __init__(self, journal: bool = True, keep_events: bool = False) -> None:
        """Create an empty book. ``journal`` and ``keep_events`` choose what is retained."""
        self._sides = (_BookSide(BUY), _BookSide(SELL))
        self._index: dict[int, _Order] = {}
        self._seen: set[int] = set()
        self._seq = 0
        self._out: list[Event] = []
        self._dirty: dict[tuple[Side, int], None] = {}  # ordered set of touched levels
        self.journal: list[Command] | None = [] if journal else None
        self.events: list[Event] | None = [] if keep_events else None

    # ------------------------------------------------------------------ public api
    def process(self, cmd: Command) -> list[Event]:
        """Apply one command; return the events it produced (in sequence order)."""
        self._out = out = []
        self._dirty = dirty = {}
        # Exact-type dispatch: `type(x) is` is cheaper than isinstance on the hot path.
        t = type(cmd)
        if t is NewOrder:
            self._new(cmd)  # type: ignore[arg-type]
        elif t is Cancel:
            self._cancel(cmd)  # type: ignore[arg-type]
        elif t is Replace:
            self._replace(cmd)  # type: ignore[arg-type]
        elif t is SnapshotRequest:
            self._seq += 1
            # A snapshot changes no book state, but it still takes a sequence number so the event
            # stream has no gaps.
            out.append(Snapshot(self._seq, *self.l2(cmd.depth)))  # type: ignore[union-attr]
        else:
            raise TypeError(f"unknown command {cmd!r}")
        # One BookDelta per touched level with its final aggregate qty. A level that the command
        # emptied was already removed from the book, so the lookup yields 0.
        if dirty:
            sides = self._sides
            for side, price in dirty:
                lvl = sides[side].levels.get(price)
                self._seq += 1
                out.append(BookDelta(self._seq, side, price, lvl.qty if lvl is not None else 0))
        # The journal stores inputs, not outputs: replaying them re-derives the same events.
        if self.journal is not None:
            self.journal.append(cmd)
        if self.events is not None:
            self.events.extend(out)
        return out

    @property
    def seq(self) -> int:
        """Sequence number of the last event emitted."""
        return self._seq

    def best_bid(self) -> int | None:
        """Highest resting bid price, or None if there are no bids."""
        return self._sides[BUY].best()

    def best_ask(self) -> int | None:
        """Lowest resting ask price, or None if there are no asks."""
        return self._sides[SELL].best()

    def has_order(self, oid: int) -> bool:
        """True if order ``oid`` is still resting. Filled, cancelled and unknown ids are False."""
        return oid in self._index

    def order_state(self, oid: int) -> tuple[Side, int, int] | None:
        """(side, price, leaves) for a resting order, else None."""
        o = self._index.get(oid)
        return None if o is None else (o.side, o.price, o.qty)

    def queue_ahead(self, oid: int) -> int:
        """Shares resting ahead of ``oid`` at its price level (its queue position)."""
        o = self._index[oid]
        # Sum the open qty ahead of the order by walking back to the head. Analytics only, so the
        # linear walk is acceptable.
        ahead = 0
        p = o.prev
        while p is not None:
            ahead += p.qty
            p = p.prev
        return ahead

    def l2(self, depth: int | None = None) -> tuple[L2Levels, L2Levels]:
        """(bids, asks) as (price, aggregate qty) best first, ``depth`` levels per side."""
        res: list[L2Levels] = []
        for s in self._sides:
            lv = []
            for lvl in s.iter_levels():
                if depth is not None and len(lv) >= depth:
                    break
                lv.append((lvl.price, lvl.qty))
            res.append(tuple(lv))
        # Results are indexed by Side value (BUY = 0, SELL = 1), so res[BUY] is the bid side.
        return res[BUY], res[SELL]

    def depth_stats(self) -> dict[str, int]:
        """Level counts per side, resting order count and total resting quantity."""
        b, a = self._sides
        return {
            "bid_levels": len(b.keys),
            "ask_levels": len(a.keys),
            "resting_orders": len(self._index),
            "resting_qty": sum(lvl.qty for s in self._sides for lvl in s.levels.values()),
        }

    def l3(self) -> Iterator[L3Level]:
        """Full order-by-order book: bids then asks, best level first, FIFO within a level."""
        for s in self._sides:
            for lvl in s.iter_levels():
                yield int(s.side), lvl.price, [(o.oid, o.owner, o.qty) for o in lvl.orders()]

    def book_hash(self) -> str:
        """Canonical hash of the L3 book (see ``hash_book``)."""
        return hash_book(self.l3())

    def check_invariants(self) -> None:
        """Expensive structural self-check used by tests."""
        bb, ba = self.best_bid(), self.best_ask()
        assert bb is None or ba is None or bb < ba, f"crossed book {bb} >= {ba}"
        # Cross-check the key list, level map and order index against each other. Each assert
        # catches a different kind of bookkeeping bug.
        n = 0
        for s in self._sides:
            assert sorted(s.keys) == s.keys
            assert len(s.keys) == len(s.levels)
            for lvl in s.levels.values():
                orders = list(lvl.orders())
                assert orders, "empty level left on book"
                assert lvl.count == len(orders)
                assert lvl.qty == sum(o.qty for o in orders)
                for o in orders:
                    assert o.qty > 0
                    assert o.price == lvl.price
                    assert o.side == s.side
                    assert self._index[o.oid] is o
                n += len(orders)
        assert n == len(self._index)

    # ------------------------------------------------------------------ internals
    def _emit(self, ev_cls: type[Event], *args: object) -> None:
        """Stamp the next sequence number on a new event and queue it for the current command."""
        self._seq += 1
        self._out.append(ev_cls(self._seq, *args))

    def _crosses(self, side: Side, price: int) -> bool:
        """Would a ``side`` order at ``price`` take liquidity right now?"""
        if side is BUY:
            a = self._sides[SELL].best()
            return a is not None and price >= a
        b = self._sides[BUY].best()
        return b is not None and price <= b

    def _new(self, c: NewOrder) -> None:
        """Validate, ack, match, then rest or cancel the remainder per ``otype``."""
        oid, otype, qty = c.order_id, c.otype, c.qty
        price = None if otype is OrdType.MARKET else c.price
        # Checks run in a fixed order and each reject returns before any state changes. Ids are
        # never reused: _seen is not cleared when an order fills or is cancelled.
        if oid in self._seen:
            return self._emit(Reject, oid, "duplicate_order_id")
        if type(qty) is not int or qty <= 0:
            return self._emit(Reject, oid, "bad_qty")
        if otype is not OrdType.MARKET and (price is None or price <= 0):
            return self._emit(Reject, oid, "bad_price")
        if otype is OrdType.POST_ONLY and self._crosses(c.side, price):  # type: ignore[arg-type]
            return self._emit(Reject, oid, "post_only_would_cross")
        # Accepted: record the id, then ack before matching so the Ack precedes any fill.
        self._seen.add(oid)
        self._emit(Ack, oid, c.owner, c.side, otype, price, qty)

        # FOK is all or nothing: prove the full quantity is fillable before touching the book.
        if otype is OrdType.FOK and self._available(c.side, price, c.owner, qty) < qty:  # type: ignore[arg-type]
            return self._emit(Cancelled, oid, qty, "fok_unfilled")

        # Self-trade prevention cancels the incoming (newest) order's remainder, never the resting
        # order it would have hit.
        remaining, stp = self._match(oid, c.owner, c.side, price, qty)
        if remaining == 0:
            return None
        if stp:
            return self._emit(Cancelled, oid, remaining, "self_trade")
        if otype is OrdType.LIMIT or otype is OrdType.POST_ONLY:
            return self._rest(_Order(oid, c.owner, c.side, price, remaining, otype))  # type: ignore[arg-type]
        # Only LIMIT and POST_ONLY remainders rest. A FOK never gets here with a remainder, because
        # _available mirrors _match's stop rule and already proved a full fill.
        reason = "no_liquidity" if otype is OrdType.MARKET else "ioc_unfilled"
        return self._emit(Cancelled, oid, remaining, reason)

    def _rest(self, o: _Order) -> None:
        """Book ``o`` at the back of its price level."""
        # Joining the back of the level is what gives the order time priority.
        self._sides[o.side].level_for(o.price).append(o)
        self._index[o.oid] = o
        # Booked is per order; the dirty level is reported once per command as a BookDelta.
        self._dirty[(o.side, o.price)] = None
        self._emit(Booked, o.oid, o.side, o.price, o.qty)

    def _available(self, side: Side, limit: int, owner: int, need: int) -> int:
        """Quantity a taker could fill right now, stopping where self-trade prevention would."""
        total = 0
        # Walk the opposite side best-first, stopping at the first price the taker cannot cross.
        for lvl in self._sides[side.opposite].iter_levels():
            if (lvl.price > limit) if side is BUY else (lvl.price < limit):
                break
            o = lvl.head
            while o is not None:
                # Self-trade prevention: stop counting at our own order, exactly where _match stops,
                # so FOK is never promised quantity that matching would not deliver.
                if o.owner == owner:
                    return total
                total += o.qty
                if total >= need:
                    return total
                o = o.next
        return total

    def _match(
        self, oid: int, owner: int, side: Side, limit: int | None, qty: int
    ) -> tuple[int, bool]:
        """Match an incoming taker. Returns (unfilled qty, stopped_by_self_trade)."""
        # Hot path: bind the opposite side's containers to locals once, not per fill.
        opp = self._sides[SELL] if side is BUY else self._sides[BUY]
        keys, levels, sign = opp.keys, opp.levels, opp.sign
        out, dirty, index = self._out, self._dirty, self._index
        opp_side = opp.side
        remaining = qty
        is_buy = side is BUY
        # Sweep best-to-worst. keys[-1] is the best level because of the price * sign encoding.
        while remaining and keys:
            best = keys[-1] * sign
            # Stop at the first level that no longer crosses the limit. Market orders pass
            # limit=None and never stop on price.
            if limit is not None and (best > limit if is_buy else best < limit):
                break
            lvl = levels[best]
            o = lvl.head
            stp = False
            while o is not None and remaining:
                # Reaching our own resting order ends the sweep and reports self-trade prevention.
                if o.owner == owner:
                    stp = True
                    break
                # A fill is the smaller of the maker's open qty and the taker's remainder. The maker
                # keeps any excess and its queue position.
                q = o.qty if o.qty < remaining else remaining
                o.qty -= q
                lvl.qty -= q
                remaining -= q
                self._seq += 1
                out.append(
                    Fill(self._seq, oid, o.oid, owner, o.owner, side, best, q, o.qty, remaining)
                )
                # Read the successor before a possible unlink, which clears o.next.
                nxt = o.next
                # A fully filled maker leaves the queue and the index. A partly filled one
                # stays queued.
                if o.qty == 0:
                    lvl.unlink(o)
                    del index[o.oid]
                o = nxt
            dirty[(opp_side, best)] = None
            # This sweep only empties the best level, so its key is keys[-1] and pop() removes it.
            # Done inline because the hot loop already holds the keys list.
            if lvl.count == 0:
                del levels[best]
                keys.pop()
            # A self-trade stops the sweep here. The caller decides what happens to the remainder.
            if stp:
                return remaining, True
        return remaining, False

    def _remove(self, o: _Order) -> None:
        """Unlink a resting order from its level and the index (emits no event)."""
        lvl = o.level
        assert lvl is not None
        # Unlink in O(1). Only an emptied level touches the sorted key list; a level that still has
        # orders just shrinks in place.
        lvl.unlink(o)
        if lvl.count == 0:
            self._sides[o.side].drop(o.price)
        self._dirty[(o.side, o.price)] = None
        del self._index[o.oid]

    def _cancel(self, c: Cancel) -> None:
        """Remove a resting order's whole open quantity. An unknown id gets a Reject instead."""
        o = self._index.get(c.order_id)
        if o is None:
            return self._emit(Reject, c.order_id, "unknown_order")
        # Capture the open qty before removal so Cancelled reports what was actually removed.
        qty = o.qty
        self._remove(o)
        return self._emit(Cancelled, o.oid, qty, "user")

    def _replace(self, c: Replace) -> None:
        """Cancel/replace; see ``types.Replace`` for the priority rules."""
        o = self._index.get(c.order_id)
        if o is None:
            return self._emit(Reject, c.order_id, "unknown_order")
        if type(c.qty) is not int or c.qty <= 0:
            return self._emit(Reject, c.order_id, "bad_qty")
        if c.price <= 0:
            return self._emit(Reject, c.order_id, "bad_price")
        # Capture the pre-change state for the Replaced event before anything mutates o.
        old_price, old_qty = o.price, o.qty
        # Priority rule: a same-price, not-larger request keeps the queue position. The level total
        # is adjusted in place and the order keeps its node in the FIFO.
        if c.price == old_price and c.qty <= old_qty:
            # Size decrease (or no-op) at the same price keeps time priority.
            lvl = o.level
            assert lvl is not None
            lvl.qty -= old_qty - c.qty
            o.qty = c.qty
            # An equal qty changes nothing on the book, so no level delta is needed.
            if c.qty != old_qty:
                self._dirty[(o.side, o.price)] = None
            return self._emit(Replaced, o.oid, old_price, old_qty, c.price, c.qty, True)
        # A repriced post-only order must not cross. A same-price size change cannot cross, because
        # the order already rests on an uncrossed book.
        if o.otype is OrdType.POST_ONLY and c.price != old_price and self._crosses(o.side, c.price):
            return self._emit(Reject, o.oid, "post_only_would_cross")
        # Price change or size increase: loses priority. Pull it, re-enter as a taker.
        # A repriced or enlarged order loses priority: pull it, report it, then re-enter it.
        self._remove(o)
        self._emit(Replaced, o.oid, old_price, old_qty, c.price, c.qty, False)
        # The re-entered order is a taker: it may now cross and trade at its new price.
        remaining, stp = self._match(o.oid, o.owner, o.side, c.price, c.qty)
        if remaining == 0:
            return None
        if stp:
            return self._emit(Cancelled, o.oid, remaining, "self_trade")
        # _remove dropped the index entry and _rest re-adds it, so the same node goes to the back of
        # the queue at the new price.
        o.price = c.price
        o.qty = remaining
        return self._rest(o)
