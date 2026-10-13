"""Threads hammer one file-backed ledger with transfers, holds and retries.

Each logical request carries an idempotency key and is sent 1-3 times, some
retries racing the original from another thread. Afterwards:

* money is conserved: wallet balances sum to exactly what was deposited,
* every logical request was applied at most once (payments == distinct keys
  that succeeded) and all copies of a request saw the same payment id,
* no wallet is overdrawn, the trial balance is zero, caches match the journal.
"""

from __future__ import annotations

import random
import threading
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ledger.domain import IdempotencyKey, LedgerError
from ledger.service import Ledger

Fund = Callable[..., dict[str, Any]]

THREADS = 8
REQUESTS = 600
WALLETS = 6
START = 5_000


def test_threads_conserve_money_and_apply_each_key_once(tmp_path: Path, fund: Fund) -> None:
    lg = Ledger(tmp_path / "hammer.db", synchronous="NORMAL")
    bank = lg.create_account("Bank", "asset", "USD")["id"]
    wallets = [lg.create_account(f"w{i}", "liability", "USD")["id"] for i in range(WALLETS)]
    for w in wallets:
        fund(lg, bank, w, START)

    rng = random.Random(1234)
    # Small balances + large amounts => many overdraft attempts race each other.
    requests = []
    for n in range(REQUESTS):
        src, dst = rng.sample(wallets, 2)
        requests.append((f"req-{n}", src, dst, rng.randint(1, 2_500), rng.random() < 0.3))
    # Each request is delivered 1-3 times, shuffled so retries interleave across threads.
    deliveries = [r for r in requests for _ in range(rng.randint(1, 3))]
    rng.shuffle(deliveries)

    results: dict[str, set[str]] = defaultdict(set)
    rejected: dict[str, int] = defaultdict(int)
    lock = threading.Lock()
    errors: list[BaseException] = []
    queue = iter(deliveries)
    qlock = threading.Lock()

    def worker() -> None:
        try:
            while True:
                with qlock:
                    item = next(queue, None)
                if item is None:
                    return
                key, src, dst, amt, hold = item
                try:
                    p = lg.create_payment(
                        src, dst, amt, "USD", capture=not hold, idempotency=IdempotencyKey(key)
                    )
                    with lock:
                        results[key].add(p["id"])
                except LedgerError as exc:
                    with lock:
                        rejected[type(exc).__name__] += 1
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors

    # Every successful key resolved to exactly one payment id across all its deliveries.
    assert all(len(ids) == 1 for ids in results.values())
    assert set(rejected) <= {"InsufficientFundsError"}
    assert rejected["InsufficientFundsError"] > 0, "test should exercise the overdraft race"

    # Now settle every pending hold concurrently: half capture, half fail.
    pending = []
    for ids in results.values():
        pid = next(iter(ids))
        if lg.get_payment(pid)["status"] == "pending":
            pending.append(pid)
    assert pending

    def settle(chunk: list[str], tid: int) -> None:
        for i, pid in enumerate(chunk):
            for _ in range(2):  # duplicate delivery of the same settlement command
                try:
                    if (i + tid) % 2:
                        lg.capture_payment(pid, idempotency=IdempotencyKey(f"settle-{pid}"))
                    else:
                        lg.fail_payment(pid, idempotency=IdempotencyKey(f"settle-{pid}"))
                except LedgerError as exc:  # pragma: no cover
                    errors.append(exc)

    chunks = [pending[i::4] for i in range(4)]
    ts = [threading.Thread(target=settle, args=(c, i)) for i, c in enumerate(chunks)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errors

    report = lg.verify()
    assert report["ok"], report
    with lg._tx(write=False) as conn:
        n_payments = conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0]
        n_open = conn.execute("SELECT COUNT(*) FROM payments WHERE status = 'pending'").fetchone()[
            0
        ]
    assert n_payments == len(results)  # retries never created a second payment
    assert n_open == 0

    balances = [lg.get_balance(w) for w in wallets]
    assert sum(b["posted"] for b in balances) == START * WALLETS  # conservation of money
    assert all(b["available"] >= 0 for b in balances)
    assert lg.get_balance(bank)["posted"] == START * WALLETS
    lg.close()


def test_concurrent_double_reversal_only_one_wins(tmp_path: Path, fund: Fund) -> None:
    lg = Ledger(tmp_path / "rev.db", synchronous="NORMAL")
    bank = lg.create_account("Bank", "asset", "USD")["id"]
    a = lg.create_account("a", "liability", "USD")["id"]
    b = lg.create_account("b", "liability", "USD")["id"]
    fund(lg, bank, a, 1_000)
    p = lg.create_payment(a, b, 1_000, "USD")
    outcomes: list[str] = []
    barrier = threading.Barrier(8)

    def go() -> None:
        barrier.wait()
        try:
            lg.reverse_payment(p["id"])
            outcomes.append("ok")
        except LedgerError as exc:
            outcomes.append(exc.code)

    ts = [threading.Thread(target=go) for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert outcomes.count("ok") == 1
    assert set(outcomes) - {"ok"} == {"illegal_state_transition"}
    assert lg.get_balance(a)["posted"] == 1_000
    assert lg.get_balance(b)["posted"] == 0
    assert lg.verify()["ok"]
    lg.close()
