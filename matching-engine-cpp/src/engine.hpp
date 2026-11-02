// Price-time priority limit order book and matching engine (C++20, header-only).
//
// This is the C++ twin of matching-engine/src/exchange/engine.py and mirrors its
// semantics exactly where they are defined there:
//   * integer ticks and integer shares everywhere, no floats in the engine
//   * order types: limit, market, IOC, FOK, post-only; cancel; cancel/replace
//   * replace: same price and size decrease keeps queue priority; a price change or
//     size increase loses it and the order re-enters as a taker
//   * self-trade prevention cancels the newest (incoming) order's remainder
//   * FOK proves the full quantity is fillable before touching the book, and the
//     feasibility walk stops at the first own resting order, so a FOK is killed
//     rather than partially filled into a self-trade
//   * every event carries a global, gap-free sequence number
//
// Data structure: each side keeps std::map<key, Level> with key = price * sign
// (bids +price, asks -price), so ascending key order is worst-to-best on both sides
// and the top of book is std::prev(end()). Each Level is an intrusive doubly linked
// FIFO of Order nodes (O(1) unlink), and an unordered_map order-id index finds any
// resting order in O(1). Order nodes come from a deque arena with a free list, so
// the book never allocates per fill once warm.
//
// Determinism: feeding the same command log to a fresh engine reproduces an
// identical book, checked via book_hash(), an FNV-1a hash of the full L3 state
// (side, price, order id, owner, open qty; best level first, FIFO within a level).

#pragma once

#include <cstddef>
#include <cstdint>
#include <deque>
#include <iterator>
#include <map>
#include <optional>
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <vector>

namespace lob {

using i64 = std::int64_t;
using u64 = std::uint64_t;

enum class Side : int { Buy = 0, Sell = 1 };

inline Side opposite(Side s) { return s == Side::Buy ? Side::Sell : Side::Buy; }
inline i64 sign_of(Side s) { return s == Side::Buy ? 1 : -1; }

enum class OrdType : int {
    Limit = 0,    // rests whatever does not match
    Market = 1,   // no price; sweeps the book, remainder cancelled
    Ioc = 2,      // limit price; immediate-or-cancel
    Fok = 3,      // limit price; fill completely right now or not at all
    PostOnly = 4  // limit price; rejected if it would take liquidity
};

inline const char* ordtype_name(OrdType t) {
    switch (t) {
        case OrdType::Limit: return "LIMIT";
        case OrdType::Market: return "MARKET";
        case OrdType::Ioc: return "IOC";
        case OrdType::Fok: return "FOK";
        case OrdType::PostOnly: return "POST_ONLY";
    }
    return "?";
}

enum class CmdType : int { New = 0, CancelCmd = 1, ReplaceCmd = 2 };

// One flat command record. For CancelCmd only order_id matters; for ReplaceCmd
// order_id, price and qty matter (qty is the new open quantity).
struct Command {
    CmdType type = CmdType::New;
    i64 order_id = 0;
    i64 owner = 0;
    Side side = Side::Buy;
    OrdType otype = OrdType::Limit;
    i64 qty = 0;
    i64 price = 0;  // ignored for Market new orders
};

inline Command make_new(i64 oid, i64 owner, Side side, OrdType t, i64 qty, i64 price = 0) {
    return Command{CmdType::New, oid, owner, side, t, qty, price};
}
inline Command make_cancel(i64 oid) {
    return Command{CmdType::CancelCmd, oid, 0, Side::Buy, OrdType::Limit, 0, 0};
}
inline Command make_replace(i64 oid, i64 price, i64 qty) {
    return Command{CmdType::ReplaceCmd, oid, 0, Side::Buy, OrdType::Limit, qty, price};
}

enum class EventType : int { Ack = 0, Reject, Fill, Booked, Cancelled, Replaced };
inline constexpr int kEventTypes = 6;

inline const char* event_name(EventType t) {
    switch (t) {
        case EventType::Ack: return "Ack";
        case EventType::Reject: return "Reject";
        case EventType::Fill: return "Fill";
        case EventType::Booked: return "Booked";
        case EventType::Cancelled: return "Cancelled";
        case EventType::Replaced: return "Replaced";
    }
    return "?";
}

// One flat event record; which fields are meaningful depends on `type`.
//   Ack:       order_id, owner, side, otype, price (0 for market), qty
//   Reject:    order_id, reason
//   Fill:      order_id = taker_id, maker_id, owner = taker_owner, maker_owner,
//              side = aggressor, price (maker's resting price), qty,
//              maker_leaves, taker_leaves
//   Booked:    order_id, side, price, qty
//   Cancelled: order_id, qty (removed), reason
//   Replaced:  order_id, old_price, old_qty, price (new), qty (new), priority_kept
// `reason` always points at a string literal, so comparing pointers is safe
// within one binary.
struct Event {
    EventType type = EventType::Ack;
    i64 seq = 0;
    i64 order_id = 0;
    i64 maker_id = 0;
    i64 owner = 0;
    i64 maker_owner = 0;
    Side side = Side::Buy;
    OrdType otype = OrdType::Limit;
    i64 price = 0;
    i64 qty = 0;
    i64 maker_leaves = 0;
    i64 taker_leaves = 0;
    i64 old_price = 0;
    i64 old_qty = 0;
    bool priority_kept = false;
    const char* reason = nullptr;
};

struct DepthStats {
    i64 bid_levels = 0;
    i64 ask_levels = 0;
    i64 resting_orders = 0;
    i64 resting_qty = 0;
};

class Engine {
public:
    struct OrderState {
        Side side;
        i64 price;
        i64 qty;
    };

