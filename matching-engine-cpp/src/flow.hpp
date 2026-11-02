// Deterministic synthetic order flow, the C++ twin of exchange/bench.py.
//
// The mix proportions, flow shape and depth bookkeeping are copied from the Python
// benchmark: ~44% passive limit adds, 4% post-only, 38% cancels of a live resting
// order, 3% cancel/replace, 7% marketable limit, 2% IOC, 1.5% market, 0.5% FOK,
// generated around a random-walk mid. A shadow engine tracks which orders are live
// while generating, so cancels and replaces target orders that actually rest.
// Generation is not timed by the benchmark.
//
// The RNG is splitmix64 (seeded, no global state). The flow is deterministic for a
// given seed on a given platform; the mid path uses libm (log, sqrt, cos), so
// bit-identical flows across different C runtimes are not guaranteed, and the
// replay-determinism check always compares two runs of the same binary.

#pragma once

#include <cmath>
#include <cstdint>
#include <vector>

#include "engine.hpp"

namespace lob {

// Flow constants, mirroring exchange/bench.py one for one.
inline constexpr i64 kFlowStartPrice = 10'000;        // ticks
inline constexpr double kMidStepSd = 0.15;            // ticks of mid move per command
inline constexpr i64 kLot = 100;
inline constexpr i64 kOwners = 199;                   // owner ids 1..199
inline constexpr double kPassiveMeanExtraTicks = 4.0; // passive distance = 1 + Exp(mean 4)
inline constexpr i64 kPassiveMaxLots = 10;
inline constexpr i64 kMarketableMaxLots = 8;
inline constexpr i64 kMarketableMaxThrough = 3;       // ticks past the mid

// Cumulative thresholds on one uniform draw r deciding the command kind.
inline constexpr double kPPassiveAdd = 0.48;     // r < 0.48: passive add
inline constexpr double kPPostOnlyFrom = 0.44;   // top slice of the passive band
inline constexpr double kPCancel = 0.86;         // 0.48 <= r < 0.86: cancel
inline constexpr double kPReplace = 0.89;        // 0.86 <= r < 0.89: cancel/replace
// Cumulative thresholds on a second draw u choosing the marketable order type.
inline constexpr double kPMarketableLimit = 0.64;
inline constexpr double kPIoc = 0.82;
inline constexpr double kPMarket = 0.96;         // remainder: FOK

// splitmix64: tiny, seedable, passes BigCrush-level statistics for this purpose.
class Rng {
public:
    explicit Rng(u64 seed) : state_(seed) {}

    u64 next_u64() {
        u64 z = (state_ += 0x9E3779B97F4A7C15ull);
        z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9ull;
        z = (z ^ (z >> 27)) * 0x94D049BB133111EBull;
        return z ^ (z >> 31);
    }

    // Uniform in [0, 1) with 53 random bits, like CPython's random().
    double uniform() { return static_cast<double>(next_u64() >> 11) * (1.0 / 9007199254740992.0); }

    // Exponential with rate `lambd` (mean 1/lambd), like Python's expovariate.
    double expovariate(double lambd) {
        double u = uniform();
        while (u <= 0.0) u = uniform();
        return -std::log(u) / lambd;
    }

    // Normal(mu, sd) via Box-Muller with a cached spare (deterministic).
    double gauss(double mu, double sd) {
        if (has_spare_) {
            has_spare_ = false;
            return mu + sd * spare_;
        }
        double u1 = uniform();
        while (u1 <= 0.0) u1 = uniform();
        const double u2 = uniform();
        const double mag = std::sqrt(-2.0 * std::log(u1));
        const double two_pi = 6.283185307179586;
        spare_ = mag * std::sin(two_pi * u2);
        has_spare_ = true;
        return mu + sd * (mag * std::cos(two_pi * u2));
    }

    // Integer in [a, b], both inclusive, like Python's randint.
    i64 randint(i64 a, i64 b) {
        return a + static_cast<i64>(next_u64() % static_cast<u64>(b - a + 1));
    }

