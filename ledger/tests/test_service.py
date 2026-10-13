from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import Any

import pytest

from ledger.domain import (
    AlreadyReversedError,
    ConcurrencyConflictError,
    CurrencyMismatchError,
    Direction,
    IdempotencyConflictError,
    IdempotencyKey,
    IllegalTransitionError,
    InsufficientFundsError,
    NotFoundError,
    PostingSpec,
    UnbalancedEntryError,
    ValidationError,
)
from ledger.service import Ledger

Fund = Callable[..., dict[str, Any]]


def bal(ledger: Ledger, acct: str) -> dict:
    return ledger.get_balance(acct)


# ------------------------------------------------------------------ accounts


def test_create_account_defaults(ledger: Ledger) -> None:
    a = ledger.create_account("Ops cash", "asset", "USD")
    assert a["id"].startswith("acct_")
    assert a["normal_balance"] == "debit"
    assert a["allow_negative"] is False
    assert a["balances"] == {"posted": 0, "pending": 0, "available": 0}


@pytest.mark.parametrize(
    "kwargs",
    [
        {"name": "x", "type": "cash", "currency": "USD"},
        {"name": "x", "type": "asset", "currency": "usd"},
        {"name": "", "type": "asset", "currency": "USD"},
    ],
)
def test_create_account_validation(ledger: Ledger, kwargs: dict) -> None:
    with pytest.raises(ValidationError):
        ledger.create_account(**kwargs)


def test_missing_account(ledger: Ledger) -> None:
    with pytest.raises(NotFoundError):
        ledger.get_balance("acct_nope")


# ------------------------------------------------------------------- journal


def test_balanced_entry_updates_both_sides(ledger: Ledger, accounts: dict) -> None:
    assert bal(ledger, accounts["bank"])["posted"] == 10_000
    assert bal(ledger, accounts["alice"])["posted"] == 10_000
    assert ledger.trial_balance() == {"USD": {"debits": 10_000, "credits": 10_000, "net": 0}}


def test_unbalanced_entry_rejected_and_nothing_written(ledger: Ledger, accounts: dict) -> None:
    with pytest.raises(UnbalancedEntryError):
        ledger.post_entry(
            [
                PostingSpec(accounts["bank"], Direction.DEBIT, 100, "USD"),
                PostingSpec(accounts["bob"], Direction.CREDIT, 99, "USD"),
            ]
        )
    assert bal(ledger, accounts["bob"])["posted"] == 0
    assert len(ledger.list_entries()["data"]) == 1


def test_single_leg_entry_rejected(ledger: Ledger, accounts: dict) -> None:
    with pytest.raises(UnbalancedEntryError):
        ledger.post_entry([PostingSpec(accounts["bank"], Direction.DEBIT, 100, "USD")])


def test_float_amount_rejected(ledger: Ledger, accounts: dict) -> None:
    with pytest.raises(ValidationError):
        ledger.post_entry(
            [
                {
                    "account_id": accounts["bank"],
                    "direction": "debit",
                    "amount": 10.5,
                    "currency": "USD",
                },
                {
                    "account_id": accounts["bob"],
                    "direction": "credit",
                    "amount": 10.5,
                    "currency": "USD",
                },
            ]
        )


def test_currency_must_match_account(ledger: Ledger, accounts: dict) -> None:
    eur = ledger.create_account("EUR wallet", "liability", "EUR")["id"]
    with pytest.raises(CurrencyMismatchError):
        ledger.post_entry(
            [
                PostingSpec(accounts["bank"], Direction.DEBIT, 100, "EUR"),
                PostingSpec(eur, Direction.CREDIT, 100, "EUR"),
            ]
        )