    Engine() { events_.reserve(32); }

    // Apply one command; the returned events are valid until the next process() call.
    const std::vector<Event>& process(const Command& c) {
        events_.clear();
        switch (c.type) {
            case CmdType::New: do_new(c); break;
            case CmdType::CancelCmd: do_cancel(c); break;
            case CmdType::ReplaceCmd: do_replace(c); break;
        }
        return events_;
    }

    i64 seq() const { return seq_; }

    std::optional<i64> best_bid() const { return best(Side::Buy); }
    std::optional<i64> best_ask() const { return best(Side::Sell); }

    bool has_order(i64 oid) const { return index_.find(oid) != index_.end(); }

    std::optional<OrderState> order_state(i64 oid) const {
        auto it = index_.find(oid);
        if (it == index_.end()) return std::nullopt;
        const Order* o = it->second;
        return OrderState{o->side, o->price, o->qty};
    }

    // Shares resting ahead of `oid` at its price level (its queue position).
    i64 queue_ahead(i64 oid) const {
        const Order* o = index_.at(oid);
        i64 ahead = 0;
        for (const Order* p = o->prev; p != nullptr; p = p->prev) ahead += p->qty;
        return ahead;
    }

    DepthStats depth_stats() const {
        DepthStats d;
        d.bid_levels = static_cast<i64>(levels_[0].size());
        d.ask_levels = static_cast<i64>(levels_[1].size());
        d.resting_orders = static_cast<i64>(index_.size());
        for (const auto& m : levels_)
            for (const auto& kv : m) d.resting_qty += kv.second.qty;
        return d;
    }

    // FNV-1a (64-bit) over the canonical L3 book: bids then asks, best level first,
    // FIFO within a level, hashing (side, price) then (oid, owner, qty) per order
    // with a level separator. Identical books hash identically by construction.
    u64 book_hash() const {
        u64 h = 14695981039346656037ull;
        auto mix = [&h](u64 v) {
            for (int i = 0; i < 8; ++i) {
                h ^= (v >> (i * 8)) & 0xFFu;
                h *= 1099511628211ull;
            }
        };
        for (int s = 0; s < 2; ++s) {
            const i64 sg = (s == 0) ? 1 : -1;
            const auto& m = levels_[static_cast<std::size_t>(s)];
            for (auto it = m.rbegin(); it != m.rend(); ++it) {
                mix(static_cast<u64>(s));
                mix(static_cast<u64>(it->first * sg));
                for (const Order* o = it->second.head; o != nullptr; o = o->next) {
                    mix(static_cast<u64>(o->oid));
                    mix(static_cast<u64>(o->owner));
                    mix(static_cast<u64>(o->qty));
                }
                mix(~0ull);
            }
        }
        return h;
    }

