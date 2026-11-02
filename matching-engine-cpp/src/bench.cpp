// Throughput / latency benchmark on the deterministic synthetic order flow.
//
//     bench.exe [orders] [seed]       (defaults: 1,000,000 commands, seed 42)
//
// Methodology mirrors the Python twin (exchange/bench.py):
//   1. The flow is generated up front against a shadow engine (not timed).
//   2. Throughput: a tight loop over the whole flow on a fresh engine, best of 3.
//   3. Latency: a separate pass timing each process() call with steady_clock,
//      sampling book depth every 1,000 commands and counting events by type.
//   4. Determinism: the same flow is fed to a fresh engine and the FNV-1a book
//      hashes are compared.
// Everything except the timing figures is deterministic for a given seed.
//
// Writes results/metrics.json, results/REPORT.md and results/site.json (UTF-8,
// LF line endings; the files are opened in binary mode so Windows does not
// rewrite the newlines).

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <map>
#include <string>
#include <vector>

#include "engine.hpp"
#include "flow.hpp"

using lob::Command;
using lob::Engine;
using lob::Event;
using lob::EventType;
using lob::i64;
using lob::OrdType;
using lob::u64;

namespace {

constexpr i64 kDefaultOrders = 1'000'000;
constexpr u64 kDefaultSeed = 42;
constexpr int kRepeats = 3;
constexpr i64 kDepthSampleEvery = 1'000;

// Check count printed by tests.exe ("OK <N> tests") from the committed run;
// surfaced in site.json so the dashboard can show it.
constexpr int kTestChecks = 105;

// Committed benchmark figures of the Python twin (matching-engine/results/
// metrics.json), used for the honest same-machine comparison.
constexpr double kPythonThroughput = 214'338.0;
constexpr double kPythonP50Us = 7.2;
constexpr double kPythonP99Us = 17.3;
constexpr double kPythonP999Us = 52.6;

using Clock = std::chrono::steady_clock;

double seconds_since(Clock::time_point t0) {
    return std::chrono::duration<double>(Clock::now() - t0).count();
}

// Nearest-rank percentile p (0-100) of an ascending vector, like the Python bench.
double pct(const std::vector<i64>& sorted_vals, double p) {
    const double n = static_cast<double>(sorted_vals.size());
    double kd = std::llround(p / 100.0 * (n - 1.0)) >= 0
                    ? static_cast<double>(std::llround(p / 100.0 * (n - 1.0)))
                    : 0.0;
    if (kd > n - 1.0) kd = n - 1.0;
    return static_cast<double>(sorted_vals[static_cast<std::size_t>(kd)]);
}

std::string comma(i64 v) {
    std::string s = std::to_string(v);
    for (i64 i = static_cast<i64>(s.size()) - 3; i > 0; i -= 3)
        s.insert(static_cast<std::size_t>(i), ",");
    return s;
}

std::string fmt(double v, int decimals) {
    char buf[64];
    std::snprintf(buf, sizeof(buf), "%.*f", decimals, v);
    return std::string(buf);
}

std::string hex64(u64 v) {
    char buf[32];
    std::snprintf(buf, sizeof(buf), "%016llx", static_cast<unsigned long long>(v));
    return std::string(buf);
}

std::string compiler_id() {
#if defined(_MSC_VER)
    return "MSVC " + std::to_string(_MSC_VER);
#elif defined(__clang__)
    return std::string("clang ") + __clang_version__;
#elif defined(__GNUC__)
    return "g++ " + std::to_string(__GNUC__) + "." + std::to_string(__GNUC_MINOR__);
#else
    return "unknown";
#endif
}

struct BenchResult {
    i64 orders = 0;
    u64 seed = 0;
    std::vector<double> rates;   // orders/s per throughput run
    double best_rate = 0.0;
    std::vector<i64> lat;        // sorted per-command latencies, ns
    double lat_mean = 0.0;
    std::map<std::string, i64> command_mix;
    std::map<std::string, i64> event_counts;
    i64 total_events = 0;
    double mean_bid_levels = 0.0;
    double mean_ask_levels = 0.0;
    double mean_resting_orders = 0.0;
    i64 max_resting_orders = 0;
    lob::DepthStats final_depth;
    bool hash_match = false;
    double replay_seconds = 0.0;
    u64 book_hash = 0;
};

BenchResult run_bench(i64 n, u64 seed) {
    BenchResult res;
    res.orders = n;
    res.seed = seed;
    const std::vector<Command> flow = lob::synthetic_flow(n, seed);

    for (const Command& c : flow) {
        std::string k = c.type == lob::CmdType::New        ? lob::ordtype_name(c.otype)
                        : c.type == lob::CmdType::CancelCmd ? "Cancel"
                                                            : "Replace";
        ++res.command_mix[k];
    }

    // 1) Throughput: tight loop, no per-op timer, best of kRepeats fresh engines.
    for (int r = 0; r < kRepeats; ++r) {
        Engine eng;
        const auto t0 = Clock::now();
        for (const Command& c : flow) eng.process(c);
        const double dt = seconds_since(t0);
        res.rates.push_back(static_cast<double>(n) / dt);
    }
    // The best run is the least disturbed one; slower runs are scheduler noise.
    res.best_rate = *std::max_element(res.rates.begin(), res.rates.end());

    // 2) Latency: per-command wall time, depth sampling, event accounting.
    Engine eng;
    res.lat.reserve(static_cast<std::size_t>(n));
    std::vector<lob::DepthStats> depth_samples;
    i64 counts[lob::kEventTypes] = {};
    i64 i = 0;
    for (const Command& c : flow) {
        const auto t0 = Clock::now();
        const auto& evs = eng.process(c);
        res.lat.push_back(
            std::chrono::duration_cast<std::chrono::nanoseconds>(Clock::now() - t0).count());
        for (const Event& e : evs) ++counts[static_cast<int>(e.type)];
        if (++i % kDepthSampleEvery == 0) depth_samples.push_back(eng.depth_stats());
    }
    double lat_sum = 0.0;
    for (const i64 v : res.lat) lat_sum += static_cast<double>(v);
    res.lat_mean = lat_sum / static_cast<double>(res.lat.size());
    std::sort(res.lat.begin(), res.lat.end());
    for (int t = 0; t < lob::kEventTypes; ++t) {
        if (counts[t] > 0) res.event_counts[lob::event_name(static_cast<EventType>(t))] = counts[t];
        res.total_events += counts[t];
    }
    double sb = 0.0;
    double sa = 0.0;
    double so = 0.0;
    for (const auto& d : depth_samples) {
        sb += static_cast<double>(d.bid_levels);
        sa += static_cast<double>(d.ask_levels);
        so += static_cast<double>(d.resting_orders);
        res.max_resting_orders = std::max(res.max_resting_orders, d.resting_orders);
    }
    const double ns = static_cast<double>(depth_samples.size());
    res.mean_bid_levels = sb / ns;
    res.mean_ask_levels = sa / ns;
    res.mean_resting_orders = so / ns;
    res.final_depth = eng.depth_stats();
    res.book_hash = eng.book_hash();

    // 3) Determinism: feed the identical command log to a fresh engine and
    // compare the FNV-1a book hashes.
    Engine rep;
    const auto t0 = Clock::now();
    for (const Command& c : flow) rep.process(c);
    res.replay_seconds = seconds_since(t0);
    res.hash_match = rep.book_hash() == res.book_hash;
    return res;
}

// ---------------------------------------------------------------- writers

std::ofstream open_lf(const std::string& path) {
    // Binary mode keeps LF endings on Windows; content is plain ASCII/UTF-8.
    return std::ofstream(path, std::ios::binary);
}

void write_metrics(const BenchResult& r, const std::string& path) {
    std::ofstream f = open_lf(path);
    f << "{\n";
    f << "  \"orders\": " << r.orders << ",\n";
    f << "  \"seed\": " << r.seed << ",\n";
    f << "  \"compiler\": \"" << compiler_id() << "\",\n";
    f << "  \"throughput_orders_per_sec\": " << static_cast<i64>(std::llround(r.best_rate))
      << ",\n";
    f << "  \"throughput_runs\": [";
    for (std::size_t i = 0; i < r.rates.size(); ++i)
        f << (i ? ", " : "") << static_cast<i64>(std::llround(r.rates[i]));
    f << "],\n";
    f << "  \"latency_ns\": {\n";
    f << "    \"p50\": " << fmt(pct(r.lat, 50), 1) << ",\n";
    f << "    \"p99\": " << fmt(pct(r.lat, 99), 1) << ",\n";
    f << "    \"p99_9\": " << fmt(pct(r.lat, 99.9), 1) << ",\n";
    f << "    \"max\": " << fmt(static_cast<double>(r.lat.back()), 1) << ",\n";
    f << "    \"mean\": " << fmt(r.lat_mean, 1) << "\n";
    f << "  },\n";
    f << "  \"command_mix\": {";
    bool first = true;
    for (const auto& [k, v] : r.command_mix) {
        f << (first ? "" : ", ") << "\"" << k << "\": " << v;
        first = false;
    }
    f << "},\n";
    f << "  \"events\": {";
    first = true;
    for (const auto& [k, v] : r.event_counts) {
        f << (first ? "" : ", ") << "\"" << k << "\": " << v;
        first = false;
    }
    f << "},\n";
    f << "  \"events_per_command\": "
      << fmt(static_cast<double>(r.total_events) / static_cast<double>(r.orders), 2) << ",\n";
    f << "  \"depth\": {\n";
    f << "    \"mean_bid_levels\": " << fmt(r.mean_bid_levels, 1) << ",\n";
    f << "    \"mean_ask_levels\": " << fmt(r.mean_ask_levels, 1) << ",\n";
    f << "    \"mean_resting_orders\": " << fmt(r.mean_resting_orders, 1) << ",\n";
    f << "    \"max_resting_orders\": " << r.max_resting_orders << ",\n";
    f << "    \"final\": {\"bid_levels\": " << r.final_depth.bid_levels
      << ", \"ask_levels\": " << r.final_depth.ask_levels
      << ", \"resting_orders\": " << r.final_depth.resting_orders
      << ", \"resting_qty\": " << r.final_depth.resting_qty << "}\n";
    f << "  },\n";
    f << "  \"replay\": {\"hash_match\": " << (r.hash_match ? "true" : "false")
      << ", \"seconds\": " << fmt(r.replay_seconds, 3) << "},\n";
    f << "  \"book_hash_fnv1a\": \"" << hex64(r.book_hash) << "\"\n";
    f << "}\n";
}

void write_report(const BenchResult& r, const std::string& path) {
    const double speedup = r.best_rate / kPythonThroughput;
    std::ofstream f = open_lf(path);
    f << "# Benchmark report: C++ matching engine\n\n";
    f << "Generated by `bench.exe` (" << compiler_id() << ", single thread, Windows laptop). "
      << "The flow is " << comma(r.orders) << " synthetic commands around a random-walk mid "
      << "(seed " << r.seed << "), the same flow shape as the Python twin's benchmark. "
      << "Generation is not timed.\n\n";
    f << "## Engine benchmark\n\n";
    f << "| metric | value |\n|---|---|\n";
    f << "| throughput (best of " << kRepeats << ") | "
      << comma(static_cast<i64>(std::llround(r.best_rate))) << " orders/s |\n";
    f << "| throughput runs | ";
    for (std::size_t i = 0; i < r.rates.size(); ++i)
        f << (i ? ", " : "") << comma(static_cast<i64>(std::llround(r.rates[i])));
    f << " orders/s |\n";
    f << "| per-order latency p50 / p99 / p99.9 | " << fmt(pct(r.lat, 50) / 1000.0, 2) << " / "
      << fmt(pct(r.lat, 99) / 1000.0, 2) << " / " << fmt(pct(r.lat, 99.9) / 1000.0, 2)
      << " us |\n";
    f << "| per-order latency mean / max | " << fmt(r.lat_mean / 1000.0, 2) << " / "
      << fmt(static_cast<double>(r.lat.back()) / 1000.0, 1) << " us |\n";
    f << "| events per command | "
      << fmt(static_cast<double>(r.total_events) / static_cast<double>(r.orders), 2) << " |\n";
    f << "| mean book depth | " << fmt(r.mean_bid_levels, 1) << " bid / "
      << fmt(r.mean_ask_levels, 1) << " ask levels, " << fmt(r.mean_resting_orders, 0)
      << " resting orders (max " << r.max_resting_orders << ") |\n";
    f << "| replay reproduces FNV-1a book hash | " << (r.hash_match ? "yes" : "NO") << " |\n\n";
    f << "Command mix:";
    bool first_mix = true;
    for (const auto& [k, v] : r.command_mix) {
        f << (first_mix ? " " : ", ") << k << " " << comma(v);
        first_mix = false;
    }
    f << ".\n\n";
    f << "## C++ vs the Python twin\n\n";
    f << "The Python numbers are the committed results of the Python twin "
      << "(`matching-engine/results/metrics.json`: 300,000 commands, seed 42, same machine, "
      << "single thread, GC paused in its timed loops). Same order types, same priority and "
      << "self-trade rules, same synthetic flow shape; flows are not command-for-command "
      << "identical because the two languages use different RNGs.\n\n";
    f << "| metric | C++ (this run) | Python (committed) | ratio |\n|---|---|---|---|\n";
    f << "| throughput | " << comma(static_cast<i64>(std::llround(r.best_rate)))
      << " orders/s | 214,338 orders/s | " << fmt(speedup, 1) << "x |\n";
    f << "| p50 latency | " << fmt(pct(r.lat, 50) / 1000.0, 2) << " us | "
      << fmt(kPythonP50Us, 1) << " us | " << fmt(kPythonP50Us / (pct(r.lat, 50) / 1000.0), 1)
      << "x |\n";
    f << "| p99 latency | " << fmt(pct(r.lat, 99) / 1000.0, 2) << " us | "
      << fmt(kPythonP99Us, 1) << " us | " << fmt(kPythonP99Us / (pct(r.lat, 99) / 1000.0), 1)
      << "x |\n";
    f << "| p99.9 latency | " << fmt(pct(r.lat, 99.9) / 1000.0, 2) << " us | "
      << fmt(kPythonP999Us, 1) << " us | "
      << fmt(kPythonP999Us / (pct(r.lat, 99.9) / 1000.0), 1) << "x |\n\n";
    f << "## Methodology and variance\n\n";
    f << "- Throughput is a tight untimed-per-op loop over the whole flow on a fresh engine, "
      << "best of " << kRepeats << " runs; the slower runs are scheduler and cache noise on a "
      << "shared laptop. This run's three rates are in the table above; expect roughly +-20% "
      << "between runs.\n";
    f << "- Latency is a separate pass that brackets each `process()` call with "
      << "`steady_clock::now()`, the same per-op method the Python bench uses. On Windows "
      << "that clock ticks in 100 ns steps and the timer call itself costs tens of "
      << "nanoseconds, so sub-microsecond percentiles are quantized and biased upward; the "
      << "throughput number, which has no per-op timer, is the fairer speed figure.\n";
    f << "- Depth is sampled every " << comma(kDepthSampleEvery) << " commands.\n";
    f << "- Determinism: the identical command log fed to a fresh engine reproduces the "
      << "FNV-1a book hash (`" << hex64(r.book_hash) << "`). The flow generator itself is "
      << "deterministic per seed and platform; its mid path uses libm, so bit-identical "
      << "flows across different C runtimes are not guaranteed.\n";
    f << "- The comparison with Python is throughput ratio on the same machine and the same "
      << "flow shape, not a command-for-command race: the Python benchmark ran 300,000 "
      << "commands with its own RNG, this one runs " << comma(r.orders)
      << " with splitmix64. Both books hold a similar number of levels and resting orders "
      << "at steady state.\n";
}

void write_site(const BenchResult& r, const std::string& path) {
    const double speedup = r.best_rate / kPythonThroughput;
    const double p50 = pct(r.lat, 50) / 1000.0;
    const double p99 = pct(r.lat, 99) / 1000.0;
    const double p999 = pct(r.lat, 99.9) / 1000.0;
    std::ofstream f = open_lf(path);
    f << "{\n";
    f << "  \"slug\": \"cpp\",\n";
    f << "  \"title\": \"C++ matching engine: the Python book, compiled\",\n";
    f << "  \"tagline\": \"The same price-time priority order book as the Python engine, with "
         "identical order types, priority rules and self-trade prevention, rebuilt in C++20 to "
         "show the same design running an order of magnitude faster on the same machine.\",\n";
    f << "  \"lane\": \"Trading & market infrastructure\",\n";
    f << "  \"stack\": [\"C++20\", \"MSVC\", \"g++\"],\n";
    f << "  \"tests\": " << kTestChecks << ",\n";
    f << "  \"run_cmd\": \"make run  (g++)  or  build.bat && bench.exe  (MSVC)\",\n";
    f << "  \"metrics\": [\n";
    f << "    {\"label\": \"Engine throughput\", \"value\": \""
      << comma(static_cast<i64>(std::llround(r.best_rate)))
      << "\", \"unit\": \"orders/s\", \"note\": \"single thread, " << comma(r.orders)
      << "-command synthetic flow, best of " << kRepeats << "\"},\n";
    f << "    {\"label\": \"p99 per-order latency\", \"value\": \"" << fmt(p99, 2)
      << "\", \"unit\": \"us\", \"note\": \"p50 " << fmt(p50, 2) << " us, p99.9 "
      << fmt(p999, 2) << " us\"},\n";
    f << "    {\"label\": \"Speedup vs Python twin\", \"value\": \"" << fmt(speedup, 1)
      << "x\", \"unit\": \"\", \"note\": \"same flow shape, same machine; vs the committed "
         "214,338 orders/s\"},\n";
    f << "    {\"label\": \"Replay determinism\", \"value\": \"identical\", \"unit\": \"\", "
         "\"note\": \"the same command log reproduces the FNV-1a book hash\"}\n";
    f << "  ],\n";
    f << "  \"highlights\": [\n";
    f << "    \"Same semantics as the Python engine: limit, market, IOC, FOK, post-only, "
         "cancel and cancel/replace with the same priority rules (size-down keeps queue "
         "priority; size-up or reprice loses it and re-enters as a taker)\",\n";
    f << "    \"Self-trade prevention (cancel newest) and an STP-aware FOK feasibility walk, "
         "so a FOK is killed rather than partially filled into a self-trade\",\n";
    f << "    \"Gap-free sequence numbers on every ack, fill, booked, cancel, replace and "
         "reject; replay of the command log reproduces the FNV-1a book hash\",\n";
    f << "    \"O(1) cancel via an order-id index into intrusive per-level FIFO lists; "
         "sorted price levels with the best level at the container end\",\n";
    f << "    \"" << kTestChecks << " checks ported from the Python suite pass, including "
         "randomized never-crossed and quantity-conservation runs on the full mixed flow\"\n";
    f << "  ],\n";
    f << "  \"limitations\": [\n";
    f << "    \"The benchmark is single-threaded and synthetic: one instrument, no network, "
         "no concurrent sessions, and the flow generator's statistical mix stands in for "
         "real order flow\",\n";
    f << "    \"Per-op latency includes the steady_clock timer cost itself, a visible "
         "fraction of a sub-microsecond median; throughput is the fairer figure\",\n";
    f << "    \"No auctions, halts, price bands or iceberg orders, and no persistence: the "
         "journal lives in memory for the replay check\"\n";
    f << "  ],\n";
    f << "  \"series\": {\n";
    f << "    \"name\": \"Per-order latency by percentile (us)\",\n";
    f << "    \"x\": [";
    std::vector<double> ps;
    for (int p = 1; p <= 99; ++p) ps.push_back(static_cast<double>(p));
    ps.push_back(99.5);
    ps.push_back(99.9);
    ps.push_back(99.99);
    for (std::size_t i = 0; i < ps.size(); ++i) f << (i ? ", " : "") << fmt(ps[i], 2);
    f << "],\n";
    f << "    \"y\": [";
    for (std::size_t i = 0; i < ps.size(); ++i)
        f << (i ? ", " : "") << fmt(pct(r.lat, ps[i]) / 1000.0, 3);
    f << "]\n";
    f << "  }\n";
    f << "}\n";
}

}  // namespace

