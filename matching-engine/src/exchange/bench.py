"""Throughput / latency benchmark on a deterministic synthetic order flow.

    python -m exchange.bench [--orders N] [--seed S]

Flow mix (per command): ~44% passive limit adds, 4% post-only adds, 38% cancels of a live
resting order, 3% cancel/replace, 7% marketable limit, 2% IOC, 1.5% market, 0.5% FOK.
The flow is generated up front (against a shadow engine, so cancels and replaces target
orders that are actually live) and then fed to a fresh engine; generation is not timed.
Everything except the timing figures is deterministic for a given seed.
"""

from __future__ import annotations

import argparse
import gc
import json
import random
import statistics
import time
from typing import Any, Final

from .config import BENCH_ORDERS, BENCH_REPEATS, BENCH_SEED
from .engine import Engine
from .rebuild import replay
from .sim import random_side
from .types import Cancel, Command, NewOrder, OrdType, Replace

FLOW_START_PRICE: Final = 10_000  # ticks
MID_STEP_SD: Final = 0.15  # ticks of random-walk move in the mid per command
LOT: Final = 100
N_OWNERS: Final = 199  # owner ids 1..199
PASSIVE_MEAN_EXTRA_TICKS: Final = 4  # passive distance from mid = 1 + Exp(mean 4)
PASSIVE_MAX_LOTS: Final = 10
MARKETABLE_MAX_LOTS: Final = 8
MARKETABLE_MAX_THROUGH: Final = 3  # ticks a marketable price may sit through the mid
DEPTH_SAMPLE_EVERY: Final = 1_000  # commands between depth samples in the latency pass

# Cumulative thresholds on one uniform draw ``r`` deciding the command kind.
P_PASSIVE_ADD: Final = 0.48  # r < 0.48: passive add (post-only when r > 0.44)
P_POST_ONLY_FROM: Final = 0.44
P_CANCEL: Final = 0.86  # 0.48 <= r < 0.86: cancel a live passive order
P_REPLACE: Final = 0.89  # 0.86 <= r < 0.89: cancel/replace it; else marketable flow
# Cumulative thresholds on a second draw ``u`` choosing the marketable order type.
P_MARKETABLE_LIMIT: Final = 0.64
P_IOC: Final = 0.82
P_MARKET: Final = 0.96  # remainder: FOK


def _passive_distance(rng: random.Random) -> int:
    """Ticks from the mid for a passive order: 1 + Exp(mean PASSIVE_MEAN_EXTRA_TICKS)."""
    return 1 + int(rng.expovariate(1 / PASSIVE_MEAN_EXTRA_TICKS))


def synthetic_flow(n: int, seed: int = BENCH_SEED, start: int = FLOW_START_PRICE) -> list[Command]:
    """Generate ``n`` commands of realistic mixed flow around a random-walk mid."""
    rng = random.Random(seed)
    # A shadow engine tracks which orders are live while generating, so cancels and replaces target
    # real orders.
    shadow = Engine(journal=False)
    mid = float(start)
    cmds: list[Command] = []
    passive: list[int] = []  # candidate live passive order ids (pruned lazily)
    oid = 0

    def live_passive() -> int | None:
        """Index into ``passive`` of a random order that is still resting, or None."""
        while passive:
            i = rng.randrange(len(passive))
            if shadow.has_order(passive[i]):
                return i
            passive[i] = passive[-1]
            passive.pop()
        return None

    for _ in range(n):
        # The random-walk mid sets where passive and marketable prices land.
        mid += rng.gauss(0.0, MID_STEP_SD)
        m = round(mid)
        r = rng.random()
        side = random_side(rng)
        owner = rng.randrange(1, N_OWNERS + 1)
        cmd: Command | None = None
        # One uniform draw r picks the command kind. Thresholds are cumulative, so each branch owns
        # one band of r.
        if r < P_PASSIVE_ADD:
            oid += 1
            dist = _passive_distance(rng)
            # The top slice of the passive band becomes post-only, so the flow includes orders that
            # must never take liquidity.
            otype = OrdType.POST_ONLY if r > P_POST_ONLY_FROM else OrdType.LIMIT
            qty = LOT * rng.randint(1, PASSIVE_MAX_LOTS)
            cmd = NewOrder(oid, owner, side, otype, qty, m - side.sign * dist)
            passive.append(oid)
        elif r < P_REPLACE:
            # Cancel and replace only act on live orders. live_passive() picks one at random and
            # prunes dead ids on the way.
            i = live_passive()
            if i is not None:
                target = passive[i]
                if r < P_CANCEL:
                    passive[i] = passive[-1]
                    passive.pop()
                    cmd = Cancel(target)
                else:  # cancel/replace to a new price and size on the same side
                    # The replace keeps the live order's side and gets a fresh price distance and
                    # size.
                    state = shadow.order_state(target)
                    assert state is not None
                    dist = _passive_distance(rng)
                    qty = LOT * rng.randint(1, PASSIVE_MAX_LOTS)
                    cmd = Replace(target, m - state[0].sign * dist, qty)
        if cmd is None:  # marketable flow (also the fallback when nothing is live)
            oid += 1
            qty = LOT * rng.randint(1, MARKETABLE_MAX_LOTS)
            # Marketable prices sit up to MARKETABLE_MAX_THROUGH ticks past the mid. Whether they
            # trade depends on the live touch.
            px = m + side.sign * rng.randint(0, MARKETABLE_MAX_THROUGH)
            u = rng.random()
            # A second draw u picks the marketable type. A LIMIT that does not fully cross rests, so
            # it is tracked as a passive candidate.
            if u < P_MARKETABLE_LIMIT:
                cmd = NewOrder(oid, owner, side, OrdType.LIMIT, qty, px)
                passive.append(oid)
            elif u < P_IOC:
                cmd = NewOrder(oid, owner, side, OrdType.IOC, qty, px)
            elif u < P_MARKET:
                cmd = NewOrder(oid, owner, side, OrdType.MARKET, qty)
            else:
                cmd = NewOrder(oid, owner, side, OrdType.FOK, qty, px)
        # Advance the shadow book so the next cancel or replace sees the true live state.
        shadow.process(cmd)
        cmds.append(cmd)
    return cmds