    // Expensive structural self-check used by tests. Returns false on any
    // bookkeeping inconsistency (crossed book, level totals, index mismatch).
    bool check_invariants() const {
        const auto bb = best_bid();
        const auto ba = best_ask();
        if (bb && ba && *bb >= *ba) return false;
        std::size_t n = 0;
        for (int s = 0; s < 2; ++s) {
            const i64 sg = (s == 0) ? 1 : -1;
            for (const auto& kv : levels_[static_cast<std::size_t>(s)]) {
                const Level& lvl = kv.second;
                i64 q = 0;
                i64 cnt = 0;
                for (const Order* o = lvl.head; o != nullptr; o = o->next) {
                    if (o->qty <= 0) return false;
                    if (o->price != kv.first * sg) return false;
                    if (static_cast<int>(o->side) != s) return false;
                    auto it = index_.find(o->oid);
                    if (it == index_.end() || it->second != o) return false;
                    q += o->qty;
                    ++cnt;
                    ++n;
                }
                if (cnt == 0 || q != lvl.qty || cnt != lvl.count) return false;
            }
        }
        return n == index_.size();
    }

private:
    struct Level;

    struct Order {
        i64 oid = 0;
        i64 owner = 0;
        i64 price = 0;
        i64 qty = 0;
        Side side = Side::Buy;
        OrdType otype = OrdType::Limit;
        Order* prev = nullptr;
        Order* next = nullptr;
        Level* level = nullptr;
    };

    // All orders resting at one price, in time priority, plus their aggregate qty.
    struct Level {
        i64 price = 0;
        i64 qty = 0;
        i64 count = 0;
        Order* head = nullptr;
        Order* tail = nullptr;

        void append(Order* o) {
            o->level = this;
            o->next = nullptr;
            o->prev = tail;
            if (tail == nullptr)
                head = o;
            else
                tail->next = o;
            tail = o;
            qty += o->qty;
            ++count;
        }

        // Detach in O(1); the caller removes an emptied level and owns the index.
        void unlink(Order* o) {
            if (o->prev == nullptr)
                head = o->next;
            else
                o->prev->next = o->next;
            if (o->next == nullptr)
                tail = o->prev;
            else
                o->next->prev = o->prev;
            qty -= o->qty;
            --count;
            o->prev = o->next = nullptr;
            o->level = nullptr;
        }
    };

    static std::size_t idx(Side s) { return static_cast<std::size_t>(static_cast<int>(s)); }

    std::optional<i64> best(Side s) const {
        const auto& m = levels_[idx(s)];
        if (m.empty()) return std::nullopt;
        return std::prev(m.end())->first * sign_of(s);
    }

    // Would a `side` order at `price` take liquidity right now?
    bool crosses(Side side, i64 price) const {
        if (side == Side::Buy) {
            const auto a = best(Side::Sell);
            return a.has_value() && price >= *a;
        }
        const auto b = best(Side::Buy);
        return b.has_value() && price <= *b;
    }

    Order* alloc() {
        if (!free_.empty()) {
            Order* o = free_.back();
            free_.pop_back();
            *o = Order{};
            return o;
        }
        arena_.emplace_back();
        return &arena_.back();
    }

    // ---- event emission (every event takes the next sequence number) ----
    void emit_reject(i64 oid, const char* reason) {
        Event e;
        e.type = EventType::Reject;
        e.seq = ++seq_;
        e.order_id = oid;
        e.reason = reason;
        events_.push_back(e);
    }
    void emit_ack(const Command& c, i64 price) {
        Event e;
        e.type = EventType::Ack;
        e.seq = ++seq_;
        e.order_id = c.order_id;
        e.owner = c.owner;
        e.side = c.side;
        e.otype = c.otype;
        e.price = price;  // 0 for a market order (the Python twin uses None)
        e.qty = c.qty;
        events_.push_back(e);
    }
    void emit_fill(i64 taker, i64 maker, i64 towner, i64 mowner, Side aggressor, i64 price,
                   i64 qty, i64 maker_leaves, i64 taker_leaves) {
        Event e;
        e.type = EventType::Fill;
        e.seq = ++seq_;
        e.order_id = taker;
        e.maker_id = maker;
        e.owner = towner;
        e.maker_owner = mowner;
        e.side = aggressor;
        e.price = price;
        e.qty = qty;
        e.maker_leaves = maker_leaves;
        e.taker_leaves = taker_leaves;
        events_.push_back(e);
    }
    void emit_booked(i64 oid, Side side, i64 price, i64 qty) {
        Event e;
        e.type = EventType::Booked;
        e.seq = ++seq_;
        e.order_id = oid;
        e.side = side;
        e.price = price;
        e.qty = qty;
        events_.push_back(e);
    }
    void emit_cancelled(i64 oid, i64 qty, const char* reason) {
        Event e;
        e.type = EventType::Cancelled;
        e.seq = ++seq_;
        e.order_id = oid;
        e.qty = qty;
        e.reason = reason;
        events_.push_back(e);
    }
    void emit_replaced(i64 oid, i64 old_price, i64 old_qty, i64 new_price, i64 new_qty,
                       bool kept) {
        Event e;
        e.type = EventType::Replaced;
        e.seq = ++seq_;
        e.order_id = oid;
        e.old_price = old_price;
        e.old_qty = old_qty;
        e.price = new_price;
        e.qty = new_qty;
        e.priority_kept = kept;
        events_.push_back(e);
    }

