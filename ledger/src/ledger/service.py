"""Ledger service facade: the public API and the only entry point that writes.

Every mutating operation runs inside one ``BEGIN IMMEDIATE`` transaction
(:meth:`ledger.storage.Database.transaction`), which takes SQLite's write lock
up front. That is the pessimistic half of the concurrency story: two transfers
touching the same account are serialized, so the overdraft check and the balance
update can never interleave. The optimistic half is ``lock_version`` on each
account: every cached-balance update is a compare-and-set on the version read in
the same transaction, and callers may pass ``expected_lock_versions`` to fail
fast (409) if an account changed since they last read it.

Idempotency records are written in the same transaction as the effect they
describe (:mod:`ledger.idempotency`). The work itself is delegated to
:mod:`ledger.journal` (entries, reversals), :mod:`ledger.payments` (lifecycle and
holds), :mod:`ledger.integrity` (verification) and :mod:`ledger.repository`
(rows and views); this class only validates input, fixes the transaction
boundary and composes them.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from datetime import datetime
from pathlib import Path
from typing import Any, Final, TypeAlias

from ledger import idempotency as idem
from ledger import integrity, repository
from ledger.balances import compute_balances
from ledger.clock import Clock, utcnow
from ledger.domain import (
    MAX_ACCOUNT_NAME_LENGTH,
    AccountType,
    Direction,
    IdempotencyKey,
    IllegalTransitionError,
    PostingSpec,
    ValidationError,
    validate_amount,
    validate_currency,
)
from ledger.journal import Journal, coerce_postings
from ledger.payments import PaymentLifecycle
from ledger.storage import DEFAULT_BUSY_TIMEOUT_S, DEFAULT_SYNCHRONOUS, MEMORY_PATH, Database

__all__ = ["Ledger", "compute_balances"]

DEFAULT_PAGE_SIZE: Final = 50
MAX_PAGE_SIZE: Final = 500

Operation: TypeAlias = Callable[[sqlite3.Connection], dict[str, Any]]


class Ledger:
    """Double-entry ledger backed by SQLite.

    ``path`` may be ``":memory:"`` (single shared connection, operations
    serialized with a lock; used by property-based tests) or a file path
    (WAL mode, one connection per thread; used by the API and benchmarks).
    """

    def __init__(
        self,
        path: str | Path = MEMORY_PATH,
        *,
        synchronous: str = DEFAULT_SYNCHRONOUS,
        clock: Callable[[], datetime] = utcnow,
        busy_timeout_s: float = DEFAULT_BUSY_TIMEOUT_S,
    ) -> None:
        self._db = Database(path, synchronous=synchronous, busy_timeout_s=busy_timeout_s)
        self.path = self._db.path
        self._clock = Clock(clock)
        self._journal = Journal(self._clock)
        self._payments = PaymentLifecycle(self._clock, self._journal)

    # ------------------------------------------------------------------ plumbing

    def _tx(self, *, write: bool = True) -> AbstractContextManager[sqlite3.Connection]:
        """A transaction on this thread's connection (see ``Database.transaction``)."""
        return self._db.transaction(write=write)

    def close(self) -> None:
        self._db.close()

    def _execute(
        self,
        op: str,
        params: Mapping[str, Any],
        idempotency: IdempotencyKey | None,
        fn: Operation,
    ) -> dict[str, Any]:
        """Run ``fn`` in a write transaction, honouring an idempotency key.

        Same key + same (op, params) -> the stored response, nothing re-executed.
        Same key + different (op, params) -> IdempotencyConflictError (HTTP 409).
        Failed operations roll back and store nothing, so a client may retry the
        same key after, say, funding an account.
        """
        if idempotency is None:
            with self._tx() as conn:
                return fn(conn)
        idem.validate_key(idempotency)
        with self._tx() as conn:
            fp = idem.fingerprint(op, params)
            stored = idem.stored_response(conn, idempotency, fp)
            if stored is not None:
                return stored
            result = fn(conn)
            idem.record_response(conn, idempotency, fp, result, created_at=self._clock.timestamp())
            return result

    # ------------------------------------------------------------------ accounts

    def create_account(
        self,
        name: str,
        type: AccountType | str,  # mirrors the API field name
        currency: str,
        *,
        allow_negative: bool = False,
        idempotency: IdempotencyKey | None = None,
    ) -> dict[str, Any]:
        try:
            acct_type = AccountType(type)
        except ValueError as exc:
            raise ValidationError(f"unknown account type {type!r}") from exc
        validate_currency(currency)
        if not name or len(name) > MAX_ACCOUNT_NAME_LENGTH:
            raise ValidationError(f"name must be 1-{MAX_ACCOUNT_NAME_LENGTH} characters")
        params = {
            "name": name,
            "type": acct_type.value,
            "currency": currency,
            "allow_negative": allow_negative,
        }

        def op(conn: sqlite3.Connection) -> dict[str, Any]:
            acct_id = repository.new_id("acct")
            repository.insert_account(
                conn,
                acct_id,
                name,
                acct_type,
                currency,
                allow_negative=allow_negative,
                created_at=self._clock.timestamp(),
            )
            return repository.account_view(repository.load_account(conn, acct_id))

        return self._execute("create_account", params, idempotency, op)

    def get_account(self, account_id: str) -> dict[str, Any]:
        with self._tx(write=False) as conn:
            return repository.account_view(repository.load_account(conn, account_id))

    def get_balance(self, account_id: str) -> dict[str, Any]:
        """Posted, pending and available balances plus the current ``lock_version``."""
        acct = self.get_account(account_id)
        return {
            "account_id": account_id,
            "currency": acct["currency"],
            **acct["balances"],
            "lock_version": acct["lock_version"],
        }

    def list_accounts(self) -> list[dict[str, Any]]:
        with self._tx(write=False) as conn:
            return [repository.account_view(r) for r in repository.list_account_rows(conn)]

    def account_activity(
        self, account_id: str, *, start: str | None = None, end: str | None = None
    ) -> list[dict[str, Any]]:
        """Postings to one account with entry metadata, oldest first.

        Signed ``amount`` is positive when the account's normal balance increases.
        """
        with self._tx(write=False) as conn:
            acct = repository.load_account(conn, account_id)
            sign = 1 if AccountType(acct["type"]).normal_side is Direction.DEBIT else -1
            rows = repository.account_activity_rows(conn, account_id, start=start, end=end)
        return [
            {
                "entry_id": r["entry_id"],
                "effective_date": r["effective_date"],
                "external_ref": r["external_ref"],
                "description": r["description"],
                "amount": sign * (r["amount"] if r["direction"] == "debit" else -r["amount"]),
                "currency": r["currency"],
            }
            for r in rows
        ]

    # ------------------------------------------------------------------- journal

    def post_entry(
        self,
        postings: Sequence[PostingSpec | Mapping[str, Any]],
        *,
        description: str = "",
        effective_date: str | None = None,
        external_ref: str | None = None,
        expected_lock_versions: Mapping[str, int] | None = None,
        idempotency: IdempotencyKey | None = None,
    ) -> dict[str, Any]:
        """Append one balanced journal entry (two or more postings)."""
        specs = coerce_postings(postings)
        params = {
            "postings": [[p.account_id, p.direction.value, p.amount, p.currency] for p in specs],
            "description": description,
            "effective_date": effective_date,
            "external_ref": external_ref,
            "expected_lock_versions": dict(expected_lock_versions or {}),
        }

        def op(conn: sqlite3.Connection) -> dict[str, Any]:
            entry_id = self._journal.write_entry(
                conn,
                specs,
                description=description,
                effective_date=effective_date,
                external_ref=external_ref,
                expected_lock_versions=expected_lock_versions,
            )
            return repository.entry_view(conn, entry_id)

        return self._execute("post_entry", params, idempotency, op)

    def get_entry(self, entry_id: str) -> dict[str, Any]:
        with self._tx(write=False) as conn:
            return repository.entry_view(conn, entry_id)

    def list_entries(
        self,
        *,
        account_id: str | None = None,
        limit: int = DEFAULT_PAGE_SIZE,
        cursor: int | None = None,
    ) -> dict[str, Any]:
        """Newest first, keyset-paginated on the journal sequence number."""
        if not 1 <= limit <= MAX_PAGE_SIZE:
            raise ValidationError(f"limit must be between 1 and {MAX_PAGE_SIZE}")
        with self._tx(write=False) as conn:
            if account_id is not None:
                repository.load_account(conn, account_id)
            rows = repository.entry_page_rows(
                conn, account_id=account_id, cursor=cursor, fetch=limit + 1
            )
            has_more = len(rows) > limit
            rows = rows[:limit]
            postings = repository.postings_for(conn, [r["id"] for r in rows])
            data = [repository.entry_dict(r, postings.get(r["id"], []), None) for r in rows]
        return {
            "data": data,
            "has_more": has_more,
            "next_cursor": rows[-1]["seq"] if has_more and rows else None,
        }

    def reverse_entry(
        self,
        entry_id: str,
        *,
        description: str | None = None,
        idempotency: IdempotencyKey | None = None,
    ) -> dict[str, Any]:
        """Write a mirror-image entry. The original is never modified.

        Entries that belong to a payment are reversed through the payment state
        machine so the payment and the journal cannot disagree.
        """

        def op(conn: sqlite3.Connection) -> dict[str, Any]:
            entry = repository.load_entry(conn, entry_id)
            if entry["payment_id"] is not None:
                pay = repository.load_payment(conn, entry["payment_id"])
                if pay["entry_id"] != entry_id:
                    raise IllegalTransitionError(
                        "entry is itself a payment reversal and cannot be reversed"
                    )
                rev_id = self._payments.reverse(conn, pay, description)
            else:
                rev_id = self._journal.write_reversal(conn, entry_id, description, None)
            return repository.entry_view(conn, rev_id)

        params = {"entry_id": entry_id, "description": description}
        return self._execute("reverse_entry", params, idempotency, op)

    # ------------------------------------------------------------------ payments

    def create_payment(
        self,
        source_account_id: str,
        destination_account_id: str,
        amount: int,
        currency: str,
        *,
        capture: bool = True,
        description: str = "",
        external_ref: str | None = None,
        effective_date: str | None = None,
        idempotency: IdempotencyKey | None = None,
    ) -> dict[str, Any]:
        """Move ``amount`` from source to destination (debit source, credit destination).

        ``capture=False`` only authorizes: funds are held against the source's
        available balance until ``capture_payment``/``fail_payment``.
        """
        validate_amount(amount)
        validate_currency(currency)
        if source_account_id == destination_account_id:
            raise ValidationError("source and destination must differ")
        params = {
            "source": source_account_id,
            "destination": destination_account_id,
            "amount": amount,
            "currency": currency,
            "capture": capture,
            "description": description,
            "external_ref": external_ref,
            "effective_date": effective_date,
        }

        def op(conn: sqlite3.Connection) -> dict[str, Any]:
            pid = self._payments.create(
                conn,
                source_account_id,
                destination_account_id,
                amount,
                currency,
                capture=capture,
                description=description,
                external_ref=external_ref,
                effective_date=effective_date,
            )
            return repository.payment_view(conn, pid)

        return self._execute("create_payment", params, idempotency, op)

    def capture_payment(
        self,
        payment_id: str,
        *,
        effective_date: str | None = None,
        idempotency: IdempotencyKey | None = None,
    ) -> dict[str, Any]:
        """Post a held payment: write its entry and release the hold atomically."""

        def op(conn: sqlite3.Connection) -> dict[str, Any]:
            self._payments.capture(conn, payment_id, effective_date=effective_date)
            return repository.payment_view(conn, payment_id)

        params = {"payment_id": payment_id, "effective_date": effective_date}
        return self._execute("capture_payment", params, idempotency, op)

    def fail_payment(
        self, payment_id: str, *, reason: str = "", idempotency: IdempotencyKey | None = None
    ) -> dict[str, Any]:
        """Release a held payment's funds without touching the journal."""

        def op(conn: sqlite3.Connection) -> dict[str, Any]:
            self._payments.fail(conn, payment_id, reason=reason)
            return repository.payment_view(conn, payment_id)

        params = {"payment_id": payment_id, "reason": reason}
        return self._execute("fail_payment", params, idempotency, op)

    def reverse_payment(
        self,
        payment_id: str,
        *,
        description: str | None = None,
        idempotency: IdempotencyKey | None = None,
    ) -> dict[str, Any]:
        """Reverse a posted payment with a mirror-image journal entry."""

        def op(conn: sqlite3.Connection) -> dict[str, Any]:
            self._payments.reverse(conn, repository.load_payment(conn, payment_id), description)
            return repository.payment_view(conn, payment_id)

        params = {"payment_id": payment_id, "description": description}
        return self._execute("reverse_payment", params, idempotency, op)

    def get_payment(self, payment_id: str) -> dict[str, Any]:
        with self._tx(write=False) as conn:
            return repository.payment_view(conn, payment_id)

    # --------------------------------------------------------------- invariants

    def trial_balance(self) -> dict[str, dict[str, int]]:
        """Total debits and credits per currency; ``net`` must be zero for each."""
        with self._tx(write=False) as conn:
            return integrity.trial_balance(conn)

    def verify(self) -> dict[str, Any]:
        """Full integrity check on one consistent snapshot.

        Covers the trial balance per currency, cached balances vs. balances
        re-derived from the journal, and disallowed overdrafts.
        """
        with self._tx(write=False) as conn:
            snapshot = integrity.read_snapshot(conn)
        return integrity.evaluate(snapshot)
