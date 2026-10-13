"""Unit tests for the matching rules, one behaviour per test."""

from __future__ import annotations

import pytest

from exchange import (
    Ack,
    BookBuilder,
    BookDelta,
    Booked,
    Cancel,
    Cancelled,
    Engine,
    Fill,
    NewOrder,
    OrdType,
    Reject,
    Replace,
    Replaced,
    Side,
    Snapshot,
    SnapshotRequest,
    replay,
)

B, S = Side.BUY, Side.SELL
L, M, IOC, FOK, PO = OrdType.LIMIT, OrdType.MARKET, OrdType.IOC, OrdType.FOK, OrdType.POST_ONLY


def of(events, cls):
    """Events of one type, in order."""
    return [e for e in events if isinstance(e, cls)]


def new(eng, oid, side, price, qty, otype=L, owner=None):
    """Submit a new order; the owner defaults to the order id (so no self-trades)."""
    return eng.process(NewOrder(oid, oid if owner is None else owner, side, otype, qty, price))


def test_limit_rests_and_acks(eng):
    ev = new(eng, 1, B, 100, 10)
    assert [type(e) for e in ev] == [Ack, Booked, BookDelta]
    assert eng.best_bid() == 100
    assert eng.best_ask() is None
    assert ev[-1] == BookDelta(ev[-1].seq, B, 100, 10)


def test_sequence_numbers_are_gap_free(eng):
    new(eng, 1, B, 100, 10)
    new(eng, 2, S, 101, 10)
    new(eng, 3, B, 101, 4, IOC)
    eng.process(Cancel(99))
    eng.process(SnapshotRequest())
    seqs = [e.seq for e in eng.events]
    assert seqs == list(range(1, len(seqs) + 1))


def test_full_and_partial_fill_maker_taker(eng):
    new(eng, 1, S, 101, 10)
    ev = new(eng, 2, B, 101, 4)
    (f,) = of(ev, Fill)
    assert (f.maker_id, f.taker_id, f.price, f.qty, f.aggressor) == (1, 2, 101, 4, B)
    assert f.maker_leaves == 6
    assert f.taker_leaves == 0
    assert eng.order_state(1) == (S, 101, 6)
    assert not of(ev, Booked)


def test_taker_gets_maker_price_and_sweeps_levels(eng):
    new(eng, 1, S, 101, 5)
    new(eng, 2, S, 102, 5)
    new(eng, 3, S, 105, 5)
    ev = new(eng, 4, B, 103, 12)
    fills = of(ev, Fill)
    assert [(f.maker_id, f.price, f.qty) for f in fills] == [(1, 101, 5), (2, 102, 5)]
    (bk,) = of(ev, Booked)
    assert (bk.price, bk.qty) == (103, 2)
    assert eng.best_bid() == 103
    assert eng.best_ask() == 105
    deltas = {(d.side, d.price): d.qty for d in of(ev, BookDelta)}
    assert deltas == {(S, 101): 0, (S, 102): 0, (B, 103): 2}


def test_time_priority_within_level(eng):
    new(eng, 1, B, 100, 5)
    new(eng, 2, B, 100, 5)
    new(eng, 3, B, 100, 5)
    ev = new(eng, 4, S, 100, 7)
    assert [(f.maker_id, f.qty) for f in of(ev, Fill)] == [(1, 5), (2, 2)]
    assert eng.queue_ahead(3) == 3


def test_price_priority_beats_time(eng):
    new(eng, 1, B, 100, 5)
    new(eng, 2, B, 101, 5)
    ev = new(eng, 3, S, 99, 5)
    assert of(ev, Fill)[0].maker_id == 2


def test_market_order_sweeps_and_cancels_rest(eng):
    new(eng, 1, S, 101, 3)
    new(eng, 2, S, 150, 3)
    ev = eng.process(NewOrder(3, 3, B, M, 10))
    assert [f.price for f in of(ev, Fill)] == [101, 150]
    (c,) = of(ev, Cancelled)
    assert (c.qty, c.reason) == (4, "no_liquidity")
    assert of(ev, Ack)[0].price is None


