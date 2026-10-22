"""Benchmark + reconciliation run. Writes results/metrics.json, REPORT.md, site.json.

    python -m ledger.bench            # full run
    python -m ledger.bench --quick    # smaller, for a smoke test

Latency is measured around ``Ledger.create_payment`` (service layer, no HTTP),
on a file-backed SQLite database in WAL mode with ``synchronous=FULL`` (every
commit is fsynced). Each transfer writes 1 payment, 1 journal entry, 2 postings,
2 payment events, 1 idempotency record and 2 cached-balance updates.

Seeds and sizes come from :mod:`ledger.config`; rendering lives in
:mod:`ledger.report`. Only wall-clock values vary between runs.
"""

from __future__ import annotations

import argparse
import json
import math
import platform
import random
import sqlite3
import statistics
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Final

from ledger.config import (
    BENCH_CURRENCY,
    BENCH_SEED,
    FUNDING,
    LIFECYCLE_OPS,
    LIFECYCLE_OPS_QUICK,
    LIFECYCLE_WALLETS,
    MAX_LIFECYCLE_AMOUNT,
    MAX_TRANSFER_AMOUNT,
    OVERDRAFT_HOLD_ATTEMPTS,
    OVERDRAFT_HOLD_FRACTION,
    RECON_DAYS,
    RECON_MOVEMENTS_PER_DAY,
    RECON_MOVEMENTS_PER_DAY_QUICK,
    RECON_STANDARD_SEED,
    RECON_STRESS_SEED,
    REPLAY_AMOUNT,
    STANDARD_RATES,
    STRESS_RATES,
    THREAD_LEVELS,
    THREAD_LEVELS_QUICK,
    TRANSFERS_PER_LEVEL,
    TRANSFERS_PER_LEVEL_QUICK,
    WALLETS,
)
from ledger.domain import Direction, IdempotencyKey, InsufficientFundsError, PostingSpec
from ledger.recon import ledger_items_from_activity, reconcile, score
from ledger.report import write_outputs
from ledger.scenario import build_settlement_scenario
from ledger.service import Ledger
from ledger.statement import InjectionRates, generate_statement

RESULTS: Final = Path(__file__).resolve().parents[2] / "results"
NS_PER_MS: Final = 1e6
MS_PER_S: Final = 1000
POSTINGS_PER_TRANSFER: Final = 2
LATENCY_DECIMALS: Final = 3


def pct(values: list[float], p: float) -> float:
    """Nearest-rank percentile."""
    s = sorted(values)
    # Nearest rank: the ceil(p% of n)-th smallest value, clamped so that p=0 and p=100 stay in
    # range.
    k = max(0, min(len(s) - 1, math.ceil(p / 100 * len(s)) - 1))
    return s[k]


def _ms(values: list[float], p: float) -> float:
    """Percentile ``p`` of ``values``, rounded to the report's latency precision."""
    return round(pct(values, p), LATENCY_DECIMALS)


def _fund(ledger: Ledger, n: int) -> tuple[str, list[str]]:
    """Create ``n`` customer wallets, each funded by a deposit into the bank account."""
    bank = ledger.create_account("Bank settlement", "asset", BENCH_CURRENCY)["id"]
    wallets = []
    for i in range(n):
        w = ledger.create_account(f"wallet-{i}", "liability", BENCH_CURRENCY)["id"]
        # One balanced entry per wallet: the bank's debit and the wallet's credit net to zero in one
        # write.
        ledger.post_entry(
            [
                PostingSpec(bank, Direction.DEBIT, FUNDING, BENCH_CURRENCY),
                PostingSpec(w, Direction.CREDIT, FUNDING, BENCH_CURRENCY),
            ],
            description="initial deposit",
        )
        wallets.append(w)
    return bank, wallets