def test_multi_currency_entry_must_balance_per_currency(
    ledger: Ledger, accounts: dict, fund: Fund
) -> None:
    """An FX conversion: USD leg and EUR leg each balance on their own."""
    eur_bank = ledger.create_account("EUR bank", "asset", "EUR")["id"]
    eur_alice = ledger.create_account("Alice EUR", "liability", "EUR")["id"]
    fx_usd = ledger.create_account("FX USD", "asset", "USD", allow_negative=True)["id"]
    fx_eur = ledger.create_account("FX EUR", "liability", "EUR", allow_negative=True)["id"]
    fund(ledger, eur_bank, fx_eur, 50_000, "EUR")
    entry = ledger.post_entry(
        [
            PostingSpec(accounts["alice"], Direction.DEBIT, 1_000, "USD"),
            PostingSpec(fx_usd, Direction.CREDIT, 1_000, "USD"),
            PostingSpec(fx_eur, Direction.DEBIT, 920, "EUR"),
            PostingSpec(eur_alice, Direction.CREDIT, 920, "EUR"),
        ],
        description="convert 10 USD -> 9.20 EUR",
    )
    assert len(entry["postings"]) == 4
    assert bal(ledger, eur_alice)["posted"] == 920
    tb = ledger.trial_balance()
    assert tb["USD"]["net"] == 0
    assert tb["EUR"]["net"] == 0
    # Same entry but EUR leg off by one cent: rejected even though grand totals look close.
    with pytest.raises(UnbalancedEntryError):
        ledger.post_entry(
            [
                PostingSpec(accounts["alice"], Direction.DEBIT, 1_000, "USD"),
                PostingSpec(fx_usd, Direction.CREDIT, 1_000, "USD"),
                PostingSpec(fx_eur, Direction.DEBIT, 920, "EUR"),
                PostingSpec(eur_alice, Direction.CREDIT, 921, "EUR"),
            ]
        )