def _pct(sorted_vals: list[int], p: float) -> float:
    """Nearest-rank percentile ``p`` (0-100) of an ascending list."""
    k = min(len(sorted_vals) - 1, max(0, round(p / 100 * (len(sorted_vals) - 1))))
    return float(sorted_vals[k])


def _count_by_kind(flow: list[Command]) -> dict[str, int]:
    """Command mix: order type name for new orders, class name otherwise."""
    kinds: dict[str, int] = {}
    for c in flow:
        k = c.otype.name if isinstance(c, NewOrder) else type(c).__name__
        kinds[k] = kinds.get(k, 0) + 1
    return dict(sorted(kinds.items()))


def run_bench(
    n_orders: int = BENCH_ORDERS, seed: int = BENCH_SEED, repeats: int = BENCH_REPEATS
) -> dict[str, Any]:
    """Throughput, per-command latency, book depth and replay determinism on one flow."""
    flow = synthetic_flow(n_orders, seed)

    # 1) Throughput: tight loop, no per-op timer, best of `repeats` fresh engines.
    # The cyclic GC is paused inside timed loops (as a production engine would tune it).
    rates = []
    for _ in range(repeats):
        eng = Engine(journal=False)
        proc = eng.process
        gc.collect()
        gc.disable()
        t0 = time.perf_counter()
        for c in flow:
            proc(c)
        # Only the flow loop is timed. Engine construction and gc setup happen outside the window.
        rates.append(n_orders / (time.perf_counter() - t0))
        gc.enable()
        del eng, proc

    # 2) Latency: per-command wall time (journal on), depth sampling, event accounting.
    eng = Engine(journal=True)
    proc = eng.process
    clock = time.perf_counter_ns
    lat: list[int] = []
    depth_samples = []
    counts: dict[str, int] = {}
    gc.collect()
    gc.disable()
    for i, c in enumerate(flow):
        # Time the process() call alone, with the journal on, so journaling is included.
        t0 = clock()
        evs = proc(c)
        lat.append(clock() - t0)
        for e in evs:
            k = type(e).__name__
            counts[k] = counts.get(k, 0) + 1
        # Depth is sampled sparsely: depth_stats walks every level, too costly per command.
        if i % DEPTH_SAMPLE_EVERY == DEPTH_SAMPLE_EVERY - 1:
            depth_samples.append(eng.depth_stats())
    gc.enable()
    lat.sort()

    # 3) Determinism: replay the journal on a fresh engine, compare book hashes.
    t0 = time.perf_counter()
    rep = replay(eng.journal or [])
    replay_s = time.perf_counter() - t0
    hash_match = rep.book_hash() == eng.book_hash()

    def mean_of(key: str) -> float:
        """Average of one depth statistic over every snapshot taken during the run."""
        return round(statistics.fmean(d[key] for d in depth_samples), 1)

    return {
        "orders": n_orders,
        "seed": seed,
        # The best run is the least disturbed one: slower runs are usually scheduler or cache noise.
        "throughput_orders_per_sec": round(max(rates)),
        "throughput_runs": [round(r) for r in rates],
        "gc_paused_in_timed_loops": True,
        "latency_ns": {
            "p50": _pct(lat, 50),
            "p99": _pct(lat, 99),
            "p99_9": _pct(lat, 99.9),
            "max": float(lat[-1]),
            "mean": round(statistics.fmean(lat), 1),
        },
        "command_mix": _count_by_kind(flow),
        "events": dict(sorted(counts.items())),
        "events_per_command": round(sum(counts.values()) / n_orders, 2),
        "depth": {
            "mean_bid_levels": mean_of("bid_levels"),
            "mean_ask_levels": mean_of("ask_levels"),
            "mean_resting_orders": mean_of("resting_orders"),
            "max_resting_orders": max(d["resting_orders"] for d in depth_samples),
            "final": eng.depth_stats(),
        },
        "replay": {"hash_match": hash_match, "seconds": round(replay_s, 3)},
        "book_hash": eng.book_hash(),
    }


def main() -> None:
    """Command-line entry point: run the benchmark and print its metrics as JSON."""
    ap = argparse.ArgumentParser(description="matching engine benchmark")
    ap.add_argument("--orders", type=int, default=BENCH_ORDERS)
    ap.add_argument("--seed", type=int, default=BENCH_SEED)
    args = ap.parse_args()
    print(json.dumps(run_bench(args.orders, args.seed), indent=2))


if __name__ == "__main__":
    main()