    // Integer in [0, n).
    u64 randrange(u64 n) { return next_u64() % n; }

private:
    u64 state_;
    double spare_ = 0.0;
    bool has_spare_ = false;
};

// Generate `n` commands of realistic mixed flow around a random-walk mid.
inline std::vector<Command> synthetic_flow(i64 n, u64 seed, i64 start = kFlowStartPrice) {
    Rng rng(seed);
    Engine shadow;  // tracks live orders so cancels and replaces hit real ones
    double mid = static_cast<double>(start);
    std::vector<Command> cmds;
    cmds.reserve(static_cast<std::size_t>(n));
    std::vector<i64> passive;  // candidate live passive order ids (pruned lazily)
    i64 oid = 0;

    // Index into `passive` of a random order that is still resting, or -1.
    auto live_passive = [&]() -> std::ptrdiff_t {
        while (!passive.empty()) {
            const std::size_t i = static_cast<std::size_t>(rng.randrange(passive.size()));
            if (shadow.has_order(passive[i])) return static_cast<std::ptrdiff_t>(i);
            passive[i] = passive.back();
            passive.pop_back();
        }
        return -1;
    };

    for (i64 k = 0; k < n; ++k) {
        // The random-walk mid sets where passive and marketable prices land.
        mid += rng.gauss(0.0, kMidStepSd);
        const i64 m = static_cast<i64>(std::llround(mid));
        const double r = rng.uniform();
        const Side side = rng.uniform() < 0.5 ? Side::Buy : Side::Sell;
        const i64 owner = 1 + static_cast<i64>(rng.randrange(static_cast<u64>(kOwners)));
        bool have = false;
        Command cmd{};
        // One uniform draw r picks the command kind; thresholds are cumulative.
        if (r < kPPassiveAdd) {
            ++oid;
            const i64 dist =
                1 + static_cast<i64>(rng.expovariate(1.0 / kPassiveMeanExtraTicks));
            // The top slice of the passive band is post-only, so the flow includes
            // orders that must never take liquidity.
            const OrdType ot = r > kPPostOnlyFrom ? OrdType::PostOnly : OrdType::Limit;
            const i64 qty = kLot * rng.randint(1, kPassiveMaxLots);
            cmd = make_new(oid, owner, side, ot, qty, m - sign_of(side) * dist);
            passive.push_back(oid);
            have = true;
        } else if (r < kPReplace) {
            // Cancel and replace only act on live orders.
            const std::ptrdiff_t i = live_passive();
            if (i >= 0) {
                const i64 target = passive[static_cast<std::size_t>(i)];
                if (r < kPCancel) {
                    passive[static_cast<std::size_t>(i)] = passive.back();
                    passive.pop_back();
                    cmd = make_cancel(target);
                } else {
                    // The replace keeps the live order's side and gets a fresh
                    // price distance and size.
                    const auto st = shadow.order_state(target);
                    const i64 dist =
                        1 + static_cast<i64>(rng.expovariate(1.0 / kPassiveMeanExtraTicks));
                    const i64 qty = kLot * rng.randint(1, kPassiveMaxLots);
                    cmd = make_replace(target, m - sign_of(st->side) * dist, qty);
                }
                have = true;
            }
        }
        if (!have) {  // marketable flow (also the fallback when nothing is live)
            ++oid;
            const i64 qty = kLot * rng.randint(1, kMarketableMaxLots);
            // Marketable prices sit up to kMarketableMaxThrough ticks past the mid;
            // whether they trade depends on the live touch.
            const i64 px = m + sign_of(side) * rng.randint(0, kMarketableMaxThrough);
            const double u = rng.uniform();
            if (u < kPMarketableLimit) {
                // A LIMIT that does not fully cross rests, so track it as passive.
                cmd = make_new(oid, owner, side, OrdType::Limit, qty, px);
                passive.push_back(oid);
            } else if (u < kPIoc) {
                cmd = make_new(oid, owner, side, OrdType::Ioc, qty, px);
            } else if (u < kPMarket) {
                cmd = make_new(oid, owner, side, OrdType::Market, qty);
            } else {
                cmd = make_new(oid, owner, side, OrdType::Fok, qty, px);
            }
        }
        // Advance the shadow book so the next cancel/replace sees true live state.
        shadow.process(cmd);
        cmds.push_back(cmd);
    }
    return cmds;
}

}  // namespace lob