    // ---- command handlers ----
    // Checks run in a fixed order and each reject returns before any state changes.
    // Ids are never reused: seen_ is not cleared when an order fills or cancels.
    void do_new(const Command& c) {
        const bool is_market = c.otype == OrdType::Market;
        const i64 price = is_market ? 0 : c.price;
        if (seen_.find(c.order_id) != seen_.end())
            return emit_reject(c.order_id, "duplicate_order_id");
        if (c.qty <= 0) return emit_reject(c.order_id, "bad_qty");
        if (!is_market && price <= 0) return emit_reject(c.order_id, "bad_price");
        if (c.otype == OrdType::PostOnly && crosses(c.side, price))
            return emit_reject(c.order_id, "post_only_would_cross");
        seen_.insert(c.order_id);
        emit_ack(c, price);  // the Ack precedes any fill

        // FOK is all or nothing: prove the full quantity is fillable first.
        if (c.otype == OrdType::Fok && available(c.side, price, c.owner, c.qty) < c.qty)
            return emit_cancelled(c.order_id, c.qty, "fok_unfilled");

        // STP cancels the incoming (newest) order's remainder, never the resting one.
        const auto [remaining, stp] =
            match(c.order_id, c.owner, c.side, is_market ? std::optional<i64>{} : price, c.qty);
        if (remaining == 0) return;
        if (stp) return emit_cancelled(c.order_id, remaining, "self_trade");
        if (c.otype == OrdType::Limit || c.otype == OrdType::PostOnly)
            return rest(c.order_id, c.owner, c.side, price, remaining, c.otype);
        // A FOK never gets here with a remainder: available() mirrors match()'s stop
        // rule, so a promised full fill is always delivered.
        emit_cancelled(c.order_id, remaining, is_market ? "no_liquidity" : "ioc_unfilled");
    }

    void do_cancel(const Command& c) {
        auto it = index_.find(c.order_id);
        if (it == index_.end()) return emit_reject(c.order_id, "unknown_order");
        Order* o = it->second;
        const i64 qty = o->qty;  // report what was actually removed
        remove(o);
        emit_cancelled(c.order_id, qty, "user");
    }

    void do_replace(const Command& c) {
        auto it = index_.find(c.order_id);
        if (it == index_.end()) return emit_reject(c.order_id, "unknown_order");
        Order* o = it->second;
        if (c.qty <= 0) return emit_reject(c.order_id, "bad_qty");
        if (c.price <= 0) return emit_reject(c.order_id, "bad_price");
        const i64 old_price = o->price;
        const i64 old_qty = o->qty;
        // Same price and a size decrease (or no-op) keeps time priority: the node
        // stays where it is and only the level total shrinks.
        if (c.price == old_price && c.qty <= old_qty) {
            o->level->qty -= old_qty - c.qty;
            o->qty = c.qty;
            return emit_replaced(o->oid, old_price, old_qty, c.price, c.qty, true);
        }
        // A repriced post-only order must not cross. A same-price size change cannot
        // cross because the order already rests on an uncrossed book.
        if (o->otype == OrdType::PostOnly && c.price != old_price && crosses(o->side, c.price))
            return emit_reject(o->oid, "post_only_would_cross");
        // Price change or size increase loses priority: pull it, then re-enter as a
        // taker, so a repriced order can trade at its new price.
        const i64 oid = o->oid;
        const i64 owner = o->owner;
        const Side side = o->side;
        const OrdType ot = o->otype;
        remove(o);
        emit_replaced(oid, old_price, old_qty, c.price, c.qty, false);
        const auto [remaining, stp] = match(oid, owner, side, c.price, c.qty);
        if (remaining == 0) return;
        if (stp) return emit_cancelled(oid, remaining, "self_trade");
        rest(oid, owner, side, c.price, remaining, ot);
    }

