from __future__ import annotations

import itertools

import pytest

from ledger.balances import compute_balances
from ledger.domain import (
    MAX_AMOUNT,
    AccountType,
    Direction,
    IllegalTransitionError,
    ValidationError,
    validate_amount,
    validate_currency,
)
from ledger.lifecycle import TERMINAL, TRANSITIONS, PaymentStatus, assert_transition, can_transition


@pytest.mark.parametrize("bad", [0, -1, 1.5, 10.0, "100", True, None, MAX_AMOUNT + 1])
def test_amount_rejects_non_positive_non_integer_and_oversized(bad: object) -> None:
    with pytest.raises(ValidationError):
        validate_amount(bad)


def test_amount_accepts_positive_ints() -> None:
    assert validate_amount(1) == 1
    assert validate_amount(MAX_AMOUNT) == MAX_AMOUNT


@pytest.mark.parametrize("bad", ["usd", "US", "USDT", "", 840, "ÜSD"])
def test_currency_validation(bad: object) -> None:
    with pytest.raises(ValidationError):
        validate_currency(bad)


def test_normal_sides() -> None:
    assert AccountType.ASSET.normal_side is Direction.DEBIT
    assert AccountType.EXPENSE.normal_side is Direction.DEBIT
    for t in (AccountType.LIABILITY, AccountType.EQUITY, AccountType.REVENUE):
        assert t.normal_side is Direction.CREDIT
    assert Direction.DEBIT.flip() is Direction.CREDIT


@pytest.mark.parametrize(
    ("account_type", "totals"),
    [
        # 100 posted credit, 30 held outbound (pending debit), 20 held inbound (pending credit)
        ("liability", (0, 100, 30, 20)),
        # 100 posted debit, 20 held outbound (pending credit), 30 held inbound (pending debit)
        ("asset", (100, 0, 20, 30)),
    ],
)
def test_balances_with_holds_in_normal_direction(
    account_type: str, totals: tuple[int, int, int, int]
) -> None:
    posted_debits, posted_credits, pending_debits, pending_credits = totals
    b = compute_balances(
        account_type,
        posted_debits=posted_debits,
        posted_credits=posted_credits,
        pending_debits=pending_debits,
        pending_credits=pending_credits,
    )
    assert b == {"posted": 100, "pending": 90, "available": 70}


@pytest.mark.parametrize(("cur", "tgt"), list(itertools.product(PaymentStatus, PaymentStatus)))
def test_state_machine_exhaustive(cur: PaymentStatus, tgt: PaymentStatus) -> None:
    legal = {
        (PaymentStatus.PENDING, PaymentStatus.POSTED),
        (PaymentStatus.PENDING, PaymentStatus.FAILED),
        (PaymentStatus.POSTED, PaymentStatus.REVERSED),
    }
    assert can_transition(cur, tgt) == ((cur, tgt) in legal)
    if (cur, tgt) in legal:
        assert_transition(cur, tgt)
    else:
        with pytest.raises(IllegalTransitionError):
            assert_transition(cur, tgt)


def test_terminal_states() -> None:
    assert {PaymentStatus.FAILED, PaymentStatus.REVERSED} == TERMINAL
    assert set(TRANSITIONS) == set(PaymentStatus)
