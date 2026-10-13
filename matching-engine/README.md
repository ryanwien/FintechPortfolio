# matching-engine

A price-time priority limit order book matching engine with sequenced events and
deterministic replay, plus an event-driven backtester whose strategies trade *through* that
engine, so queue position, fees and slippage show up in their PnL.

## Why this matters in fintech

Every venue, internaliser and crossing network runs a matching engine. Its rules decide who
gets filled, at what price, and in what order. A bug here is a regulatory event (Reg NMS
trade-throughs, MiFID II best execution), not just a wrong number. Trading firms also need
to know how an engine behaves before they quote into it. Queue priority on cancel/replace,
self-trade prevention, and what happens to an IOC or FOK remainder are all things they need
to get right. Backtests that skip the order book make two predictable mistakes. They
overstate taker strategies, which in reality pay spread and fees. They also misread maker
strategies, which earn spread and rebates but lose to adverse selection. This project builds
the engine to exchange-style rules, proves its invariants with property tests, and then uses
it to show how much those costs change a backtest's conclusion.

## Architecture

```
            commands (NewOrder / Cancel / Replace / SnapshotRequest)
                                   |
                                   v
 +---------------------------- Engine.process -----------------------------+
 |  validate -> Reject | Ack                                               |
 |  FOK feasibility walk (stops at own order: STP-aware)                   |
 |  match: best opposite level -> FIFO head -> ... (STP: cancel newest)    |
 |  remainder: rest (LIMIT/POST_ONLY) | Cancelled (IOC/FOK/MARKET/STP)     |
 |                                                                          |
 |  book side: dict price -> Level   + sorted key list (best at end: pop)  |
 |  Level: intrusive doubly linked FIFO (O(1) unlink), aggregate qty       |
 |  index: order_id -> node            (O(1) cancel / replace lookup)      |
 +------------------------------------+-------------------------------------+
        | journal (inputs)            | events, gap-free seq:
        v                             v Ack Fill Booked Cancelled Replaced Reject
   replay() -> same events,          BookDelta (L2 increments) Snapshot
   same book hash                     |
                                      v
                      BookBuilder (rebuilds the L3 book from events alone; same hash)

 Backtester (one step = one tick of the sim clock)
   FundamentalProcess (regimes: calm / trend_up / trend_down / volatile, own RNG)
        |
   BackgroundMarket: informed IOC pick-offs -> noise market orders -> maker cancels/adds
        |                       (all through Engine.process)
   strategy orders decided at t-1 arrive here (1 step latency, behind the crowd)
        |
   Ledger (integer $0.0001 units): position, cash, cash-at-mid, fees/rebates
        |
   View(L2 depth 5, mid history <= t, position) -> Strategy.decide -> orders for t+1
```

Package layout (`src/exchange/`): `types.py` commands and events, `engine.py` order book
and matching, `rebuild.py` journal `replay()` and the event-sourced `BookBuilder`,
`config.py` seeds, run sizes and calendar constants, `bench.py` synthetic flow and
benchmark, `sim.py` fundamental process and background participants, `strategies.py`
market maker and momentum, `backtest.py` runner, ledger and metrics, `run.py` orchestration
and result writers, `report.py` REPORT.md and site.json rendering.

## Run it

```bash
cd matching-engine
pip install -r requirements.txt -e .   # from the repo's virtualenv
ruff check . && pytest
python -m exchange.run             # ~40 s; regenerates everything in results/
python -m exchange.bench          # benchmark only, prints JSON
```

## Results

All figures come from `results/metrics.json`, written by `python -m exchange.run` (Python
3.14, Windows laptop, single thread). Timing numbers move by roughly ±30% between runs on a
shared machine. The three throughput runs in this file were 214k, 148k and 155k orders/s.
Everything else is bit-for-bit deterministic.

**Engine benchmark** (300,000 commands, seed 42: 153,354 limit (passive and marketable),
11,993 post-only, 113,810 cancels, 8,974 cancel/replaces, 5,881 IOC, 4,613 market, 1,375 FOK):

| metric | value |
|---|---|
| throughput (best of 3) | 214,338 orders/s |
| per-order latency p50 / p99 / p99.9 | 7.2 / 17.3 / 52.6 µs |
| events per command | 2.72 (Ack, Fill, Booked, Cancelled, Replaced, Reject, BookDelta) |
| mean book depth | 15.3 bid / 15.2 ask levels, 141 resting orders (max 326) |
| journal replay reproduces book hash | yes |

**Backtest** (seed 7, 30,000 steps = 60 sim days, taker fee $0.0030/sh, maker rebate
$0.0020/sh). Net PnL decomposes exactly, in integer units, as costs-ignored (fills marked at
the pre-trade mid, no fees) + spread/slippage + fees/rebates:

| | market maker | momentum |
|---|---|---|
| **net PnL** | **$190.80** | **$465.00** |
| costs ignored | -$1,923.00 | $1,732.50 |
| spread capture / slippage | +$1,597.00 | -$916.50 |
| fees & rebates | +$516.80 | -$351.00 |
| Sharpe net / costs ignored | 4.39 / -19.93 | 3.01 / 12.15 |
| max drawdown (net) | $78.40 | $248.50 |
| fill ratio (filled / offered shares) | 0.369 | 0.997 |
| inventory mean abs / max abs (shares) | 53.8 / 300 | 182.1 / 500 |
| avg shares queued ahead when booked | 1,162 | n/a (IOC only) |

**Across 5 seeds (7, 11, 23, 42, 99):** the momentum strategy makes money with costs ignored
on 5/5 seeds (mean Sharpe 9.62) but only 3/5 net of costs (mean Sharpe 1.23). Spread and fees
take most of its edge. The market maker is the mirror image: it loses at mid on 5/5 seeds
(mean Sharpe -20.71, because informed flow picks off its quotes) and is net positive on 5/5
(mean Sharpe 7.36), because spread capture and rebates more than pay for that adverse selection. A
costs-ignored backtest would have you deploy the wrong one.

## Design decisions

- **Integer ticks and shares, integer money.** Prices are ticks ($0.01), quantities are
  shares, and backtest cash is in $0.0001 units, so fees like $0.003/sh stay exact. Floats
  only appear in reported ratios (Sharpe, fill ratio).
- **Intrusive linked list per level plus an id index.** Cancel is O(1) unlink with no
  tombstones, so the book never holds dead orders and `check_invariants()` can verify
  structure exactly. Price levels sit in a sorted key list oriented so the best price is
  last, which makes consuming the top of book a `pop()`.
- **Replace semantics follow common exchange practice.** `qty` is the new open quantity.
  Same price and smaller size keeps priority. A larger size or a new price loses priority,
  and the order re-enters as a taker, so a repriced order can trade. A post-only order
  cannot be replaced across the spread.
- **STP is cancel-newest**, and the FOK feasibility walk stops at the first own order. A FOK
  is therefore killed rather than partially filled into a self-trade.
- **Two replay paths.** `replay(journal)` re-runs the inputs and must reproduce the exact
  event list. `BookBuilder` rebuilds the full L3 book from output events alone, which is
  what a drop-copy or risk consumer does. Both must match `Engine.book_hash()`, and the
  property tests check both.
- **Property tests over random command streams** (Hypothesis, 200 examples each, narrow
  price band and 3 owners so crossing and STP happen constantly). The book is never
  crossed. Quantity is conserved (`submitted = 2·filled + resting + cancelled`, counting
  both sides of each fill). Every fill hits the head of the best opposite level and never
  trades through the taker's limit. Sequence numbers have no gaps. L2 increments rebuild
  the L2 book.
- **No lookahead, tested.** Strategies get a `View` built from data up to step t, and their
  orders arrive at t+1 behind that step's background flow. A test runs N and 2N steps and
  asserts that the first N steps' orders and equity are identical, so future data cannot
  change past decisions. The fundamental value has its own RNG stream and is never shown to
  strategies.
- **"Costs ignored" is computed from the same fills.** Both ledgers book every fill, one at
  the execution price with fees and one at the pre-trade mid without them. The difference
  is therefore pure cost, not path noise from a second simulation.

## Limitations

- Pure Python: ~10^5 orders/s and tens-of-µs tails, with GC paused in the timed loops. A
  production engine in C++/Rust on a pinned core does millions per second with sub-µs
  latency. The value here is in the rules and invariants, not raw speed.
- The market is synthetic. Background participants are stylised (post-only makers, noise
  market orders, an informed pick-off trader). Strategy PnL shows mechanics and cost
  sensitivity, not tradeable alpha. The high market-maker Sharpe reflects the absence of
  faster competing makers.
- Single instrument, continuous trading only: no opening/closing auctions, halts, price
  bands, iceberg/hidden orders, or GTD expiry. Positions are marked to mid at the end, not
  liquidated.

## What I'd do next

- Port the hot path (`_match`, level management) to Rust via PyO3 and keep the Python
  engine as the reference model, with differential tests on the same Hypothesis streams.
- Add auctions (uncrossing at the volume-maximising price), price bands and iceberg orders.
- Use a binary journal (fixed-width records) with a recovery test that kills the process
  mid-stream and replays to the same hash.
- Give the backtester a configurable latency distribution per participant, and add a
  competing fast market maker, so queue position and the MM's Sharpe become realistic.
