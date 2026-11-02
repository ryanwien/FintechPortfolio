// Unit and randomized tests for the C++ matching engine, ported from the Python
// twin's test suite (matching-engine/tests/test_engine.py and the property tests).
// No third-party code: a minimal hand-rolled CHECK framework. Exit code 0 and
// "OK <N> tests" when every check passes; details and a nonzero exit otherwise.

#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

#include "../src/engine.hpp"
#include "../src/flow.hpp"

using lob::Command;
using lob::Engine;
using lob::Event;
using lob::EventType;
using lob::i64;
using lob::OrdType;
using lob::Side;
using lob::u64;

namespace {

const char* g_test = "";
int g_checks = 0;
int g_failed = 0;

void check_impl(bool ok, const char* expr, int line) {
    ++g_checks;
    if (!ok) {
        ++g_failed;
        std::printf("FAILED [%s] tests.cpp:%d: %s\n", g_test, line, expr);
    }
}

#define CHECK(cond) check_impl(static_cast<bool>(cond), #cond, __LINE__)

#define RUN(fn)        \
    do {               \
        g_test = #fn;  \
        fn();          \
    } while (0)

constexpr Side B = Side::Buy;
constexpr Side S = Side::Sell;
constexpr OrdType L = OrdType::Limit;
constexpr OrdType M = OrdType::Market;
constexpr OrdType IOC = OrdType::Ioc;
constexpr OrdType FOK = OrdType::Fok;
constexpr OrdType PO = OrdType::PostOnly;

// Submit a new order; the owner defaults to the order id (so no self-trades).
std::vector<Event> neworder(Engine& eng, i64 oid, Side side, i64 price, i64 qty,
                            OrdType ot = L, i64 owner = -1) {
    return eng.process(lob::make_new(oid, owner < 0 ? oid : owner, side, ot, qty, price));
}

std::vector<Event> of(const std::vector<Event>& evs, EventType t) {
    std::vector<Event> out;
    for (const Event& e : evs)
        if (e.type == t) out.push_back(e);
    return out;
}

bool reason_is(const Event& e, const char* want) {
    return e.reason != nullptr && std::strcmp(e.reason, want) == 0;
}

// ------------------------------------------------------------------ unit tests

void test_limit_rests_and_acks() {
    Engine eng;
    const auto ev = neworder(eng, 1, B, 100, 10);
    CHECK(ev.size() == 2);
    CHECK(ev[0].type == EventType::Ack);
    CHECK(ev[1].type == EventType::Booked);
    CHECK(ev[1].price == 100 && ev[1].qty == 10 && ev[1].side == B);
    CHECK(eng.best_bid() && *eng.best_bid() == 100);
    CHECK(!eng.best_ask());
}

void test_sequence_numbers_are_gap_free() {
    Engine eng;
    i64 expected = 0;
    bool gap_free = true;
    auto feed = [&](const Command& c) {
        for (const Event& e : eng.process(c))
            if (e.seq != ++expected) gap_free = false;
    };
    feed(lob::make_new(1, 1, B, L, 10, 100));
    feed(lob::make_new(2, 2, S, L, 10, 101));
    feed(lob::make_new(3, 3, B, IOC, 4, 101));
    feed(lob::make_cancel(99));  // Reject still takes a sequence number
    CHECK(gap_free);
    CHECK(eng.seq() == expected);
}

void test_full_and_partial_fill_maker_taker() {
    Engine eng;
    neworder(eng, 1, S, 101, 10);
    const auto ev = neworder(eng, 2, B, 101, 4);
    const auto fills = of(ev, EventType::Fill);
    CHECK(fills.size() == 1);
    const Event& f = fills[0];
    CHECK(f.maker_id == 1 && f.order_id == 2 && f.price == 101 && f.qty == 4 && f.side == B);
    CHECK(f.maker_leaves == 6 && f.taker_leaves == 0);
    const auto st = eng.order_state(1);
    CHECK(st && st->side == S && st->price == 101 && st->qty == 6);
    CHECK(of(ev, EventType::Booked).empty());
}

void test_ack_precedes_fill() {
    Engine eng;
    neworder(eng, 1, S, 101, 5);
    const auto ev = neworder(eng, 2, B, 101, 5);
    CHECK(ev.size() >= 2 && ev[0].type == EventType::Ack && ev[1].type == EventType::Fill);
}

void test_taker_gets_maker_price_and_sweeps_levels() {
    Engine eng;
    neworder(eng, 1, S, 101, 5);
    neworder(eng, 2, S, 102, 5);
    neworder(eng, 3, S, 105, 5);
    const auto ev = neworder(eng, 4, B, 103, 12);
    const auto fills = of(ev, EventType::Fill);
    CHECK(fills.size() == 2);
    CHECK(fills[0].maker_id == 1 && fills[0].price == 101 && fills[0].qty == 5);
    CHECK(fills[1].maker_id == 2 && fills[1].price == 102 && fills[1].qty == 5);
    const auto booked = of(ev, EventType::Booked);
    CHECK(booked.size() == 1 && booked[0].price == 103 && booked[0].qty == 2);
    CHECK(*eng.best_bid() == 103 && *eng.best_ask() == 105);
}

void test_time_priority_within_level() {
    Engine eng;
    neworder(eng, 1, B, 100, 5);
    neworder(eng, 2, B, 100, 5);
    neworder(eng, 3, B, 100, 5);
    const auto ev = neworder(eng, 4, S, 100, 7);
    const auto fills = of(ev, EventType::Fill);
    CHECK(fills.size() == 2);
    CHECK(fills[0].maker_id == 1 && fills[0].qty == 5);
    CHECK(fills[1].maker_id == 2 && fills[1].qty == 2);
    CHECK(eng.queue_ahead(3) == 3);
}

void test_price_priority_beats_time() {
    Engine eng;
    neworder(eng, 1, B, 100, 5);
    neworder(eng, 2, B, 101, 5);
    const auto ev = neworder(eng, 3, S, 99, 5);
    const auto fills = of(ev, EventType::Fill);
    CHECK(fills.size() == 1 && fills[0].maker_id == 2);
}

void test_market_order_sweeps_and_cancels_rest() {
    Engine eng;
    neworder(eng, 1, S, 101, 3);
    neworder(eng, 2, S, 150, 3);
    const auto ev = eng.process(lob::make_new(3, 3, B, M, 10));
    const auto fills = of(ev, EventType::Fill);
    CHECK(fills.size() == 2 && fills[0].price == 101 && fills[1].price == 150);
    const auto canc = of(ev, EventType::Cancelled);
    CHECK(canc.size() == 1 && canc[0].qty == 4 && reason_is(canc[0], "no_liquidity"));
}

void test_market_order_on_empty_book() {
    Engine eng;
    const auto ev = eng.process(lob::make_new(1, 1, S, M, 10));
    CHECK(ev.size() == 2);
    CHECK(ev[0].type == EventType::Ack && ev[1].type == EventType::Cancelled);
    CHECK(reason_is(ev[1], "no_liquidity") && ev[1].qty == 10);
}

void test_ioc_partial_then_cancel() {
    Engine eng;
    neworder(eng, 1, S, 101, 3);
    const auto ev = neworder(eng, 2, B, 101, 10, IOC);
    const auto fills = of(ev, EventType::Fill);
    CHECK(fills.size() == 1 && fills[0].qty == 3);
    const auto canc = of(ev, EventType::Cancelled);
    CHECK(canc.size() == 1 && reason_is(canc[0], "ioc_unfilled") && canc[0].qty == 7);
    CHECK(!eng.best_bid());
}

void test_ioc_respects_limit() {
    Engine eng;
    neworder(eng, 1, S, 105, 3);
    const auto ev = neworder(eng, 2, B, 101, 10, IOC);
    CHECK(of(ev, EventType::Fill).empty());
    const auto canc = of(ev, EventType::Cancelled);
    CHECK(canc.size() == 1 && canc[0].qty == 10);
}

void test_fok_fills_fully_across_levels() {
    Engine eng;
    neworder(eng, 1, S, 101, 3);
    neworder(eng, 2, S, 102, 3);
    const auto ev = neworder(eng, 3, B, 102, 6, FOK);
    i64 filled = 0;
    for (const Event& f : of(ev, EventType::Fill)) filled += f.qty;
    CHECK(filled == 6);
    CHECK(of(ev, EventType::Cancelled).empty());
}

void test_fok_kills_when_insufficient() {
    Engine eng;
    neworder(eng, 1, S, 101, 3);
    neworder(eng, 2, S, 103, 3);
    const u64 h = eng.book_hash();
    const auto ev = neworder(eng, 3, B, 102, 4, FOK);
    CHECK(of(ev, EventType::Fill).empty());
    const auto canc = of(ev, EventType::Cancelled);
    CHECK(canc.size() == 1 && reason_is(canc[0], "fok_unfilled"));
    CHECK(eng.book_hash() == h);  // the kill leaves the book untouched
}

void test_fok_counts_self_trade_as_unavailable() {
    Engine eng;
    neworder(eng, 1, S, 101, 3, L, 7);
    neworder(eng, 2, S, 101, 3, L, 9);
    // Owner 9's buy would have to trade through its own resting sell: the
    // feasibility walk stops there, so the FOK is killed with zero fills.
    const auto ev = eng.process(lob::make_new(3, 9, B, FOK, 6, 101));
    CHECK(of(ev, EventType::Fill).empty());
    const auto canc = of(ev, EventType::Cancelled);
    CHECK(canc.size() == 1 && reason_is(canc[0], "fok_unfilled"));
}

void test_post_only_rests_or_rejects() {
    Engine eng;
    neworder(eng, 1, S, 101, 3);
    const auto ev = neworder(eng, 2, B, 101, 3, PO);
    CHECK(ev.size() == 1 && ev[0].type == EventType::Reject);
    CHECK(reason_is(ev[0], "post_only_would_cross"));
    const auto ev2 = neworder(eng, 3, B, 100, 3, PO);
    CHECK(!of(ev2, EventType::Booked).empty());
}

void test_self_trade_prevention_cancels_newest() {
    Engine eng;
    neworder(eng, 1, S, 101, 2, L, 5);
    neworder(eng, 2, S, 101, 2, L, 6);
    neworder(eng, 3, S, 102, 2, L, 6);
    const auto ev = eng.process(lob::make_new(4, 6, B, L, 10, 102));
    const auto fills = of(ev, EventType::Fill);
    CHECK(fills.size() == 1 && fills[0].maker_id == 1);
    const auto canc = of(ev, EventType::Cancelled);
    CHECK(canc.size() == 1);
    CHECK(canc[0].order_id == 4 && canc[0].qty == 8 && reason_is(canc[0], "self_trade"));
    // The resting (older) self orders are untouched.
    const auto st2 = eng.order_state(2);
    const auto st3 = eng.order_state(3);
    CHECK(st2 && st2->side == S && st2->price == 101 && st2->qty == 2);
    CHECK(st3 && st3->side == S && st3->price == 102 && st3->qty == 2);
    CHECK(!eng.has_order(4));
}

void test_cancel_and_unknown_cancel() {
    Engine eng;
    neworder(eng, 1, B, 100, 5);
    neworder(eng, 2, B, 100, 5);
    const auto ev = eng.process(lob::make_cancel(1));
    const auto canc = of(ev, EventType::Cancelled);
    CHECK(canc.size() == 1 && canc[0].qty == 5 && reason_is(canc[0], "user"));
    CHECK(eng.queue_ahead(2) == 0);
    const auto ev2 = eng.process(lob::make_cancel(1));
    CHECK(ev2.size() == 1 && ev2[0].type == EventType::Reject);
    CHECK(reason_is(ev2[0], "unknown_order"));
}

void test_cancel_last_order_removes_level() {
    Engine eng;
    neworder(eng, 1, B, 100, 5);
    neworder(eng, 2, B, 99, 5);
    eng.process(lob::make_cancel(1));
    CHECK(*eng.best_bid() == 99);
    CHECK(eng.check_invariants());
}

void test_new_order_rejects_bad_input() {
    Engine eng;
    neworder(eng, 1, B, 100, 5);
    const u64 h = eng.book_hash();
    struct Case {
        i64 oid;
        i64 price;
        i64 qty;
        OrdType ot;
        const char* reason;
    };
    const Case cases[] = {
        {1, 100, 5, L, "duplicate_order_id"},
        {2, 100, 0, L, "bad_qty"},
        {3, 0, 5, L, "bad_price"},
        {4, 0, 5, IOC, "bad_price"},  // IOC without a usable price
    };
    for (const Case& c : cases) {
        const auto ev = neworder(eng, c.oid, B, c.price, c.qty, c.ot);
        CHECK(ev.size() == 1 && ev[0].type == EventType::Reject);
        CHECK(ev[0].order_id == c.oid && reason_is(ev[0], c.reason));
    }
    CHECK(eng.book_hash() == h);  // every reject left the book untouched
}

void test_rejected_id_can_be_resubmitted() {
    Engine eng;
    const auto ev = neworder(eng, 1, B, 100, 0);  // bad_qty: rejected before seen
    CHECK(reason_is(ev[0], "bad_qty"));
    const auto ev2 = neworder(eng, 1, B, 100, 5);
    CHECK(!of(ev2, EventType::Booked).empty());
}

void test_replace_size_down_keeps_priority() {
    Engine eng;
    neworder(eng, 1, B, 100, 10);
    neworder(eng, 2, B, 100, 10);
    const auto ev = eng.process(lob::make_replace(1, 100, 4));
    const auto rep = of(ev, EventType::Replaced);
    CHECK(rep.size() == 1 && rep[0].priority_kept);
    CHECK(rep[0].old_qty == 10 && rep[0].qty == 4);
    const auto ev2 = neworder(eng, 3, S, 100, 4);
    const auto fills = of(ev2, EventType::Fill);
    CHECK(fills.size() == 1 && fills[0].maker_id == 1);  // still first in queue
}

void test_replace_size_up_loses_priority() {
    Engine eng;
    neworder(eng, 1, B, 100, 10);
    neworder(eng, 2, B, 100, 10);
    const auto ev = eng.process(lob::make_replace(1, 100, 12));
    const auto rep = of(ev, EventType::Replaced);
    CHECK(rep.size() == 1 && !rep[0].priority_kept);
    CHECK(eng.queue_ahead(1) == 10);
    const auto ev2 = neworder(eng, 3, S, 100, 4);
    const auto fills = of(ev2, EventType::Fill);
    CHECK(fills.size() == 1 && fills[0].maker_id == 2);  // 1 went to the back
}

void test_replace_price_change_loses_priority_and_can_match() {
    Engine eng;
    neworder(eng, 1, B, 100, 10);
    neworder(eng, 2, B, 99, 10);
    neworder(eng, 9, S, 102, 3);
    eng.process(lob::make_replace(2, 100, 10));
    CHECK(eng.queue_ahead(2) == 10);
    const auto ev = eng.process(lob::make_replace(1, 102, 10));
    const auto fills = of(ev, EventType::Fill);
    CHECK(fills.size() == 1);
    CHECK(fills[0].maker_id == 9 && fills[0].order_id == 1 && fills[0].qty == 3);
    const auto st = eng.order_state(1);
    CHECK(st && st->side == B && st->price == 102 && st->qty == 7);
    CHECK(eng.check_invariants());
}

void test_replace_post_only_cannot_cross() {
    Engine eng;
    neworder(eng, 1, B, 100, 10, PO);
    neworder(eng, 2, S, 102, 10);
    const auto ev = eng.process(lob::make_replace(1, 102, 10));
    CHECK(ev.size() == 1 && reason_is(ev[0], "post_only_would_cross"));
    const auto st = eng.order_state(1);
    CHECK(st && st->price == 100 && st->qty == 10);  // untouched
}

void test_replace_rejects_bad_input() {
    Engine eng;
    neworder(eng, 1, B, 100, 10);
    struct Case {
        i64 oid;
        i64 price;
        i64 qty;
        const char* reason;
    };
    const Case cases[] = {
        {5, 100, 1, "unknown_order"},
        {1, 100, 0, "bad_qty"},
        {1, -1, 3, "bad_price"},
    };
    for (const Case& c : cases) {
        const auto ev = eng.process(lob::make_replace(c.oid, c.price, c.qty));
        CHECK(ev.size() == 1 && ev[0].type == EventType::Reject);
        CHECK(ev[0].order_id == c.oid && reason_is(ev[0], c.reason));
    }
    const auto st = eng.order_state(1);
    CHECK(st && st->price == 100 && st->qty == 10);
}

void test_replace_self_trade() {
    Engine eng;
    neworder(eng, 1, S, 101, 5, L, 3);
    neworder(eng, 2, B, 100, 5, L, 3);
    const auto ev = eng.process(lob::make_replace(2, 101, 5));
    const auto canc = of(ev, EventType::Cancelled);
    CHECK(canc.size() == 1 && reason_is(canc[0], "self_trade"));
    CHECK(!eng.has_order(2));
    const auto st = eng.order_state(1);
    CHECK(st && st->qty == 5);  // the resting self order is untouched
}

void test_depth_stats_and_queue() {
    Engine eng;
    neworder(eng, 1, B, 100, 5);
    neworder(eng, 2, B, 100, 5);
    neworder(eng, 3, B, 98, 1);
    neworder(eng, 4, S, 103, 2);
    const auto d = eng.depth_stats();
    CHECK(d.bid_levels == 2 && d.ask_levels == 1);
    CHECK(d.resting_orders == 4 && d.resting_qty == 13);
    CHECK(eng.queue_ahead(2) == 5);
}

// ------------------------------------------------------------- randomized tests

// Hash a full event stream (every field that defines behaviour) with FNV-1a, so
// two runs can be compared without storing both streams.
u64 event_stream_hash(const std::vector<Event>& evs, u64 h) {
    auto mix = [&h](u64 v) {
        for (int i = 0; i < 8; ++i) {
            h ^= (v >> (i * 8)) & 0xFFu;
            h *= 1099511628211ull;
        }
    };
    for (const Event& e : evs) {
        mix(static_cast<u64>(e.type));
        mix(static_cast<u64>(e.seq));
        mix(static_cast<u64>(e.order_id));
        mix(static_cast<u64>(e.maker_id));
        mix(static_cast<u64>(e.price));
        mix(static_cast<u64>(e.qty));
        mix(static_cast<u64>(e.maker_leaves));
        mix(static_cast<u64>(e.taker_leaves));
        mix(static_cast<u64>(e.old_price));
        mix(static_cast<u64>(e.old_qty));
        mix(e.priority_kept ? 1u : 0u);
        if (e.reason != nullptr)
            for (const char* p = e.reason; *p != '\0'; ++p) mix(static_cast<u64>(*p));
    }
    return h;
}

void test_replay_determinism_and_identical_events() {
    const auto flow = lob::synthetic_flow(20'000, 42);
    Engine a;
    Engine b;
    u64 ha = 14695981039346656037ull;
    u64 hb = ha;
    for (const Command& c : flow) ha = event_stream_hash(a.process(c), ha);
    for (const Command& c : flow) hb = event_stream_hash(b.process(c), hb);
    CHECK(a.book_hash() == b.book_hash());  // same command log -> same book
    CHECK(ha == hb);                        // ... and the exact same event stream
    // The hash is not vacuous: an extra resting order changes it.
    const u64 before = b.book_hash();
    b.process(lob::make_new(9'999'999, 1, B, L, 100, 1));
    CHECK(b.book_hash() != before);
    CHECK(a.check_invariants());
}

void test_random_flow_quantity_conservation() {
    // Identity over the whole run, counting both sides of each fill:
    //   acked qty + replace deltas = 2 * filled + cancelled + resting
    // Rejects contribute nothing. Checked on the full mixed flow.
    const auto flow = lob::synthetic_flow(50'000, 7);
    Engine eng;
    i64 submitted = 0;
    i64 filled2 = 0;  // both sides of each fill
    i64 cancelled = 0;
    i64 expected_seq = 0;
    bool seq_ok = true;
    for (const Command& c : flow) {
        for (const Event& e : eng.process(c)) {
            if (e.seq != ++expected_seq) seq_ok = false;
            switch (e.type) {
                case EventType::Ack: submitted += e.qty; break;
                case EventType::Fill: filled2 += 2 * e.qty; break;
                case EventType::Cancelled: cancelled += e.qty; break;
                case EventType::Replaced: submitted += e.qty - e.old_qty; break;
                case EventType::Reject: break;
                case EventType::Booked: break;
            }
        }
    }
    const i64 resting = eng.depth_stats().resting_qty;
    CHECK(submitted == filled2 + cancelled + resting);
    CHECK(seq_ok);
    CHECK(eng.check_invariants());
}

void test_random_flow_never_crossed_and_invariants() {
    // The book must be uncrossed after every single command, and structurally
    // consistent at regular intervals (the full check walks every order).
    const auto flow = lob::synthetic_flow(5'000, 11);
    Engine eng;
    bool never_crossed = true;
    bool invariants_ok = true;
    std::size_t i = 0;
    for (const Command& c : flow) {
        eng.process(c);
        const auto bb = eng.best_bid();
        const auto ba = eng.best_ask();
        if (bb && ba && *bb >= *ba) never_crossed = false;
        if (++i % 250 == 0 && !eng.check_invariants()) invariants_ok = false;
    }
    CHECK(never_crossed);
    CHECK(invariants_ok);
    CHECK(eng.check_invariants());
}

void test_fills_never_trade_through_takers_limit() {
    // Every fill happens at the maker's price, on the best opposite level, and
    // never through the taker's limit.
    const auto flow = lob::synthetic_flow(20'000, 23);
    Engine eng;
    bool price_ok = true;
    for (const Command& c : flow) {
        const bool is_new = c.type == lob::CmdType::New;
        const bool limited = is_new && c.otype != OrdType::Market;
        for (const Event& e : eng.process(c)) {
            if (e.type != EventType::Fill) continue;
            if (limited && e.order_id == c.order_id) {
                if (e.side == B && e.price > c.price) price_ok = false;
                if (e.side == S && e.price < c.price) price_ok = false;
            }
        }
    }
    CHECK(price_ok);
    CHECK(eng.check_invariants());
}

}  // namespace

int main() {
    RUN(test_limit_rests_and_acks);
    RUN(test_sequence_numbers_are_gap_free);
    RUN(test_full_and_partial_fill_maker_taker);
    RUN(test_ack_precedes_fill);
    RUN(test_taker_gets_maker_price_and_sweeps_levels);
    RUN(test_time_priority_within_level);
    RUN(test_price_priority_beats_time);
    RUN(test_market_order_sweeps_and_cancels_rest);
    RUN(test_market_order_on_empty_book);
    RUN(test_ioc_partial_then_cancel);
    RUN(test_ioc_respects_limit);
    RUN(test_fok_fills_fully_across_levels);
    RUN(test_fok_kills_when_insufficient);
    RUN(test_fok_counts_self_trade_as_unavailable);
    RUN(test_post_only_rests_or_rejects);
    RUN(test_self_trade_prevention_cancels_newest);
    RUN(test_cancel_and_unknown_cancel);
    RUN(test_cancel_last_order_removes_level);
    RUN(test_new_order_rejects_bad_input);
    RUN(test_rejected_id_can_be_resubmitted);
    RUN(test_replace_size_down_keeps_priority);
    RUN(test_replace_size_up_loses_priority);
    RUN(test_replace_price_change_loses_priority_and_can_match);
    RUN(test_replace_post_only_cannot_cross);
    RUN(test_replace_rejects_bad_input);
    RUN(test_replace_self_trade);
    RUN(test_depth_stats_and_queue);
    RUN(test_replay_determinism_and_identical_events);
    RUN(test_random_flow_quantity_conservation);
    RUN(test_random_flow_never_crossed_and_invariants);
    RUN(test_fills_never_trade_through_takers_limit);

    if (g_failed > 0) {
        std::printf("FAILED %d of %d checks\n", g_failed, g_checks);
        return 1;
    }
    std::printf("OK %d tests\n", g_checks);
    return 0;
}