def test_market_order_on_empty_book(eng):
    ev = eng.process(NewOrder(1, 1, S, M, 10))
    assert [type(e) for e in ev] == [Ack, Cancelled]


def test_ioc_partial_then_cancel(eng):
    new(eng, 1, S, 101, 3)
    ev = new(eng, 2, B, 101, 10, IOC)
    assert of(ev, Fill)[0].qty == 3
    assert of(ev, Cancelled)[0].reason == "ioc_unfilled"
    assert eng.best_bid() is None


def test_ioc_respects_limit(eng):
    new(eng, 1, S, 105, 3)
    ev = new(eng, 2, B, 101, 10, IOC)
    assert not of(ev, Fill)
    assert of(ev, Cancelled)[0].qty == 10


def test_fok_fills_fully_across_levels(eng):
    new(eng, 1, S, 101, 3)
    new(eng, 2, S, 102, 3)
    ev = new(eng, 3, B, 102, 6, FOK)
    assert sum(f.qty for f in of(ev, Fill)) == 6
    assert not of(ev, Cancelled)


def test_fok_kills_when_insufficient(eng):
    new(eng, 1, S, 101, 3)
    new(eng, 2, S, 103, 3)
    h = eng.book_hash()
    ev = new(eng, 3, B, 102, 4, FOK)
    assert not of(ev, Fill)
    assert of(ev, Cancelled)[0].reason == "fok_unfilled"
    assert eng.book_hash() == h


def test_fok_counts_self_trade_as_unavailable(eng):
    new(eng, 1, S, 101, 3, owner=7)
    new(eng, 2, S, 101, 3, owner=9)
    ev = eng.process(NewOrder(3, 9, B, FOK, 6, 101))
    assert not of(ev, Fill)
    assert of(ev, Cancelled)[0].reason == "fok_unfilled"


def test_post_only_rests_or_rejects(eng):
    new(eng, 1, S, 101, 3)
    ev = new(eng, 2, B, 101, 3, PO)
    assert [type(e) for e in ev] == [Reject]
    assert ev[0].reason == "post_only_would_cross"
    ev = new(eng, 3, B, 100, 3, PO)
    assert of(ev, Booked)


def test_self_trade_prevention_cancels_newest(eng):
    new(eng, 1, S, 101, 2, owner=5)
    new(eng, 2, S, 101, 2, owner=6)
    new(eng, 3, S, 102, 2, owner=6)
    ev = eng.process(NewOrder(4, 6, B, L, 10, 102))
    fills = of(ev, Fill)
    assert [f.maker_id for f in fills] == [1]
    (c,) = of(ev, Cancelled)
    assert (c.order_id, c.qty, c.reason) == (4, 8, "self_trade")
    # resting (older) self orders untouched
    assert eng.order_state(2) == (S, 101, 2)
    assert eng.order_state(3) == (S, 102, 2)
    assert not eng.has_order(4)


def test_cancel_and_unknown_cancel(eng):
    new(eng, 1, B, 100, 5)
    new(eng, 2, B, 100, 5)
    ev = eng.process(Cancel(1))
    assert of(ev, Cancelled)[0].qty == 5
    assert of(ev, BookDelta)[0].qty == 5
    assert eng.queue_ahead(2) == 0
    ev = eng.process(Cancel(1))
    assert ev[0] == Reject(ev[0].seq, 1, "unknown_order")


def test_cancel_last_order_removes_level(eng):
    new(eng, 1, B, 100, 5)
    new(eng, 2, B, 99, 5)
    ev = eng.process(Cancel(1))
    assert of(ev, BookDelta)[0].qty == 0
    assert eng.best_bid() == 99
    eng.check_invariants()


@pytest.mark.parametrize(
    ("oid", "price", "qty", "otype", "reason"),
    [
        (1, 100, 5, L, "duplicate_order_id"),
        (2, 100, 0, L, "bad_qty"),
        (3, 0, 5, L, "bad_price"),
        (4, None, 5, IOC, "bad_price"),
    ],
    ids=["duplicate_id", "zero_qty", "zero_price", "ioc_without_price"],
)
def test_new_order_rejects_bad_input(eng, oid, price, qty, otype, reason):
    new(eng, 1, B, 100, 5)
    h = eng.book_hash()
    ev = new(eng, oid, B, price, qty, otype)
    assert ev == [Reject(ev[0].seq, oid, reason)]
    assert eng.book_hash() == h


