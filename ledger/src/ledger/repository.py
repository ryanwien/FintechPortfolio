"""Persistence: row loaders, inserts and the JSON views returned by the API.

Every function takes an open connection and runs inside the caller's
transaction; nothing here begins or commits. Loaders raise
:class:`~ledger.domain.NotFoundError` for missing rows so callers never handle
``None``. Views are plain dicts because they are also what idempotency records
store and replay.
"""

from __future__ import annotations

import sqlite3
import uuid
from collections import defaultdict
from collections.abc import Sequence
from typing import Any, Final

from ledger.balances import compute_balances, row_totals
from ledger.domain import AccountType, Direction, NotFoundError, PostingSpec
from ledger.lifecycle import PaymentStatus

ID_HEX_LENGTH: Final = 24


def new_id(prefix: str) -> str:
    """Random, prefixed public id such as ``acct_<24 hex>``."""
    return f"{prefix}_{uuid.uuid4().hex[:ID_HEX_LENGTH]}"


def _placeholders(n: int) -> str:
    return ",".join("?" * n)


# ------------------------------------------------------------------ accounts


def insert_account(
    conn: sqlite3.Connection,
    acct_id: str,
    name: str,
    acct_type: AccountType,
    currency: str,
    *,
    allow_negative: bool,
    created_at: str,
) -> None:
    conn.execute(
        "INSERT INTO accounts (id, name, type, currency, allow_negative, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (acct_id, name, acct_type.value, currency, int(allow_negative), created_at),
    )


def load_account(conn: sqlite3.Connection, account_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"account {account_id} not found")
    return row


def load_accounts(conn: sqlite3.Connection, ids: Sequence[str]) -> dict[str, sqlite3.Row]:
    """Load several accounts by id; raises for the first missing id in sorted order."""
    uniq = sorted(set(ids))
    rows = conn.execute(
        f"SELECT * FROM accounts WHERE id IN ({_placeholders(len(uniq))})", uniq
    ).fetchall()
    found = {r["id"]: r for r in rows}
    missing = [i for i in uniq if i not in found]
    if missing:
        raise NotFoundError(f"account {missing[0]} not found")
    return found


def list_account_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM accounts ORDER BY created_at, id").fetchall()


def account_view(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "type": row["type"],
        "currency": row["currency"],
        "allow_negative": bool(row["allow_negative"]),
        "normal_balance": AccountType(row["type"]).normal_side.value,
        "balances": compute_balances(row["type"], *row_totals(row)),
        "lock_version": row["lock_version"],
        "created_at": row["created_at"],
    }


def account_activity_rows(
    conn: sqlite3.Connection, account_id: str, *, start: str | None, end: str | None
) -> list[sqlite3.Row]:
    """Postings to one account joined with entry metadata, oldest first."""
    sql = (
        "SELECT e.id AS entry_id, e.effective_date, e.external_ref, e.description, "
        "p.direction, p.amount, p.currency FROM postings p JOIN entries e ON e.id = p.entry_id "
        "WHERE p.account_id = ?"
    )
    args: list[Any] = [account_id]
    if start:
        sql += " AND e.effective_date >= ?"
        args.append(start)
    if end:
        sql += " AND e.effective_date <= ?"
        args.append(end)
    return conn.execute(sql + " ORDER BY p.id", args).fetchall()


# ------------------------------------------------------------------- journal


def insert_entry(
    conn: sqlite3.Connection,
    entry_id: str,
    postings: Sequence[PostingSpec],
    *,
    description: str,
    effective_date: str,
    created_at: str,
    external_ref: str | None,
    payment_id: str | None,
    reverses_entry_id: str | None,
) -> None:
    """Append an entry and its postings (the journal is insert-only)."""
    conn.execute(
        "INSERT INTO entries (id, description, effective_date, created_at, external_ref, "
        "payment_id, reverses_entry_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            entry_id,
            description,
            effective_date,
            created_at,
            external_ref,
            payment_id,
            reverses_entry_id,
        ),
    )
    conn.executemany(
        "INSERT INTO postings (entry_id, account_id, direction, amount, currency) "
        "VALUES (?, ?, ?, ?, ?)",
        [(entry_id, p.account_id, p.direction.value, p.amount, p.currency) for p in postings],
    )


def entry_is_balanced_in_db(conn: sqlite3.Connection, entry_id: str) -> bool:
    """Re-check per-currency balance from the rows that actually hit the table."""
    unbalanced = conn.execute(
        "SELECT currency FROM postings WHERE entry_id = ? GROUP BY currency "
        "HAVING SUM(CASE direction WHEN 'debit' THEN amount ELSE -amount END) <> 0",
        (entry_id,),
    ).fetchone()
    return unbalanced is None


def load_entry(conn: sqlite3.Connection, entry_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"entry {entry_id} not found")
    return row


def is_reversed(conn: sqlite3.Connection, entry_id: str) -> bool:
    found = conn.execute(
        "SELECT 1 FROM entries WHERE reverses_entry_id = ?", (entry_id,)
    ).fetchone()
    return found is not None


def reversed_postings(conn: sqlite3.Connection, entry_id: str) -> list[PostingSpec]:
    """The legs of ``entry_id`` with every direction flipped, in posting order."""
    legs = conn.execute(
        "SELECT account_id, direction, amount, currency FROM postings "
        "WHERE entry_id = ? ORDER BY id",
        (entry_id,),
    ).fetchall()
    return [
        PostingSpec(r["account_id"], Direction(r["direction"]).flip(), r["amount"], r["currency"])
        for r in legs
    ]