def transfer_load(
    db: Path, threads: int, total_ops: int, synchronous: str = "FULL"
) -> dict[str, Any]:
    """``total_ops`` idempotent transfers between random wallets from ``threads`` threads."""
    ledger = Ledger(db, synchronous=synchronous)
    _, wallets = _fund(ledger, WALLETS)
    # Snapshot wallet totals around the run. Transfers move money between wallets but must neither
    # create nor destroy any.
    wallet_total_before = sum(ledger.get_balance(w)["posted"] for w in wallets)
    # Integer division drops any remainder. The reported count comes from the latencies actually
    # recorded, so it stays honest.
    per_thread = total_ops // threads
    latencies: list[list[float]] = [[] for _ in range(threads)]
    errors: list[BaseException] = []
    # Workers plus the main thread, so the clock starts only once every worker is ready.
    barrier = threading.Barrier(threads + 1)

    def worker(t: int) -> None:
        """Run one thread's share of the transfers after the shared start barrier."""
        # Seeded per thread: the workload is reproducible, and no two threads share one generator.
        rng = random.Random(BENCH_SEED + t)
        lat = latencies[t]
        try:
            # Hold until every thread is ready, so thread start-up is not counted in latency.
            barrier.wait()
            for i in range(per_thread):
                src, dst = rng.sample(wallets, 2)
                amount = rng.randint(1, MAX_TRANSFER_AMOUNT)
                # A fresh key per transfer. Every call is a first write, so replays cannot inflate
                # the throughput figure.
                key = IdempotencyKey(f"bench-{t}-{i}")
                # Time only the service call, not the RNG draws or the key construction.
                t0 = time.perf_counter_ns()
                ledger.create_payment(src, dst, amount, BENCH_CURRENCY, idempotency=key)
                lat.append((time.perf_counter_ns() - t0) / NS_PER_MS)
        except BaseException as exc:  # pragma: no cover - re-raised on the main thread
            errors.append(exc)

    pool = [threading.Thread(target=worker, args=(t,)) for t in range(threads)]
    for th in pool:
        th.start()
    # Release the workers together, then start the wall clock.
    barrier.wait()
    t0 = time.perf_counter()
    for th in pool:
        th.join()
    wall = time.perf_counter() - t0
    if errors:
        raise errors[0]
    flat = [x for lat in latencies for x in lat]
    # Check the invariants once every thread has finished, on the settled state.
    check = ledger.verify()
    wallet_total_after = sum(ledger.get_balance(w)["posted"] for w in wallets)
    ledger.close()
    # Count the transfers that completed, from the recorded latencies.
    ops = len(flat)
    return {
        "threads": threads,
        "transfers": ops,
        "wall_s": round(wall, 3),
        "transfers_per_s": round(ops / wall, 1),
        "postings_per_s": round(POSTINGS_PER_TRANSFER * ops / wall, 1),
        "latency_ms": {
            "p50": _ms(flat, 50),
            "p95": _ms(flat, 95),
            "p99": _ms(flat, 99),
            "max": round(max(flat), LATENCY_DECIMALS),
            "mean": round(statistics.fmean(flat), LATENCY_DECIMALS),
        },
        "invariants_ok": check["ok"],
        "money_conserved": wallet_total_before == wallet_total_after,
    }


