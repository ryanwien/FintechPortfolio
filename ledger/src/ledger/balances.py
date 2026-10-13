"""Cached account balances: arithmetic, hold deltas and the guarded update.

Each account row caches four running totals, always in this order::

    (posted_debits, posted_credits, pending_debits, pending_credits)

A :data:`Delta` is a change to those four totals. :func:`apply_deltas` is the
only code that writes them; it enforces, inside the caller's write transaction:

* no total ever goes negative (a hold can't be over-released),
* the overdraft rule on the *available* balance (unless ``allow_negative``),
* a compare-and-set on ``lock_version`` (optimistic concurrency),

and visits accounts in sorted id order, a stable lock order should this ever
move to row-level locking on Postgres.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import Final, TypeAlias

from ledger.domain import (
    AccountType,
    ConcurrencyConflictError,
    Direction,
    InsufficientFundsError,
    ValidationError,
)

# (posted_debits, posted_credits, pending_debits, pending_credits)
Delta: TypeAlias = list[int]
Totals: TypeAlias = tuple[int, int, int, int]

POSTED_DEBITS: Final = 0
POSTED_CREDITS: Final = 1
PENDING_DEBITS: Final = 2
PENDING_CREDITS: Final = 3


def zero_delta() -> Delta:
    """A fresh no-op delta."""
    return [0, 0, 0, 0]


def posted_index(direction: Direction) -> int:
    """Index of the posted total that a posting in ``direction`` increases."""
    return POSTED_DEBITS if direction is Direction.DEBIT else POSTED_CREDITS


def hold_deltas(source_id: str, destination_id: str, amount: int) -> dict[str, Delta]:
    """Place an authorization hold: pending debit on source, pending credit on destination."""
    return {source_id: [0, 0, amount, 0], destination_id: [0, 0, 0, amount]}


def release_hold_deltas(source_id: str, destination_id: str, amount: int) -> dict[str, Delta]:
    """Release a hold placed by :func:`hold_deltas` (on capture or failure)."""
    return {source_id: [0, 0, -amount, 0], destination_id: [0, 0, 0, -amount]}


def row_totals(row: sqlite3.Row) -> Totals:
    """The four cached totals of an ``accounts`` row."""
    return (
        row["posted_debits"],
        row["posted_credits"],
        row["pending_debits"],
        row["pending_credits"],
    )


def compute_balances(
    account_type: AccountType | str,
    posted_debits: int,
    posted_credits: int,
    pending_debits: int,
    pending_credits: int,
) -> dict[str, int]:
    """Balances in the account's normal direction.

    * ``posted``: settled journal postings only.
    * ``pending``: posted plus every open authorization, in both directions.
    * ``available``: posted minus open *outbound* authorizations. Inbound holds
      are not spendable until they post. This is the number overdraft rules use.
    """
    if AccountType(account_type).normal_side is Direction.DEBIT:
        posted = posted_debits - posted_credits
        pending = posted + pending_debits - pending_credits
        available = posted - pending_credits
    else:
        posted = posted_credits - posted_debits
        pending = posted + pending_credits - pending_debits
        available = posted - pending_debits
    return {"posted": posted, "pending": pending, "available": available}


def apply_deltas(
    conn: sqlite3.Connection,
    deltas: Mapping[str, Delta],
    accounts: Mapping[str, sqlite3.Row],
    expected_lock_versions: Mapping[str, int] | None = None,
) -> None:
    """Apply cached-balance deltas with overdraft and ``lock_version`` checks.

    ``accounts`` must hold the rows read in the *same* write transaction. Checks
    run per account in sorted id order; when one fails, the caller's transaction
    rolls back the updates already made to earlier accounts.
    """
    for acct_id in sorted(deltas):
        row = accounts[acct_id]
        delta = deltas[acct_id]
        expected = (expected_lock_versions or {}).get(acct_id)
        if expected is not None and expected != row["lock_version"]:
            raise ConcurrencyConflictError(
                f"account {acct_id} is at lock_version {row['lock_version']}, expected {expected}"
            )
        old = row_totals(row)
        new = tuple(o + x for o, x in zip(old, delta, strict=True))
        if min(new) < 0:
            raise ValidationError(f"account {acct_id} totals would go negative (hold over-release)")
        if not row["allow_negative"]:
            before = compute_balances(row["type"], *old)["available"]
            after = compute_balances(row["type"], *new)["available"]
            if after < 0 and after < before:
                raise InsufficientFundsError(
                    f"account {acct_id} has available balance {before} {row['currency']}; "
                    f"operation would take it to {after}"
                )
        cur = conn.execute(
            "UPDATE accounts SET posted_debits = ?, posted_credits = ?, pending_debits = ?, "
            "pending_credits = ?, lock_version = lock_version + 1 "
            "WHERE id = ? AND lock_version = ?",
            (*new, acct_id, row["lock_version"]),
        )
        if cur.rowcount != 1:  # pragma: no cover - impossible under BEGIN IMMEDIATE
            raise ConcurrencyConflictError(f"account {acct_id} changed concurrently")
