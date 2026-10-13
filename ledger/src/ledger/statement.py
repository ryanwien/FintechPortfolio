"""Synthetic bank statements with injected, recorded discrepancies.

``generate_statement`` turns real ledger activity into the bank's view of the
same cash account, deliberately breaking a known share of lines (missing,
amount mismatch, late settlement, duplicate, stripped reference, bank-only
lines). Every injected break is recorded so :func:`ledger.recon.score` can
measure the matcher against ground truth. Same items + seed -> same statement.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Final

from ledger.recon import (
    DEFAULT_DATE_TOLERANCE_DAYS,
    BankLine,
    InjectedBreak,
    LedgerItem,
    Recon,
)

DEFAULT_STATEMENT_SEED: Final = 7
# Amount mismatches: the bank netted a fee / partial settlement of this many minor units.
NETTED_FEE_RANGE: Final = (25, 1_500)
# Timing breaks: settled this many days late, i.e. after the tolerance window.
LATE_SETTLEMENT_DAYS: Final = (2, 4)
# Bank-only lines: mostly service fees (debits), otherwise unidentified incoming wires.
BANK_FEE_PROBABILITY: Final = 0.6
BANK_FEE_RANGE: Final = (1_00, 75_00)
UNIDENTIFIED_WIRE_RANGE: Final = (500_00, 9_000_00)
UNIDENTIFIED_WIRE_ODD_CENTS: Final = 37
EMPTY_STATEMENT_DATE: Final = "2026-01-01"
EMPTY_STATEMENT_CURRENCY: Final = "USD"


@dataclass(frozen=True, slots=True)
class Statement:
    """A generated statement and the breaks injected into it."""

    lines: list[BankLine]
    injected: list[InjectedBreak]


@dataclass(frozen=True, slots=True)
class InjectionRates:
    """Share of ledger items turned into each kind of break by the generator."""

    missing_in_bank: float = 0.02
    amount_mismatch: float = 0.015
    timing: float = 0.03
    duplicate: float = 0.01
    missing_in_ledger: float = 0.01  # relative to number of ledger items
    stripped_reference: float = 0.08  # clean lines whose memo lost our reference
    # Settlement lag for reference-less lines. Above the matcher's 1-day tolerance
    # these are real timing breaks the matcher cannot see without a reference.
    stripped_max_lag_days: int = 1
    date_tolerance_days: int = DEFAULT_DATE_TOLERANCE_DAYS


def _shift(d: str, days: int) -> str:
    return (date.fromisoformat(d) + timedelta(days=days)).isoformat()


def generate_statement(
    items: list[LedgerItem],
    *,
    seed: int = DEFAULT_STATEMENT_SEED,
    rates: InjectionRates | None = None,
) -> Statement:
    """Deterministically turn ledger activity into a bank statement.

    Injected discrepancies are recorded in ``Statement.injected`` as ground truth.
    """
    rates = rates or InjectionRates()
    rng = random.Random(seed)
    ordered = sorted(items, key=lambda i: (i.date, i.ref or "", i.amount))
    n = len(ordered)
    idx = list(range(n))
    rng.shuffle(idx)

    def take(rate: float) -> list[int]:
        k = round(rate * n)
        out, idx[:] = idx[:k], idx[k:]
        return out

    missing = take(rates.missing_in_bank)
    mismatch = take(rates.amount_mismatch)
    timing = take(rates.timing)
    duplicate = take(rates.duplicate)
    stripped = set(take(rates.stripped_reference))

    raw: list[tuple[str, int, str, str | None, str]] = []  # date, amount, ccy, ref, description
    injected: list[InjectedBreak] = []
    skip = set(missing)
    for i in missing:
        injected.append(InjectedBreak(Recon.MISSING_IN_BANK, str(ordered[i].ref)))
    for pos, it in enumerate(ordered):
        if pos in skip:
            continue
        d, amt, ref = it.date, it.amount, it.ref
        desc = "ACH" if amt > 0 else "PAYOUT"
        if pos in mismatch:
            fee = rng.randint(*NETTED_FEE_RANGE)
            amt = amt - fee if amt > 0 else amt + fee
            injected.append(InjectedBreak(Recon.AMOUNT_MISMATCH, str(ref)))
        elif pos in timing:
            d = _shift(d, rng.randint(*LATE_SETTLEMENT_DAYS))
            injected.append(InjectedBreak(Recon.TIMING, str(ref)))
        elif pos in stripped:
            lag = rng.choice((0, 0, *range(1, rates.stripped_max_lag_days + 1)))
            if lag > rates.date_tolerance_days:
                injected.append(InjectedBreak(Recon.TIMING, str(ref)))
            ref, desc = None, desc + " (memo truncated)"
            d = _shift(d, lag)
        raw.append((d, amt, it.currency, ref, desc))
        if pos in duplicate:
            raw.append((d, amt, it.currency, ref, desc + " (resubmitted)"))
            injected.append(InjectedBreak(Recon.DUPLICATE, str(ref)))

    first = ordered[0].date if ordered else EMPTY_STATEMENT_DATE
    last = ordered[-1].date if ordered else EMPTY_STATEMENT_DATE
    span = (date.fromisoformat(last) - date.fromisoformat(first)).days
    ccy = ordered[0].currency if ordered else EMPTY_STATEMENT_CURRENCY
    bank_only: list[int] = []
    for _ in range(round(rates.missing_in_ledger * n)):
        if rng.random() < BANK_FEE_PROBABILITY:
            amt = -rng.randint(*BANK_FEE_RANGE)
        else:
            amt = rng.randint(*UNIDENTIFIED_WIRE_RANGE) + UNIDENTIFIED_WIRE_ODD_CENTS
        desc = "SERVICE FEE" if amt < 0 else "INCOMING WIRE - UNIDENTIFIED"
        raw.append((_shift(first, rng.randint(0, span)), amt, ccy, None, desc))
        bank_only.append(len(raw) - 1)

    order = sorted(range(len(raw)), key=lambda k: (raw[k][0], k))
    lines: list[BankLine] = []
    for line_no, k in enumerate(order, start=1):
        d, amt, c, ref, desc = raw[k]
        line = BankLine(f"BL-{line_no:06d}", d, amt, c, ref, desc)
        lines.append(line)
        if k in bank_only:
            injected.append(InjectedBreak(Recon.MISSING_IN_LEDGER, line.line_id))
    return Statement(lines=lines, injected=injected)