def test_journal_is_append_only_at_the_database_level(file_ledger: Ledger, fund: Fund) -> None:
    bank = file_ledger.create_account("Bank", "asset", "USD")["id"]
    w = file_ledger.create_account("W", "liability", "USD")["id"]
    entry = fund(file_ledger, bank, w, 500)
    conn = sqlite3.connect(file_ledger.path)
    try:
        for sql in (
            "UPDATE postings SET amount = 1",
            "DELETE FROM postings",
            f"UPDATE entries SET description = 'x' WHERE id = '{entry['id']}'",
            "DELETE FROM entries",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                conn.execute(sql)
        with pytest.raises(sqlite3.IntegrityError, match="currency"):
            conn.execute(
                "INSERT INTO postings (entry_id, account_id, direction, amount, currency) "
                "VALUES (?, ?, 'debit', 1, 'EUR')",
                (entry["id"], bank),
            )
    finally:
        conn.close()


def test_cache_tampering_is_detected_by_verify(file_ledger: Ledger, fund: Fund) -> None:
    bank = file_ledger.create_account("Bank", "asset", "USD")["id"]
    w = file_ledger.create_account("W", "liability", "USD")["id"]
    fund(file_ledger, bank, w, 500)
    assert file_ledger.verify()["ok"]
    conn = sqlite3.connect(file_ledger.path)
    conn.execute("UPDATE accounts SET posted_credits = posted_credits + 1 WHERE id = ?", (w,))
    conn.commit()
    conn.close()
    report = file_ledger.verify()
    assert not report["ok"]
    assert report["cache_mismatches"] == [w]


# ----------------------------------------------------------------- overdraft


def test_overdraft_rejected_for_wallets(ledger: Ledger, accounts: dict) -> None:
    with pytest.raises(InsufficientFundsError):
        ledger.create_payment(accounts["alice"], accounts["bob"], 10_001, "USD")
    p = ledger.create_payment(accounts["alice"], accounts["bob"], 10_000, "USD")
    assert p["status"] == "posted"
    assert bal(ledger, accounts["alice"])["available"] == 0


def test_allow_negative_account_can_go_below_zero(ledger: Ledger, accounts: dict) -> None:
    credit_line = ledger.create_account("Credit line", "liability", "USD", allow_negative=True)[
        "id"
    ]
    ledger.create_payment(credit_line, accounts["bob"], 5_000, "USD")
    assert bal(ledger, credit_line)["posted"] == -5_000


def test_asset_cannot_be_credited_below_zero(ledger: Ledger, accounts: dict) -> None:
    expense = ledger.create_account("Bank fees", "expense", "USD")["id"]
    legs = [
        PostingSpec(expense, Direction.DEBIT, 10_001, "USD"),
        PostingSpec(accounts["bank"], Direction.CREDIT, 10_001, "USD"),
    ]
    with pytest.raises(InsufficientFundsError, match=accounts["bank"]):
        ledger.post_entry(legs)
    legs = [
        PostingSpec(expense, Direction.DEBIT, 10_000, "USD"),
        PostingSpec(accounts["bank"], Direction.CREDIT, 10_000, "USD"),
    ]
    ledger.post_entry(legs)
    assert bal(ledger, accounts["bank"])["posted"] == 0


# -------------------------------------------------------- payments lifecycle


def test_authorization_hold_reduces_available_not_posted(ledger: Ledger, accounts: dict) -> None:
    p = ledger.create_payment(accounts["alice"], accounts["bob"], 3_000, "USD", capture=False)
    assert p["status"] == "pending"
    assert p["entry_id"] is None
    a, b = bal(ledger, accounts["alice"]), bal(ledger, accounts["bob"])
    assert (a["posted"], a["pending"], a["available"]) == (10_000, 7_000, 7_000)
    # inbound hold shows in pending but is not spendable yet
    assert (b["posted"], b["pending"], b["available"]) == (0, 3_000, 0)
    with pytest.raises(InsufficientFundsError):
        ledger.create_payment(accounts["alice"], accounts["bob"], 7_001, "USD")
    assert ledger.verify()["ok"]


def test_capture_posts_entry_and_releases_hold(ledger: Ledger, accounts: dict) -> None:
    p = ledger.create_payment(accounts["alice"], accounts["bob"], 3_000, "USD", capture=False)
    p = ledger.capture_payment(p["id"])
    assert p["status"] == "posted"
    assert p["entry_id"]
    assert [e["to_status"] for e in p["events"]] == ["pending", "posted"]
    a, b = bal(ledger, accounts["alice"]), bal(ledger, accounts["bob"])
    assert (a["posted"], a["pending"], a["available"]) == (7_000, 7_000, 7_000)
    assert (b["posted"], b["pending"], b["available"]) == (3_000, 3_000, 3_000)
    assert ledger.verify()["ok"]


def test_fail_releases_hold_without_journal_entry(ledger: Ledger, accounts: dict) -> None:
    entries_before = len(ledger.list_entries()["data"])
    p = ledger.create_payment(accounts["alice"], accounts["bob"], 3_000, "USD", capture=False)
    p = ledger.fail_payment(p["id"], reason="card declined")
    assert p["status"] == "failed"
    assert p["failure_reason"] == "card declined"
    assert bal(ledger, accounts["alice"])["available"] == 10_000
    assert bal(ledger, accounts["bob"])["pending"] == 0
    assert len(ledger.list_entries()["data"]) == entries_before
    assert ledger.verify()["ok"]


def test_reverse_posted_payment(ledger: Ledger, accounts: dict) -> None:
    p = ledger.create_payment(accounts["alice"], accounts["bob"], 2_500, "USD")
    p = ledger.reverse_payment(p["id"])
    assert p["status"] == "reversed"
    assert p["reversal_entry_id"]
    assert bal(ledger, accounts["alice"])["posted"] == 10_000
    assert bal(ledger, accounts["bob"])["posted"] == 0
    original = ledger.get_entry(p["entry_id"])
    assert original["reversed_by_entry_id"] == p["reversal_entry_id"]
    assert ledger.verify()["ok"]


@pytest.mark.parametrize(
    "path",
    [
        ("capture", "capture"),
        ("fail", "capture"),
        ("fail", "fail"),
        ("fail", "reverse"),
        ("capture", "fail"),
        ("reverse_pending",),
    ],
)
def test_illegal_transitions_rejected(ledger: Ledger, accounts: dict, path: tuple) -> None:
    p = ledger.create_payment(accounts["alice"], accounts["bob"], 100, "USD", capture=False)
    ops = {
        "capture": ledger.capture_payment,
        "fail": ledger.fail_payment,
        "reverse": ledger.reverse_payment,
        "reverse_pending": ledger.reverse_payment,
    }
    *ok_steps, bad = path
    for step in ok_steps:
        ops[step](p["id"])
    snapshot = ledger.verify()
    with pytest.raises(IllegalTransitionError):
        ops[bad](p["id"])
    assert ledger.verify() == snapshot


def test_reversal_respects_overdraft(ledger: Ledger, accounts: dict) -> None:
    """Bob spent the money: pulling it back would overdraw him, so the reversal fails."""
    p = ledger.create_payment(accounts["alice"], accounts["bob"], 5_000, "USD")
    ledger.create_payment(accounts["bob"], accounts["alice"], 4_000, "USD")
    with pytest.raises(InsufficientFundsError):
        ledger.reverse_payment(p["id"])
    assert ledger.get_payment(p["id"])["status"] == "posted"


def test_payment_validation(ledger: Ledger, accounts: dict) -> None:
    eur = ledger.create_account("EUR", "liability", "EUR")["id"]
    with pytest.raises(ValidationError):
        ledger.create_payment(accounts["alice"], accounts["alice"], 1, "USD")
    with pytest.raises(CurrencyMismatchError):
        ledger.create_payment(accounts["alice"], eur, 1, "USD")
    with pytest.raises(NotFoundError):
        ledger.create_payment(accounts["alice"], "acct_missing", 1, "USD")
    with pytest.raises(NotFoundError):
        ledger.capture_payment("pay_missing")


# ----------------------------------------------------------------- reversals


def test_reverse_manual_entry_once(ledger: Ledger, accounts: dict) -> None:
    e = ledger.post_entry(
        [
            PostingSpec(accounts["alice"], Direction.DEBIT, 1_000, "USD"),
            PostingSpec(accounts["bob"], Direction.CREDIT, 1_000, "USD"),
        ]
    )
    rev = ledger.reverse_entry(e["id"])
    assert rev["reverses_entry_id"] == e["id"]
    assert {(p["account_id"], p["direction"]) for p in rev["postings"]} == {
        (accounts["alice"], "credit"),
        (accounts["bob"], "debit"),
    }
    assert bal(ledger, accounts["bob"])["posted"] == 0
    with pytest.raises(AlreadyReversedError):
        ledger.reverse_entry(e["id"])
    # the original entry is untouched
    assert ledger.get_entry(e["id"])["postings"] == e["postings"]


def test_reverse_entry_of_payment_moves_payment_state(ledger: Ledger, accounts: dict) -> None:
    p = ledger.create_payment(accounts["alice"], accounts["bob"], 1_000, "USD")
    rev = ledger.reverse_entry(p["entry_id"])
    assert ledger.get_payment(p["id"])["status"] == "reversed"
    with pytest.raises(IllegalTransitionError):
        ledger.reverse_entry(rev["id"])  # payment reversal entries are final


def test_reverse_missing_entry(ledger: Ledger) -> None:
    with pytest.raises(NotFoundError):
        ledger.reverse_entry("entry_nope")


# --------------------------------------------------------------- idempotency


def test_idempotent_replay_returns_original_and_applies_once(
    ledger: Ledger, accounts: dict
) -> None:
    k1 = IdempotencyKey("order-42")
    first = ledger.create_payment(accounts["alice"], accounts["bob"], 700, "USD", idempotency=k1)
    k2 = IdempotencyKey("order-42")
    second = ledger.create_payment(accounts["alice"], accounts["bob"], 700, "USD", idempotency=k2)
    assert not k1.replayed
    assert k2.replayed
    assert first == second
    assert bal(ledger, accounts["bob"])["posted"] == 700


def test_idempotency_key_reuse_with_different_body_conflicts(
    ledger: Ledger, accounts: dict
) -> None:
    ledger.create_payment(
        accounts["alice"], accounts["bob"], 700, "USD", idempotency=IdempotencyKey("k")
    )
    with pytest.raises(IdempotencyConflictError):
        ledger.create_payment(
            accounts["alice"], accounts["bob"], 701, "USD", idempotency=IdempotencyKey("k")
        )
    with pytest.raises(IdempotencyConflictError):  # same key, different operation
        ledger.create_account("x", "asset", "USD", idempotency=IdempotencyKey("k"))
    assert bal(ledger, accounts["bob"])["posted"] == 700


def test_failed_request_does_not_burn_the_key(ledger: Ledger, accounts: dict) -> None:
    with pytest.raises(InsufficientFundsError):
        ledger.create_payment(
            accounts["bob"], accounts["alice"], 500, "USD", idempotency=IdempotencyKey("retry")
        )
    ledger.create_payment(accounts["alice"], accounts["bob"], 500, "USD")
    p = ledger.create_payment(
        accounts["bob"], accounts["alice"], 500, "USD", idempotency=IdempotencyKey("retry")
    )
    assert p["status"] == "posted"


def test_idempotency_key_length(ledger: Ledger) -> None:
    with pytest.raises(ValidationError):
        ledger.create_account("x", "asset", "USD", idempotency=IdempotencyKey("k" * 256))


# ---------------------------------------------------------------- concurrency


def test_expected_lock_version_optimistic_check(ledger: Ledger, accounts: dict) -> None:
    v = bal(ledger, accounts["alice"])["lock_version"]
    legs = [
        PostingSpec(accounts["alice"], Direction.DEBIT, 10, "USD"),
        PostingSpec(accounts["bob"], Direction.CREDIT, 10, "USD"),
    ]
    ledger.post_entry(legs, expected_lock_versions={accounts["alice"]: v})
    with pytest.raises(ConcurrencyConflictError):  # stale version: someone else wrote in between
        ledger.post_entry(legs, expected_lock_versions={accounts["alice"]: v})
    assert bal(ledger, accounts["alice"])["lock_version"] == v + 1


# ----------------------------------------------------------------- listings


def test_list_entries_pagination_and_filter(ledger: Ledger, accounts: dict) -> None:
    for _ in range(5):
        ledger.create_payment(accounts["alice"], accounts["bob"], 1, "USD")
    page1 = ledger.list_entries(account_id=accounts["bob"], limit=2)
    assert len(page1["data"]) == 2
    assert page1["has_more"]
    page2 = ledger.list_entries(account_id=accounts["bob"], limit=2, cursor=page1["next_cursor"])
    page3 = ledger.list_entries(account_id=accounts["bob"], limit=2, cursor=page2["next_cursor"])
    seqs = [e["seq"] for e in page1["data"] + page2["data"] + page3["data"]]
    assert len(seqs) == 5
    assert seqs == sorted(seqs, reverse=True)
    assert not page3["has_more"]
    assert page3["next_cursor"] is None
    assert len(ledger.list_entries()["data"]) == 6  # + the funding entry
    with pytest.raises(ValidationError):
        ledger.list_entries(limit=0)


def test_account_activity_signed_in_normal_direction(ledger: Ledger, accounts: dict) -> None:
    ledger.create_payment(
        accounts["alice"],
        accounts["bob"],
        400,
        "USD",
        external_ref="X1",
        effective_date="2026-09-02",
    )
    act = ledger.account_activity(accounts["alice"])
    assert [a["amount"] for a in act] == [10_000, -400]
    assert act[1]["external_ref"] == "X1"
    assert act[1]["effective_date"] == "2026-09-02"
    assert (
        ledger.account_activity(accounts["alice"], start="2026-09-02", end="2026-09-02")[0][
            "amount"
        ]
        == -400
    )


def test_bad_effective_date(ledger: Ledger, accounts: dict) -> None:
    with pytest.raises(ValidationError):
        ledger.create_payment(
            accounts["alice"], accounts["bob"], 1, "USD", effective_date="2026-13-45"
        )
