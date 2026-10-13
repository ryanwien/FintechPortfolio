from __future__ import annotations

import pytest

from ledger.recon import (
    BankLine,
    LedgerItem,
    Recon,
    ReconReport,
    ledger_items_from_activity,
    reconcile,
    score,
)
from ledger.scenario import build_settlement_scenario
from ledger.service import Ledger
from ledger.statement import InjectionRates, generate_statement

L = [
    LedgerItem("e1", "DEP-1", "2026-09-01", 10_000, "USD"),
    LedgerItem("e2", "DEP-2", "2026-09-01", 2_500, "USD"),
    LedgerItem("e3", "WD-3", "2026-09-02", -4_000, "USD"),
    LedgerItem("e4", "DEP-4", "2026-09-03", 7_777, "USD"),
    LedgerItem("e5", "WD-5", "2026-09-03", -1_200, "USD"),
    LedgerItem("e6", "DEP-6", "2026-09-04", 3_333, "USD"),
]


def kinds(report: ReconReport) -> dict[str, str]:
    return {i.key: i.kind.value for i in report.items}


def test_each_break_type_is_classified() -> None:
    bank = [
        # matched
        BankLine("B1", "2026-09-01", 10_000, "USD", "DEP-1", "ACH"),
        # matched (within 1 day)
        BankLine("B2", "2026-09-02", 2_500, "USD", "DEP-2", "ACH"),
        # amount mismatch (fee netted)
        BankLine("B3", "2026-09-02", -3_975, "USD", "WD-3", "PAYOUT"),
        # timing (3 days late)
        BankLine("B4", "2026-09-06", 7_777, "USD", "DEP-4", "ACH"),
        # fuzzy match on amount+date
        BankLine("B5", "2026-09-04", -1_200, "USD", None, "PAYOUT trunc"),
        # duplicate of DEP-1
        BankLine("B6", "2026-09-01", 10_000, "USD", "DEP-1", "ACH resub"),
        # missing in ledger
        BankLine("B7", "2026-09-03", -1_500, "USD", None, "SERVICE FEE"),
        # DEP-6 never shows up -> missing in bank
    ]
    rep = reconcile(L, bank)
    assert kinds(rep) == {
        # the matched DEP-1 and its duplicate share a key; duplicate is recorded last
        "DEP-1": "duplicate",
        "DEP-2": "matched",
        "WD-3": "amount_mismatch",
        "DEP-4": "timing",
        "WD-5": "matched_fuzzy",
        "B7": "missing_in_ledger",
        "DEP-6": "missing_in_bank",
    }
    counts = rep.counts()
    assert counts["matched"] == 2
    assert counts["duplicate"] == 1
    s = rep.summary(L, bank)
    assert s["unexplained_difference"] == 0
    assert s["difference"] == sum(b.amount for b in bank) - sum(i.amount for i in L)
    by_kind = {b.kind: b for b in rep.breaks}
    assert by_kind[Recon.AMOUNT_MISMATCH].delta == 25
    assert by_kind[Recon.TIMING].to_dict()["days_apart"] == 3


def test_fuzzy_match_does_not_cross_tolerance() -> None:
    bank = [BankLine("B1", "2026-09-05", 2_500, "USD", None, "no memo")]
    rep = reconcile([L[1]], bank)
    assert {i.kind for i in rep.items} == {Recon.MISSING_IN_LEDGER, Recon.MISSING_IN_BANK}


def test_currency_must_agree() -> None:
    bank = [BankLine("B1", "2026-09-01", 10_000, "EUR", "DEP-1", "ACH")]
    rep = reconcile([L[0]], bank)
    assert Recon.MATCHED not in {i.kind for i in rep.items}


def test_clean_statement_fully_matches() -> None:
    stmt = generate_statement(L, rates=InjectionRates(0, 0, 0, 0, 0, 0))
    rep = reconcile(L, stmt.lines)
    assert rep.breaks == []
    assert stmt.injected == []
    assert rep.summary(L, stmt.lines)["difference"] == 0


@pytest.fixture(scope="module")
def scenario_items() -> list[LedgerItem]:
    lg = Ledger(":memory:")
    sc = build_settlement_scenario(lg, customers=15, days=10, movements_per_day=30)
    items = ledger_items_from_activity(lg.account_activity(sc.cash_account_id))
    assert lg.verify()["ok"]
    lg.close()
    return items


def test_generator_is_deterministic(scenario_items: list[LedgerItem]) -> None:
    a = generate_statement(scenario_items, seed=3)
    b = generate_statement(scenario_items, seed=3)
    assert a.lines == b.lines
    assert a.injected == b.injected
    assert generate_statement(scenario_items, seed=4).lines != a.lines


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_injected_breaks_are_recovered(scenario_items: list[LedgerItem], seed: int) -> None:
    rates = InjectionRates(
        missing_in_bank=0.05,
        amount_mismatch=0.05,
        timing=0.05,
        duplicate=0.05,
        missing_in_ledger=0.03,
        stripped_reference=0.1,
    )
    stmt = generate_statement(scenario_items, seed=seed, rates=rates)
    rep = reconcile(scenario_items, stmt.lines)
    sc = score(rep, stmt.injected)
    assert sc["injected_breaks"] > 20
    assert sc["recall"] >= 0.95
    assert sc["precision"] >= 0.95
    assert rep.summary(scenario_items, stmt.lines)["unexplained_difference"] == 0