def lifecycle_and_replay(db: Path, n: int) -> dict[str, Any]:
    """Two-step authorize -> capture latency, idempotent replay latency, overdraft probe."""
    ledger = Ledger(db)
    _, wallets = _fund(ledger, LIFECYCLE_WALLETS)
    rng = random.Random(BENCH_SEED)
    auth, cap, replay, fresh = [], [], [], []
    for _ in range(n):
        src, dst = rng.sample(wallets, 2)
        amount = rng.randint(1, MAX_LIFECYCLE_AMOUNT)
        t0 = time.perf_counter_ns()
        pay = ledger.create_payment(src, dst, amount, BENCH_CURRENCY, capture=False)
        t1 = time.perf_counter_ns()
        ledger.capture_payment(pay["id"])
        t2 = time.perf_counter_ns()
        auth.append((t1 - t0) / NS_PER_MS)
        cap.append((t2 - t1) / NS_PER_MS)

    # Keep each key with its request, so the replay phase can resend the identical call.
    keys = []
    for i in range(n):
        src, dst = rng.sample(wallets, 2)
        k = IdempotencyKey(f"replay-{i}")
        t0 = time.perf_counter_ns()
        ledger.create_payment(src, dst, REPLAY_AMOUNT, BENCH_CURRENCY, idempotency=k)
        fresh.append((time.perf_counter_ns() - t0) / NS_PER_MS)
        keys.append((src, dst, k.key))
    # Count replays the ledger served from its store (k.replayed is set on a hit), not
    # re-executions.
    replayed = 0
    for src, dst, key in keys:
        k = IdempotencyKey(key)
        t0 = time.perf_counter_ns()
        ledger.create_payment(src, dst, REPLAY_AMOUNT, BENCH_CURRENCY, idempotency=k)
        replay.append((time.perf_counter_ns() - t0) / NS_PER_MS)
        replayed += k.replayed

    # Overdraft rule under load: try to drain one wallet with many holds.
    # Overdraft probe. Ten holds of one seventh of the balance add up to more than the balance, so
    # some of them must be refused.
    victim = wallets[0]
    chunk = ledger.get_balance(victim)["available"] // OVERDRAFT_HOLD_FRACTION
    accepted = rejected = 0
    for _ in range(OVERDRAFT_HOLD_ATTEMPTS):
        try:
            ledger.create_payment(victim, wallets[1], chunk, BENCH_CURRENCY, capture=False)
            accepted += 1
        except InsufficientFundsError:
            rejected += 1
    ok = ledger.verify()["ok"]
    ledger.close()
    return {
        "authorize_ms_p50": _ms(auth, 50),
        "authorize_ms_p99": _ms(auth, 99),
        "capture_ms_p50": _ms(cap, 50),
        "capture_ms_p99": _ms(cap, 99),
        "first_write_ms_p50": _ms(fresh, 50),
        "replay_ms_p50": _ms(replay, 50),
        "replay_ms_p99": _ms(replay, 99),
        "replays_served_from_store": replayed,
        "replays_attempted": len(keys),
        "holds_accepted_before_overdraft": accepted,
        "holds_rejected_insufficient_funds": rejected,
        "invariants_ok": ok,
    }


def recon_run(
    db: Path, *, days: int, per_day: int, rates: InjectionRates, seed: int
) -> dict[str, Any]:
    """Build a settlement month, generate a bank statement for it and reconcile."""
    ledger = Ledger(db, synchronous="NORMAL")
    t0 = time.perf_counter()
    sc = build_settlement_scenario(ledger, days=days, movements_per_day=per_day)
    build_s = time.perf_counter() - t0
    # The cash account's view (+ means cash in) is the side the bank statement describes.
    activity = ledger.account_activity(sc.cash_account_id, start=sc.start, end=sc.end)
    items = ledger_items_from_activity(activity)
    # The same items and seed always produce the same statement, so the scoring is repeatable.
    stmt = generate_statement(items, seed=seed, rates=rates)
    # Time only the matcher, excluding statement generation.
    t1 = time.perf_counter()
    report = reconcile(items, stmt.lines)
    recon_s = time.perf_counter() - t1
    ok = ledger.verify()["ok"]
    ledger.close()
    example: dict[str, Any] = {}
    # One representative break per kind, the first one found, for the report.
    for b in report.breaks:
        example.setdefault(b.kind.value, b.to_dict())
    return {
        "period": f"{sc.start}..{sc.end}",
        "scenario": {
            "deposits": sc.deposits,
            "payouts_posted": sc.payouts_posted,
            "payouts_failed": sc.payouts_failed,
            "internal_fees": sc.fees,
            "build_s": round(build_s, 3),
        },
        "summary": report.summary(items, stmt.lines),
        "scoring": score(report, stmt.injected),
        "reconcile_ms": round(recon_s * MS_PER_S, 2),
        "example_breaks": example,
        "ledger_invariants_ok": ok,
    }


