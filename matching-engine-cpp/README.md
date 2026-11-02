# matching-engine-cpp

The C++20 twin of [`matching-engine`](../matching-engine): the same price-time priority
limit order book, with the same order types, priority rules, self-trade prevention and
sequenced, replayable events, rebuilt in C++ to show the same design running an order of
magnitude faster on the same machine. No dependencies, no build system beyond one
compiler invocation; the tests are a hand-rolled CHECK framework with zero third-party
code.

## Why this matters in fintech

The Python engine proves the rules: who gets filled, at what price, in what order, and
that a replayed journal reproduces the book bit for bit. Production venues then need the
same rules at six or seven figures of orders per second. This project shows that the two
are the same artifact at different altitudes: the matching logic ports line for line
(the handler structure, the STP-aware FOK walk, the replace priority rules), while the
performance comes from the things C++ controls and Python does not, which are memory
layout, allocation, and the absence of an interpreter in the hot loop. Writing the
reference model first and the fast engine second, with the same semantics and a shared
invariant suite, is also how real trading systems are built and validated.

## Architecture

```
            commands (NewOrder / Cancel / Replace), one flat 48-byte struct
                                   |
                                   v
 +---------------------------- Engine::process ----------------------------+
 |  validate -> Reject | Ack                                               |
 |  FOK feasibility walk (stops at own order: STP-aware)                   |
 |  match: best opposite level -> FIFO head -> ... (STP: cancel newest)    |
 |  remainder: rest (LIMIT/POST_ONLY) | Cancelled (IOC/FOK/MARKET/STP)     |
 |                                                                         |
 |  book side: std::map<price * sign, Level>   (best level at the end)     |
 |  Level: intrusive doubly linked FIFO (O(1) unlink), aggregate qty       |
 |  index: unordered_map order_id -> Order*    (O(1) cancel / replace)     |
 |  orders: deque arena + free list            (no per-fill allocation)    |
 +------------------------------------+------------------------------------+
                                       | events, gap-free seq:
                                       v Ack Fill Booked Cancelled Replaced Reject
                   book_hash(): FNV-1a over the L3 book
                   (same command log -> same hash, checked every run)
```

Layout: `src/engine.hpp` is the whole engine (header-only), `src/flow.hpp` the
deterministic synthetic flow generator shared by the benchmark and the tests,
`src/bench.cpp` the benchmark and results writers, `tests/tests.cpp` the test suite.

## Build and run

Windows (MSVC, Visual Studio 2022 Build Tools):

```bat
build.bat        rem cl /std:c++20 /O2 /EHsc /W4 /WX -> tests.exe, bench.exe
tests.exe        rem prints: OK 105 tests
bench.exe        rem a few seconds; regenerates everything in results/
```

Linux / CI (g++):

```bash
make check       # g++ -std=c++20 -O2 -Wall -Wextra -Werror; runs ./build/tests
make run         # runs ./build/bench, regenerates results/
```

Both compilers build the same strictly standard C++20 sources with warnings as errors.

## Results

All figures come from `results/metrics.json`, written by `bench.exe` (MSVC 1944, Windows
laptop, single thread, 1,000,000 synthetic commands, seed 42). The flow mix matches the
Python benchmark: ~51% limit (passive and marketable), 4% post-only, 38% cancels, 3%
cancel/replaces, 2% IOC, 1.5% market, 0.4% FOK. Timing numbers move by roughly +-20%
between runs on a shared machine; everything else is deterministic for the seed.

| metric | value |
|---|---|
| throughput (best of 3) | 4,321,213 orders/s |
| per-order latency p50 / p99 / p99.9 | 0.2 / 0.6 / 1.4 us |
| mean book depth | 14.6 bid / 14.7 ask levels, 129 resting orders (max 302) |
| replay reproduces FNV-1a book hash | yes |
| tests | OK 105 tests |

Against the committed results of the Python twin (same machine, same flow shape, its own
300,000-command run at 214,338 orders/s, p99 17.3 us):

