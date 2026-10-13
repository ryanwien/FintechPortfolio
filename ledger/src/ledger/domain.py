"""Core domain types: account types, posting directions, money rules and errors.

Money is always an ``int`` of minor units (cents for USD, yen for JPY). Floats are
rejected at every boundary; there is no code path that converts money to float.
Every error raised by the ledger derives from :class:`LedgerError` and carries a
stable machine-readable ``code`` plus the HTTP status the API maps it to.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

# 10^15 minor units = 10 trillion USD. Keeps every SUM() far inside SQLite's int64.
MAX_AMOUNT: Final = 10**15
CURRENCY_CODE_LENGTH: Final = 3
MAX_ACCOUNT_NAME_LENGTH: Final = 200
MAX_IDEMPOTENCY_KEY_LENGTH: Final = 255
MAX_DESCRIPTION_LENGTH: Final = 500
MAX_EXTERNAL_REF_LENGTH: Final = 200


class AccountType(StrEnum):
    """The five account classes of double-entry bookkeeping."""

    ASSET = "asset"
    LIABILITY = "liability"
    EQUITY = "equity"
    REVENUE = "revenue"
    EXPENSE = "expense"

    @property
    def normal_side(self) -> Direction:
        """Debit-normal accounts grow with debits; credit-normal ones with credits."""
        if self in (AccountType.ASSET, AccountType.EXPENSE):
            return Direction.DEBIT
        return Direction.CREDIT


class Direction(StrEnum):
    """Side of a posting."""

    DEBIT = "debit"
    CREDIT = "credit"

    def flip(self) -> Direction:
        """The opposite side, used to build reversing entries."""
        return Direction.CREDIT if self is Direction.DEBIT else Direction.DEBIT


@dataclass(frozen=True, slots=True)
class PostingSpec:
    """One leg of a journal entry, before it is written."""

    account_id: str
    direction: Direction
    amount: int
    currency: str


@dataclass(slots=True)
class IdempotencyKey:
    """Client-supplied idempotency key.

    ``replayed`` is set by the ledger when the stored response of an earlier
    identical request is returned instead of executing the operation again.
    """

    key: str
    replayed: bool = False


def validate_amount(amount: object) -> int:
    """Return ``amount`` if it is a positive integer of minor units within range."""
    # bool is a subclass of int; exclude it explicitly.
    if not isinstance(amount, int) or isinstance(amount, bool):
        raise ValidationError(
            f"amount must be an integer number of minor units, got {type(amount).__name__}"
        )
    if amount <= 0:
        raise ValidationError("amount must be positive")
    if amount > MAX_AMOUNT:
        raise ValidationError(f"amount exceeds the maximum of {MAX_AMOUNT} minor units")
    return amount


def validate_currency(currency: object) -> str:
    """Return ``currency`` if it looks like an upper-case ISO 4217 code."""
    if (
        not isinstance(currency, str)
        or len(currency) != CURRENCY_CODE_LENGTH
        or not currency.isascii()
        or not currency.isupper()
    ):
        raise ValidationError("currency must be a 3-letter upper-case ISO 4217 code")
    return currency


# --------------------------------------------------------------------------- errors


class LedgerError(Exception):
    """Base class for every ledger error.

    ``code`` is a stable machine-readable identifier, ``http_status`` is what the
    API maps it to.
    """

    code = "ledger_error"
    http_status = 400


class ValidationError(LedgerError):
    code = "validation_error"
    http_status = 422


class UnbalancedEntryError(ValidationError):
    code = "unbalanced_entry"


class CurrencyMismatchError(ValidationError):
    code = "currency_mismatch"


class InsufficientFundsError(LedgerError):
    code = "insufficient_funds"
    http_status = 422


class NotFoundError(LedgerError):
    code = "not_found"
    http_status = 404


class ConflictError(LedgerError):
    code = "conflict"
    http_status = 409


class IdempotencyConflictError(ConflictError):
    code = "idempotency_key_reused"


class ConcurrencyConflictError(ConflictError):
    code = "lock_version_mismatch"


class IllegalTransitionError(ConflictError):
    code = "illegal_state_transition"


class AlreadyReversedError(ConflictError):
    code = "already_reversed"
