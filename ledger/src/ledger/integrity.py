"""Integrity verification: the books balance and the cache matches the journal.

:func:`read_snapshot` reads everything the check needs inside one read
transaction (one consistent snapshot); :func:`evaluate` is pure. The check:

* trial balance: total debits equal total credits in every currency;
* cache: every account's four cached totals equal totals re-derived from
  ``postings`` and open (``pending``) payments, ignoring the cached columns;
* overdraft: no account that disallows negatives has a negative available balance.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, TypeAlias

from ledger.balances import (
    PENDING_CREDITS,
    PENDING_DEBITS,
    Totals,
    compute_balances,
    posted_index,
    row_totals,
    zero_delta,
)
from ledger.domain import Direction

TrialBalance: TypeAlias = dict[str, dict[str, int]]


@dataclass(frozen=True, slots=True)
class IntegritySnapshot:
    trial_balance: TrialBalance
    derived_totals: dict[str, Totals]
    accounts: list[sqlite3.Row]


def trial_balance(conn: sqlite3.Connection) -> TrialBalance:
    """Total debits, credits and ``net`` per currency over the whole journal."""
    rows = conn.execute(
        "SELECT currency, "
        "COALESCE(SUM(CASE direction WHEN 'debit' THEN amount END), 0) AS debits, "
        "COALESCE(SUM(CASE direction WHEN 'credit' THEN amount END), 0) AS credits "
        "FROM postings GROUP BY currency ORDER BY currency"
    ).fetchall()
    return {
        r["currency"]: {
            "debits": r["debits"],
            "credits": r["credits"],
            "net": r["debits"] - r["credits"],
        }
        for r in rows
    }


def derived_totals(conn: sqlite3.Connection) -> dict[str, Totals]:
    """Recompute every account's totals from the journal and open holds."""
    totals = {r["id"]: zero_delta() for r in conn.execute("SELECT id FROM accounts")}
    for r in conn.execute(
        "SELECT account_id, direction, SUM(amount) AS s FROM postings "
        "GROUP BY account_id, direction"
    ):
        totals[r["account_id"]][posted_index(Direction(r["direction"]))] = r["s"]
    for r in conn.execute(
        "SELECT source_account_id AS a, SUM(amount) AS s FROM payments "
        "WHERE status = 'pending' GROUP BY a"
    ):
        totals[r["a"]][PENDING_DEBITS] += r["s"]
    for r in conn.execute(
        "SELECT destination_account_id AS a, SUM(amount) AS s FROM payments "
        "WHERE status = 'pending' GROUP BY a"
    ):
        totals[r["a"]][PENDING_CREDITS] += r["s"]
    return {k: (v[0], v[1], v[2], v[3]) for k, v in totals.items()}


def read_snapshot(conn: sqlite3.Connection) -> IntegritySnapshot:
    """Everything :func:`evaluate` needs; call inside a single transaction."""
    return IntegritySnapshot(
        trial_balance=trial_balance(conn),
        derived_totals=derived_totals(conn),
        accounts=conn.execute("SELECT * FROM accounts").fetchall(),
    )


def evaluate(snapshot: IntegritySnapshot) -> dict[str, Any]:
    """The verification report; ``ok`` is true only if every check passes."""
    tb, derived, rows = snapshot.trial_balance, snapshot.derived_totals, snapshot.accounts
    cache_mismatches = sorted(r["id"] for r in rows if row_totals(r) != derived[r["id"]])
    overdrafts = sorted(
        r["id"]
        for r in rows
        if not r["allow_negative"]
        and compute_balances(r["type"], *derived[r["id"]])["available"] < 0
    )
    unbalanced = sorted(c for c, v in tb.items() if v["net"] != 0)
    return {
        "ok": not (cache_mismatches or overdrafts or unbalanced),
        "trial_balance": tb,
        "unbalanced_currencies": unbalanced,
        "cache_mismatches": cache_mismatches,
        "disallowed_overdrafts": overdrafts,
        "accounts_checked": len(rows),
    }