def postings_for(
    conn: sqlite3.Connection, entry_ids: Sequence[str]
) -> dict[str, list[dict[str, Any]]]:
    """Posting views grouped by entry id, in posting order."""
    if not entry_ids:
        return {}
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in conn.execute(
        "SELECT entry_id, account_id, direction, amount, currency FROM postings "
        f"WHERE entry_id IN ({_placeholders(len(entry_ids))}) ORDER BY id",
        list(entry_ids),
    ):
        out[r["entry_id"]].append(
            {
                "account_id": r["account_id"],
                "direction": r["direction"],
                "amount": r["amount"],
                "currency": r["currency"],
            }
        )
    return out


def entry_dict(
    row: sqlite3.Row, postings: list[dict[str, Any]], reversed_by: str | None
) -> dict[str, Any]:
    return {
        "id": row["id"],
        "seq": row["seq"],
        "description": row["description"],
        "effective_date": row["effective_date"],
        "created_at": row["created_at"],
        "external_ref": row["external_ref"],
        "payment_id": row["payment_id"],
        "reverses_entry_id": row["reverses_entry_id"],
        "reversed_by_entry_id": reversed_by,
        "postings": postings,
    }


def entry_view(conn: sqlite3.Connection, entry_id: str) -> dict[str, Any]:
    row = load_entry(conn, entry_id)
    rev = conn.execute("SELECT id FROM entries WHERE reverses_entry_id = ?", (entry_id,)).fetchone()
    return entry_dict(
        row, postings_for(conn, [entry_id]).get(entry_id, []), rev["id"] if rev else None
    )


def entry_page_rows(
    conn: sqlite3.Connection, *, account_id: str | None, cursor: int | None, fetch: int
) -> list[sqlite3.Row]:
    """Up to ``fetch`` entries newest first, keyset-paginated on ``seq < cursor``."""
    sql = "SELECT * FROM entries WHERE 1 = 1"
    args: list[Any] = []
    if account_id is not None:
        sql += " AND id IN (SELECT entry_id FROM postings WHERE account_id = ?)"
        args.append(account_id)
    if cursor is not None:
        sql += " AND seq < ?"
        args.append(cursor)
    sql += " ORDER BY seq DESC LIMIT ?"
    args.append(fetch)
    return conn.execute(sql, args).fetchall()


# ------------------------------------------------------------------ payments


def insert_payment(
    conn: sqlite3.Connection,
    payment_id: str,
    source_account_id: str,
    destination_account_id: str,
    amount: int,
    currency: str,
    *,
    description: str,
    external_ref: str | None,
    now: str,
) -> None:
    """Insert a new payment in ``pending`` status."""
    conn.execute(
        "INSERT INTO payments (id, source_account_id, destination_account_id, amount, currency, "
        "status, description, external_ref, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)",
        (
            payment_id,
            source_account_id,
            destination_account_id,
            amount,
            currency,
            description,
            external_ref,
            now,
            now,
        ),
    )


def load_payment(conn: sqlite3.Connection, payment_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM payments WHERE id = ?", (payment_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"payment {payment_id} not found")
    return row


def update_payment_status(
    conn: sqlite3.Connection,
    payment_id: str,
    current: PaymentStatus,
    target: PaymentStatus,
    *,
    updated_at: str,
    entry_id: str | None,
    reversal_entry_id: str | None,
    reason: str | None,
) -> bool:
    """Compare-and-set the payment's status; ``False`` if it was no longer ``current``."""
    cur = conn.execute(
        "UPDATE payments SET status = ?, updated_at = ?, entry_id = COALESCE(?, entry_id), "
        "reversal_entry_id = COALESCE(?, reversal_entry_id), "
        "failure_reason = COALESCE(?, failure_reason) "
        "WHERE id = ? AND status = ?",
        (
            target.value,
            updated_at,
            entry_id,
            reversal_entry_id,
            reason,
            payment_id,
            current.value,
        ),
    )
    return cur.rowcount == 1


def insert_payment_event(
    conn: sqlite3.Connection,
    payment_id: str,
    from_status: PaymentStatus | None,
    to_status: PaymentStatus,
    *,
    entry_id: str | None,
    detail: str | None,
    created_at: str,
) -> None:
    conn.execute(
        "INSERT INTO payment_events "
        "(payment_id, from_status, to_status, entry_id, detail, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            payment_id,
            from_status.value if from_status else None,
            to_status.value,
            entry_id,
            detail,
            created_at,
        ),
    )


def payment_view(conn: sqlite3.Connection, payment_id: str) -> dict[str, Any]:
    p = load_payment(conn, payment_id)
    events = conn.execute(
        "SELECT from_status, to_status, entry_id, detail, created_at FROM payment_events "
        "WHERE payment_id = ? ORDER BY id",
        (payment_id,),
    ).fetchall()
    return {
        "id": p["id"],
        "source_account_id": p["source_account_id"],
        "destination_account_id": p["destination_account_id"],
        "amount": p["amount"],
        "currency": p["currency"],
        "status": p["status"],
        "description": p["description"],
        "external_ref": p["external_ref"],
        "entry_id": p["entry_id"],
        "reversal_entry_id": p["reversal_entry_id"],
        "failure_reason": p["failure_reason"],
        "created_at": p["created_at"],
        "updated_at": p["updated_at"],
        "events": [dict(e) for e in events],
    }