    // Book the remainder at the back of its price level (that is what gives it
    // time priority) and index it.
    void rest(i64 oid, i64 owner, Side side, i64 price, i64 qty, OrdType ot) {
        Order* o = alloc();
        o->oid = oid;
        o->owner = owner;
        o->side = side;
        o->price = price;
        o->qty = qty;
        o->otype = ot;
        auto& m = levels_[idx(side)];
        const i64 key = price * sign_of(side);
        auto [it, inserted] = m.try_emplace(key);
        if (inserted) it->second.price = price;
        it->second.append(o);
        index_.emplace(oid, o);
        emit_booked(oid, side, price, qty);
    }

    // Unlink a resting order from its level and the index (emits no event).
    void remove(Order* o) {
        Level* lvl = o->level;
        lvl->unlink(o);
        if (lvl->count == 0) levels_[idx(o->side)].erase(o->price * sign_of(o->side));
        index_.erase(o->oid);
        free_.push_back(o);
    }

    // Quantity a taker could fill right now, stopping exactly where self-trade
    // prevention would, so a FOK is never promised quantity matching will not give.
    i64 available(Side side, i64 limit, i64 owner, i64 need) const {
        const Side opp = opposite(side);
        const i64 osign = sign_of(opp);
        const auto& m = levels_[idx(opp)];
        i64 total = 0;
        for (auto it = m.rbegin(); it != m.rend(); ++it) {
            const i64 price = it->first * osign;
            if (side == Side::Buy ? price > limit : price < limit) break;
            for (const Order* o = it->second.head; o != nullptr; o = o->next) {
                if (o->owner == owner) return total;
                total += o->qty;
                if (total >= need) return total;
            }
        }
        return total;
    }

    // Match an incoming taker against the opposite side, best level first, FIFO
    // within a level. Returns (unfilled qty, stopped_by_self_trade). Market orders
    // pass an empty limit and never stop on price.
    std::pair<i64, bool> match(i64 oid, i64 owner, Side side, std::optional<i64> limit, i64 qty) {
        auto& m = levels_[idx(opposite(side))];
        const i64 osign = sign_of(opposite(side));
        const bool is_buy = side == Side::Buy;
        i64 remaining = qty;
        while (remaining > 0 && !m.empty()) {
            auto it = std::prev(m.end());  // best opposite level
            const i64 best_px = it->first * osign;
            if (limit && (is_buy ? best_px > *limit : best_px < *limit)) break;
            Level& lvl = it->second;
            Order* o = lvl.head;
            bool stp = false;
            while (o != nullptr && remaining > 0) {
                // Reaching our own resting order ends the sweep (cancel-newest STP).
                if (o->owner == owner) {
                    stp = true;
                    break;
                }
                // A fill is the smaller of the maker's open qty and the taker's
                // remainder; the price is always the maker's resting price.
                const i64 q = o->qty < remaining ? o->qty : remaining;
                o->qty -= q;
                lvl.qty -= q;
                remaining -= q;
                emit_fill(oid, o->oid, owner, o->owner, side, best_px, q, o->qty, remaining);
                Order* nxt = o->next;  // read before a possible unlink
                if (o->qty == 0) {
                    lvl.unlink(o);
                    index_.erase(o->oid);
                    free_.push_back(o);
                }
                o = nxt;
            }
            if (lvl.count == 0) m.erase(it);
            if (stp) return {remaining, true};
        }
        return {remaining, false};
    }

    std::map<i64, Level> levels_[2];  // key = price * sign; best level at the end
    std::unordered_map<i64, Order*> index_;
    std::unordered_set<i64> seen_;
    std::deque<Order> arena_;
    std::vector<Order*> free_;
    std::vector<Event> events_;
    i64 seq_ = 0;
};

}  // namespace lob