def test_replace_size_down_keeps_priority(eng):
    new(eng, 1, B, 100, 10)
    new(eng, 2, B, 100, 10)
    ev = eng.process(Replace(1, 100, 4))
    (r,) = of(ev, Replaced)
    assert r.priority_kept
    assert (r.old_qty, r.new_qty) == (10, 4)
    assert of(ev, BookDelta)[0].qty == 14
    ev = new(eng, 3, S, 100, 4)
    assert of(ev, Fill)[0].maker_id == 1


def test_replace_size_up_loses_priority(eng):
    new(eng, 1, B, 100, 10)
    new(eng, 2, B, 100, 10)
    ev = eng.process(Replace(1, 100, 12))
    assert not of(ev, Replaced)[0].priority_kept
    assert eng.queue_ahead(1) == 10
    ev = new(eng, 3, S, 100, 4)
    assert of(ev, Fill)[0].maker_id == 2


def test_replace_price_change_loses_priority_and_can_match(eng):
    new(eng, 1, B, 100, 10)
    new(eng, 2, B, 99, 10)
    new(eng, 9, S, 102, 3)
    ev = eng.process(Replace(2, 100, 10))
    assert eng.queue_ahead(2) == 10
    ev = eng.process(Replace(1, 102, 10))
    (f,) = of(ev, Fill)
    assert (f.maker_id, f.taker_id, f.qty) == (9, 1, 3)
    assert eng.order_state(1) == (B, 102, 7)
    eng.check_invariants()


def test_replace_post_only_cannot_cross(eng):
    new(eng, 1, B, 100, 10, PO)
    new(eng, 2, S, 102, 10)
    ev = eng.process(Replace(1, 102, 10))
    assert ev[0].reason == "post_only_would_cross"
    assert eng.order_state(1) == (B, 100, 10)


@pytest.mark.parametrize(
    ("oid", "price", "qty", "reason"),
    [(5, 100, 1, "unknown_order"), (1, 100, 0, "bad_qty"), (1, -1, 3, "bad_price")],
    ids=["unknown_order", "zero_qty", "negative_price"],
)
def test_replace_rejects_bad_input(eng, oid, price, qty, reason):
    new(eng, 1, B, 100, 10)
    ev = eng.process(Replace(oid, price, qty))
    assert ev == [Reject(ev[0].seq, oid, reason)]
    assert eng.order_state(1) == (B, 100, 10)


def test_replace_self_trade(eng):
    new(eng, 1, S, 101, 5, owner=3)
    new(eng, 2, B, 100, 5, owner=3)
    ev = eng.process(Replace(2, 101, 5))
    assert of(ev, Cancelled)[0].reason == "self_trade"
    assert not eng.has_order(2)


def test_snapshot(eng):
    new(eng, 1, B, 100, 5)
    new(eng, 2, B, 100, 5)
    new(eng, 3, B, 98, 1)
    new(eng, 4, S, 103, 2)
    (snap,) = eng.process(SnapshotRequest(depth=1))
    assert isinstance(snap, Snapshot)
    assert snap.bids == ((100, 10),)
    assert snap.asks == ((103, 2),)


def test_replay_and_event_rebuild_match():
    eng = Engine(keep_events=True)
    for i, (side, px, q, t) in enumerate(
        [(B, 100, 5, L), (S, 103, 5, L), (B, 103, 7, L), (S, 99, 20, IOC), (B, 98, 4, PO)]
    ):
        new(eng, i + 1, side, px, q, t)
    eng.process(Replace(5, 98, 1))
    eng.process(Cancel(2))
    r = replay(eng.journal)
    assert r.events == eng.events
    assert r.book_hash() == eng.book_hash()
    bb = BookBuilder()
    for e in eng.events:
        bb.apply(e)
    assert bb.book_hash() == eng.book_hash()