int main(int argc, char** argv) {
    i64 n = kDefaultOrders;
    u64 seed = kDefaultSeed;
    if (argc > 1) n = std::atoll(argv[1]);
    if (argc > 2) seed = static_cast<u64>(std::atoll(argv[2]));
    if (n <= 0) {
        std::fprintf(stderr, "usage: bench [orders] [seed]\n");
        return 2;
    }

    std::printf("generating %lld commands (seed %llu)...\n", static_cast<long long>(n),
                static_cast<unsigned long long>(seed));
    const BenchResult r = run_bench(n, seed);

    std::printf("throughput best of %d: %lld orders/s (runs:", kRepeats,
                static_cast<long long>(std::llround(r.best_rate)));
    for (const double rate : r.rates)
        std::printf(" %lld", static_cast<long long>(std::llround(rate)));
    std::printf(")\n");
    std::printf("latency ns p50/p99/p99.9: %.0f / %.0f / %.0f (mean %.1f)\n", pct(r.lat, 50),
                pct(r.lat, 99), pct(r.lat, 99.9), r.lat_mean);
    std::printf("replay hash match: %s (book hash %s)\n", r.hash_match ? "yes" : "NO",
                hex64(r.book_hash).c_str());
    if (!r.hash_match) return 1;

    std::filesystem::create_directories("results");
    write_metrics(r, "results/metrics.json");
    write_report(r, "results/REPORT.md");
    write_site(r, "results/site.json");
    std::printf("wrote results/metrics.json, results/REPORT.md, results/site.json\n");
    return 0;
}
