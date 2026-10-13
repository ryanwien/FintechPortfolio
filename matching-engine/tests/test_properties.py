"""Hypothesis property tests: random command streams against invariants."""

from __future__ import annotations

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

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
    Replace,
    Replaced,
    Side,
    replay,
)

raw_cmd = st.tuples(
    st.sampled_from(["new"] * 6 + ["cancel", "replace"]),
    st.sampled_from([Side.BUY, Side.SELL]),
    st.sampled_from(list(OrdType)),
    st.integers(95, 105),  # narrow band => lots of crossing
    st.integers(1, 20),
    st.integers(1, 3),  # owner: few owners => self-trade prevention gets exercised
    st.integers(0, 60),  # reference to an earlier order id
)

SETTINGS = settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])


def to_commands(raws):
    """Turn drawn tuples into commands with sequential new-order ids."""
    cmds, next_id = [], 1
    for kind, side, otype, price, qty, owner, ref in raws:
        if kind == "new":
            cmds.append(NewOrder(next_id, owner, side, otype, qty, price))
            next_id += 1
        elif kind == "cancel":
            cmds.append(Cancel(ref))
        else:
            cmds.append(Replace(ref, price, qty))
    return cmds


@SETTINGS
@given(st.lists(raw_cmd, max_size=120))
def test_book_never_crossed_and_structure_sound(raws):
    eng = Engine()
    for cmd in to_commands(raws):
        eng.process(cmd)
        bb, ba = eng.best_bid(), eng.best_ask()
        assert bb is None or ba is None or bb < ba
    eng.check_invariants()


@SETTINGS
@given(st.lists(raw_cmd, max_size=120))
def test_quantity_conserved(raws):
    """submitted = filled + resting + cancelled, counted per order side of each trade."""
    eng = Engine()
    submitted = filled = cancelled = 0
    for cmd in to_commands(raws):
        for e in eng.process(cmd):
            if isinstance(e, Ack):
                submitted += e.qty
            elif isinstance(e, Fill):
                filled += 2 * e.qty  # maker and taker each fill e.qty
            elif isinstance(e, Cancelled):
                cancelled += e.qty
            elif isinstance(e, Replaced):
                submitted += max(0, e.new_qty - e.old_qty)
                cancelled += max(0, e.old_qty - e.new_qty)
        resting = eng.depth_stats()["resting_qty"]
        assert submitted == filled + resting + cancelled


@SETTINGS
@given(st.lists(raw_cmd, max_size=120))
def test_price_time_priority(raws):
    """Every fill hits the head of the best opposite level, at that level's price,
    and never trades through the taker's limit."""
    eng = Engine()
    shadow = BookBuilder()
    limits: dict[int, int | None] = {}
    for cmd in to_commands(raws):
        for e in eng.process(cmd):
            if isinstance(e, Ack):
                limits[e.order_id] = e.price
            elif isinstance(e, Replaced):
                limits[e.order_id] = e.new_price
            elif isinstance(e, Fill):
                maker_side = e.aggressor.opposite
                assert shadow.head(maker_side) == e.maker_id
                assert shadow.best(maker_side) == e.price
                lim = limits[e.taker_id]
                if lim is not None:
                    assert e.price <= lim if e.aggressor is Side.BUY else e.price >= lim
                assert e.maker_owner != e.taker_owner
            shadow.apply(e)


@SETTINGS
@given(st.lists(raw_cmd, max_size=120))
def test_replay_determinism_and_event_sourcing(raws):
    eng = Engine(keep_events=True)
    for cmd in to_commands(raws):
        eng.process(cmd)
    again = replay(eng.journal)
    assert again.events == eng.events
    assert again.book_hash() == eng.book_hash()
    # The output stream alone is enough to rebuild the exact L3 book.
    bb = BookBuilder()
    for e in eng.events:
        bb.apply(e)
    assert bb.book_hash() == eng.book_hash()
    seqs = [e.seq for e in eng.events]
    assert seqs == list(range(1, len(seqs) + 1))


@SETTINGS
@given(st.lists(raw_cmd, max_size=120))
def test_l2_increments_reproduce_book(raws):
    eng = Engine()
    l2: tuple[dict[int, int], dict[int, int]] = ({}, {})
    for cmd in to_commands(raws):
        for e in eng.process(cmd):
            if isinstance(e, BookDelta):
                if e.qty:
                    l2[e.side][e.price] = e.qty
                else:
                    l2[e.side].pop(e.price, None)
            elif isinstance(e, Booked):
                assert e.qty > 0
        bids, asks = eng.l2()
        assert dict(bids) == l2[Side.BUY]
        assert dict(asks) == l2[Side.SELL]