| metric | C++ | Python | ratio |
|---|---|---|---|
| throughput | 4,321,213 orders/s | 214,338 orders/s | 20.2x |
| p99 latency | 0.60 us | 17.3 us | 28.8x |

The comparison is honest but not a command-for-command race: the two benchmarks use
different RNGs, so the flows are statistically alike (the C++ book averages 14.6/14.7
levels and 129 resting orders against Python's 15.3/15.2 and 141) rather than identical.
The per-op latency figures sit near the 100 ns resolution of the Windows steady clock,
so the percentiles are quantized and biased upward; throughput, which has no per-op
timer, is the fairer speed figure. See `results/REPORT.md` for the full methodology.

## Design decisions

- **Integer ticks and shares.** Prices are `int64_t` ticks and quantities are `int64_t`
  shares, exactly like the Python engine. Nothing in the engine touches a float, so
  matching, hashing and replay are exact; floats exist only in the benchmark's flow
  generator (the random-walk mid) and its reported statistics.
- **Memory layout.** Each side is a `std::map` keyed by `price * sign` (bids +price,
  asks -price), so the best level is at the end of both maps and map nodes never move,
  which lets orders hold stable pointers to their level. Each level is an intrusive
  doubly linked FIFO, so cancel is an O(1) unlink with no tombstones, found through an
  `unordered_map` order-id index. Order nodes live in a `std::deque` arena with a free
  list: once the book is warm the hot path performs no allocation per fill or cancel.
  With the ~15 price levels per side this flow produces, the map's O(log n) is noise; a
  flat sorted vector of keys (as the Python engine uses) is the next step if level
  counts grew.
- **Same semantics, ported line for line.** Validation order, reject reasons, the
  replace priority rule (same price and size decrease keeps the queue position; a price
  change or size increase re-enters as a taker), cancel-newest STP, and the FOK
  feasibility walk that stops at the first own resting order all mirror the Python
  engine, which remains the reference model. The FOK own-liquidity kill and the
  post-only reprice-across-the-spread reject are covered by ported tests.
- **Determinism as a first-class output.** Every event carries a gap-free sequence
  number, and `book_hash()` is a 64-bit FNV-1a over the canonical L3 book (side, price,
  order id, owner, open quantity; best level first, FIFO within a level). The benchmark
  and the tests both feed the same command log to a fresh engine and require an
  identical hash.
- **Flat commands and events.** One POD struct each, with an enum tag. This keeps the
  flow a contiguous `std::vector<Command>` the benchmark can stream, and keeps event
  emission a push into a reused vector instead of an allocation per event.

## Limitations

- The benchmark is single-threaded and synthetic: one instrument, no network, no
  sessions, no risk checks, and the flow generator's statistical mix stands in for real
  order flow. Real engines spend much of their complexity exactly where this stops.
- Per-op latency is measured with `steady_clock` around each `process()` call; on
  Windows that clock ticks in 100 ns steps and the call itself costs tens of
  nanoseconds, so the sub-microsecond percentiles are coarse. The max (32.8 ms in the
  committed run) is scheduler noise on a shared laptop, not engine behaviour.
- The flow generator is deterministic per seed and platform, but its mid path uses libm,
  so bit-identical flows across different C runtimes are not guaranteed. The replay
  check always compares two runs of the same binary.
- No auctions, halts, price bands, iceberg orders or persistence; the journal lives in
  memory for the replay check. The Python twin's backtester has no C++ counterpart.

## What I'd do next

- Differential testing against the Python engine: one process generates a command
  stream, both engines consume it, and every event and book hash must agree. That turns
  the Python property tests into an oracle for the C++ hot path.
- Replace `std::map` with a flat sorted key vector plus an open-addressing price table,
  and pool events, then measure again; the current layout leaves an estimated 2-3x on
  the table.
- Pin the benchmark to a core, use the invariant TSC directly, and report latency as a
  full histogram instead of quantized percentiles.
- A binary journal on disk with a crash-recovery test: kill the process mid-stream,
  replay, and require the same hash.
