"""Payment lifecycle: authorization holds, capture, failure and reversal.

Every method runs inside the caller's write transaction and moves a payment
through :mod:`ledger.lifecycle` with a compare-and-set on ``status``, appending
an audit row to ``payment_events`` for each transition.

* authorize (``capture=False``): places a hold, writes nothing to the journal;
* capture: posts the entry *and* releases the hold in one balance update, so
  the source's available balance is unchanged and a held payment can always settle;
* fail: releases the hold, writes nothing to the journal;
* reverse: writes a reversing entry, subject to the overdraft rule.
"""

from __future__ import annotations

import sqlite3

from ledger import repository
from ledger.balances import apply_deltas, hold_deltas, release_hold_deltas
from ledger.clock import Clock
from ledger.domain import (
    ConcurrencyConflictError,
    CurrencyMismatchError,
    Direction,
    PostingSpec,
)
from ledger.journal import Journal
from ledger.lifecycle import PaymentStatus, assert_transition


def payment_legs(src: str, dst: str, amount: int, currency: str) -> list[PostingSpec]:
    """The two postings of a payment: debit the source, credit the destination."""
    return [
        PostingSpec(src, Direction.DEBIT, amount, currency),
        PostingSpec(dst, Direction.CREDIT, amount, currency),
    ]


class PaymentLifecycle:
    """State transitions of payments and their journal / hold side effects."""

    def __init__(self, clock: Clock, journal: Journal) -> None:
        """Bind the clock used for timestamps and the journal used for entry writes."""
        self._clock = clock
        self._journal = journal

    def create(
        self,
        conn: sqlite3.Connection,
        source_account_id: str,
        destination_account_id: str,
        amount: int,
        currency: str,
        *,
        capture: bool,
        description: str,
        external_ref: str | None,
        effective_date: str | None,
    ) -> str:
        """Insert a payment and either post it immediately or place a hold."""
        # Load both accounts once. The currency check and the hold below use the same rows.
        accounts = repository.load_accounts(conn, [source_account_id, destination_account_id])
        for a in accounts.values():
            if a["currency"] != currency:
                raise CurrencyMismatchError(
                    f"account {a['id']} is denominated in {a['currency']}, not {currency}"
                )
        # The payment row and its first event are written before any money moves, so the audit trail
        # starts at pending.
        pid = repository.new_id("pay")
        repository.insert_payment(
            conn,
            pid,
            source_account_id,
            destination_account_id,
            amount,
            currency,
            description=description,
            external_ref=external_ref,
            now=self._clock.timestamp(),
        )
        self._record_event(conn, pid, None, PaymentStatus.PENDING, None, None)
        # Immediate capture posts now. A held payment only reserves funds until it is captured or
        # failed.
        if capture:
            entry_id = self._journal.write_entry(
                conn,
                payment_legs(source_account_id, destination_account_id, amount, currency),
                description=description,
                effective_date=effective_date,
                external_ref=external_ref,
                payment_id=pid,
            )
            self._transition(
                conn, pid, PaymentStatus.PENDING, PaymentStatus.POSTED, entry_id=entry_id
            )
        else:
            # Authorization: only the pending totals move. Nothing is journaled until the payment is
            # captured.
            apply_deltas(
                conn, hold_deltas(source_account_id, destination_account_id, amount), accounts
            )
        return pid

    def capture(
        self, conn: sqlite3.Connection, payment_id: str, *, effective_date: str | None
    ) -> None:
        """Post a held payment's entry and release its hold atomically."""
        pay = repository.load_payment(conn, payment_id)
        # Check the state machine before writing anything. Only a pending payment can be captured.
        assert_transition(pay["status"], PaymentStatus.POSTED)
        src, dst, amt = pay["source_account_id"], pay["destination_account_id"], pay["amount"]
        entry_id = self._journal.write_entry(
            conn,
            payment_legs(src, dst, amt, pay["currency"]),
            description=pay["description"],
            effective_date=effective_date,
            external_ref=pay["external_ref"],
            payment_id=payment_id,
            # The hold release goes in the same balance update as the postings, so funds are never
            # counted as both held and posted.
            extra_deltas=release_hold_deltas(src, dst, amt),
        )
        self._transition(
            conn, payment_id, PaymentStatus.PENDING, PaymentStatus.POSTED, entry_id=entry_id
        )

    def fail(self, conn: sqlite3.Connection, payment_id: str, *, reason: str) -> None:
        """Release a held payment's hold; nothing is written to the journal."""
        pay = repository.load_payment(conn, payment_id)
        # Failing is valid only while pending. A posted payment must be reversed instead.
        assert_transition(pay["status"], PaymentStatus.FAILED)
        src, dst, amt = pay["source_account_id"], pay["destination_account_id"], pay["amount"]
        # Only the hold is released. Failing writes nothing to the journal, so the posted totals do
        # not change.
        apply_deltas(
            conn, release_hold_deltas(src, dst, amt), repository.load_accounts(conn, [src, dst])
        )
        self._transition(
            conn, payment_id, PaymentStatus.PENDING, PaymentStatus.FAILED, reason=reason
        )

    def reverse(self, conn: sqlite3.Connection, pay: sqlite3.Row, description: str | None) -> str:
        """Reverse a posted payment's entry; returns the reversal entry id."""
        assert_transition(pay["status"], PaymentStatus.REVERSED)
        # Journal the reversal first, then move the status. Both happen in one transaction, so they
        # succeed or fail together.
        rev_id = self._journal.write_reversal(conn, pay["entry_id"], description, pay["id"])
        self._transition(
            conn,
            pay["id"],
            PaymentStatus.POSTED,
            PaymentStatus.REVERSED,
            reversal_entry_id=rev_id,
        )
        return rev_id

    def _transition(
        self,
        conn: sqlite3.Connection,
        payment_id: str,
        current: PaymentStatus,
        target: PaymentStatus,
        *,
        entry_id: str | None = None,
        reversal_entry_id: str | None = None,
        reason: str | None = None,
    ) -> None:
        """Move a payment from ``current`` to ``target`` and record the transition.

        Validates the transition, then compare-and-sets the status. Raises
        ConcurrencyConflictError if the payment is no longer in ``current``.
        """
        # Reject an illegal move before the UPDATE, so only valid transitions reach the database.
        assert_transition(current, target)
        changed = repository.update_payment_status(
            conn,
            payment_id,
            current,
            target,
            updated_at=self._clock.timestamp(),
            entry_id=entry_id,
            reversal_entry_id=reversal_entry_id,
            reason=reason,
        )
        if not changed:  # compare-and-set on status
            raise ConcurrencyConflictError(f"payment {payment_id} is no longer {current}")
        # Reached only after a successful compare-and-set, so every audit row follows a real status
        # change.
        self._record_event(conn, payment_id, current, target, entry_id or reversal_entry_id, reason)

    def _record_event(
        self,
        conn: sqlite3.Connection,
        payment_id: str,
        from_status: PaymentStatus | None,
        to_status: PaymentStatus,
        entry_id: str | None,
        detail: str | None,
    ) -> None:
        """Append one row to the payment's audit trail in ``payment_events``."""
        repository.insert_payment_event(
            conn,
            payment_id,
            from_status,
            to_status,
            entry_id=entry_id,
            detail=detail,
            created_at=self._clock.timestamp(),
        )