def _headline(
    by_conc: list[dict[str, Any]],
    lifecycle: dict[str, Any],
    standard: dict[str, Any],
    stress: dict[str, Any],
) -> dict[str, Any]:
    """Reduce the full run to the flat headline figures stored in metrics.json."""
    # The first concurrency level is the single-thread run, and the headline latency is taken from
    # it.
    single = by_conc[0]
    return {
        "transfer_p50_ms": single["latency_ms"]["p50"],
        "transfer_p95_ms": single["latency_ms"]["p95"],
        "transfer_p99_ms": single["latency_ms"]["p99"],
        "transfers_per_s_single_thread": single["transfers_per_s"],
        "postings_per_s_single_thread": single["postings_per_s"],
        "max_transfers_per_s": max(r["transfers_per_s"] for r in by_conc),
        "recon_precision": standard["scoring"]["precision"],
        "recon_recall": standard["scoring"]["recall"],
        "recon_unexplained_difference": standard["summary"]["unexplained_difference"],
        "recon_stress_precision": stress["scoring"]["precision"],
        "recon_stress_recall": stress["scoring"]["recall"],
        "all_invariants_ok": all(r["invariants_ok"] and r["money_conserved"] for r in by_conc)
        and lifecycle["invariants_ok"]
        and standard["ledger_invariants_ok"],
    }


def main(argv: list[str] | None = None) -> dict[str, Any]:
    """Run every benchmark, write the results and return the metrics dict.

    Pass ``--quick`` for a smoke-test-sized run.
    """
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--quick", action="store_true")
    args = ap.parse_args(argv)
    # --quick shrinks the workloads only. The seeds are unchanged, so a quick run is still
    # deterministic.
    total = TRANSFERS_PER_LEVEL_QUICK if args.quick else TRANSFERS_PER_LEVEL
    levels = THREAD_LEVELS_QUICK if args.quick else THREAD_LEVELS
    per_day = RECON_MOVEMENTS_PER_DAY_QUICK if args.quick else RECON_MOVEMENTS_PER_DAY
    with tempfile.TemporaryDirectory() as tmp:
        # The databases are scratch files. Only the rendered outputs written to results/ are kept.
        tmpd = Path(tmp)
        by_conc = []
        for c in levels:
            r = transfer_load(tmpd / f"load-{c}.db", c, total)
            print(
                f"threads={c:>2} {r['transfers_per_s']:>8} tx/s  p50={r['latency_ms']['p50']}ms "
                f"p99={r['latency_ms']['p99']}ms ok={r['invariants_ok'] and r['money_conserved']}"
            )
            by_conc.append(r)
        # The same single-thread load with fsync only at checkpoints, to show what fsync on every
        # commit costs.
        normal = transfer_load(tmpd / "load-normal.db", 1, total, synchronous="NORMAL")
        lifecycle = lifecycle_and_replay(
            tmpd / "lifecycle.db", LIFECYCLE_OPS_QUICK if args.quick else LIFECYCLE_OPS
        )
        standard = recon_run(
            tmpd / "recon.db",
            days=RECON_DAYS,
            per_day=per_day,
            rates=STANDARD_RATES,
            seed=RECON_STANDARD_SEED,
        )
        stress = recon_run(
            tmpd / "recon-stress.db",
            days=RECON_DAYS,
            per_day=per_day,
            rates=STRESS_RATES,
            seed=RECON_STRESS_SEED,
        )
    metrics = {
        "environment": {
            "generated_at": time.strftime("%Y-%m-%d"),
            "python": platform.python_version(),
            "sqlite": sqlite3.sqlite_version,
            "platform": f"{platform.system()} {platform.release()} {platform.machine()}",
            "quick": args.quick,
        },
        "config": {
            "wallets": WALLETS,
            "transfers_per_level": total,
            "seed": BENCH_SEED,
            "storage": "SQLite WAL, synchronous=FULL, BEGIN IMMEDIATE per operation",
        },
        "headline": _headline(by_conc, lifecycle, standard, stress),
        "transfers": {"by_concurrency": by_conc, "synchronous_normal": normal},
        "lifecycle": lifecycle,
        "reconciliation": {"standard": standard, "stress": stress},
    }
    write_outputs(metrics, RESULTS)
    print(json.dumps(metrics["headline"], indent=2))
    return metrics


if __name__ == "__main__":
    main()
