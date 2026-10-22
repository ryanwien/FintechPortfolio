"""Journal posting: validation, balanced-entry writes and reversals.

Invariants enforced here, inside the caller's write transaction:

* every entry has at least two postings and balances *per currency*, checked in
  Python before the insert and again in SQL over the rows actually inserted;
* every posting's currency matches its account's currency;
* the journal is append-only: a correction is a new entry that mirrors the
  original with every direction flipped, and an entry is reversed at most once;
* cached balances move in the same transaction as the postings
  (see :func:`ledger.balances.apply_deltas`).
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any, Final

from ledger import repository
from ledger.balances import Delta, apply_deltas, posted_index, zero_delta
from ledger.clock import Clock
from ledger.domain import (
    AlreadyReversedError,
    CurrencyMismatchError,
    Direction,
    PostingSpec,
    UnbalancedEntryError,
    ValidationError,
    validate_amount,
    validate_currency,
)

MIN_POSTINGS_PER_ENTRY: Final = 2


def coerce_postings(postings: Sequence[PostingSpec | Mapping[str, Any]]) -> list[PostingSpec]:
    """Normalize API dicts or :class:`PostingSpec` objects into validated specs."""
    out: list[PostingSpec] = []
    for p in postings:
        # Accept either a PostingSpec or an API dict. A missing dict key arrives as None and is
        # rejected by the checks below or by the account lookup.
        raw = (
            (p.account_id, p.direction, p.amount, p.currency)
            if isinstance(p, PostingSpec)
            else (p.get("account_id"), p.get("direction"), p.get("amount"), p.get("currency"))
        )
        # Direction() raises ValueError on an unknown value. Re-raise it as a ValidationError so the
        # API returns 422.
        try:
            spec = PostingSpec(
                account_id=str(raw[0]),
                direction=Direction(raw[1]),
                amount=raw[2],  # type: ignore[arg-type]
                currency=raw[3],  # type: ignore[arg-type]
            )
        except ValueError as exc:
            raise ValidationError(f"malformed posting: {exc}") from exc
        validate_amount(spec.amount)
        validate_currency(spec.currency)
        out.append(spec)
    return out


def check_balanced(postings: Sequence[PostingSpec]) -> None:
    """Raise :class:`UnbalancedEntryError` unless debits equal credits per currency."""
    if len(postings) < MIN_POSTINGS_PER_ENTRY:
        raise UnbalancedEntryError("an entry needs at least two postings")
    # Signed sum per currency: debits count positive and credits negative, so a balanced entry nets
    # to zero in every currency.
    net: dict[str, int] = defaultdict(int)
    for p in postings:
        net[p.currency] += p.amount if p.direction is Direction.DEBIT else -p.amount
    bad = {c: n for c, n in net.items() if n != 0}
    if bad:
        detail = ", ".join(f"{c} debits-credits={n}" for c, n in sorted(bad.items()))
        raise UnbalancedEntryError(f"entry does not balance per currency: {detail}")


def _validate_effective_date(effective_date: str) -> None:
    """Raise ValidationError unless ``effective_date`` parses as an ISO date."""
    try:
        date.fromisoformat(effective_date)
    except ValueError as exc:
        raise ValidationError("effective_date must be YYYY-MM-DD") from exc


class Journal:
    """Writes balanced entries and their cached-balance effects."""

    def __init__(self, clock: Clock) -> None:
        """Create a journal that stamps its entries with ``clock``."""
        self._clock = clock

    def write_entry(
        self,
        conn: sqlite3.Connection,
        postings: Sequence[PostingSpec],
        *,
        description: str = "",
        effective_date: str | None = None,
        external_ref: str | None = None,
        payment_id: str | None = None,
        reverses_entry_id: str | None = None,
        extra_deltas: Mapping[str, Delta] | None = None,
        expected_lock_versions: Mapping[str, int] | None = None,
    ) -> str:
        """Append one balanced entry and apply its balance effects; returns the entry id.

        ``extra_deltas`` are applied in the same compare-and-set as the postings
        (capture uses it to release the hold while posting the entry).
        """
        # Pure checks run before any row is written.
        check_balanced(postings)
        # One query loads every account the entry touches, inside this transaction, so the
        # compare-and-set below works from the same rows.
        ids = [p.account_id for p in postings] + list(extra_deltas or {})
        accounts = repository.load_accounts(conn, ids)
        for p in postings:
            # The schema trigger enforces this too. Checking here gives the client a readable error.
            if accounts[p.account_id]["currency"] != p.currency:
                raise CurrencyMismatchError(
                    f"posting in {p.currency} to account {p.account_id} "
                    f"denominated in {accounts[p.account_id]['currency']}"
                )
        # A missing effective_date falls back to the clock's date, so a pinned clock makes the
        # result repeatable.
        eff = effective_date or self._clock.today()
        _validate_effective_date(eff)

        entry_id = repository.new_id("entry")
        repository.insert_entry(
            conn,
            entry_id,
            postings,
            description=description,
            effective_date=eff,
            created_at=self._clock.timestamp(),
            external_ref=external_ref,
            payment_id=payment_id,
            reverses_entry_id=reverses_entry_id,
        )
        # Defence in depth: re-check balance from what actually hit the table.
        if not repository.entry_is_balanced_in_db(conn, entry_id):  # pragma: no cover
            raise UnbalancedEntryError("entry failed the in-database balance check")

        # Sum the legs per account first, so an account that appears in several legs gets one
        # compare-and-set.
        deltas: dict[str, Delta] = defaultdict(zero_delta)
        for p in postings:
            deltas[p.account_id][posted_index(p.direction)] += p.amount
        # Fold the hold release into the same per-account delta, so a capture is a single balance
        # update.
        for acct_id, d in (extra_deltas or {}).items():
            deltas[acct_id] = [a + b for a, b in zip(deltas[acct_id], d, strict=True)]
        # The overdraft and lock_version checks run after the insert, inside this transaction. A
        # failure rolls back the entry too.
        apply_deltas(conn, deltas, accounts, expected_lock_versions)
        return entry_id

    def write_reversal(
        self,
        conn: sqlite3.Connection,
        entry_id: str,
        description: str | None,
        payment_id: str | None,
    ) -> str:
        """Append the mirror image of ``entry_id``; the original is never modified."""
        # Load first, so an unknown id reports not_found rather than a reversal error.
        repository.load_entry(conn, entry_id)
        # The UNIQUE constraint on reverses_entry_id also blocks a second reversal. This check gives
        # the caller the clearer error.
        if repository.is_reversed(conn, entry_id):
            raise AlreadyReversedError(f"entry {entry_id} has already been reversed")
        return self.write_entry(
            conn,
            # The mirror image reuses the original's accounts and amounts, with every direction
            # flipped.
            repository.reversed_postings(conn, entry_id),
            description=description or f"Reversal of {entry_id}",
            payment_id=payment_id,
            reverses_entry_id=entry_id,
        )
