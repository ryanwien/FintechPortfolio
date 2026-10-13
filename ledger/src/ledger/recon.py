"""Bank reconciliation.

Compares the ledger's view of a settlement (cash) account with the bank's
statement for the same account and classifies every difference:

* ``matched``           reference, amount and date (within tolerance) agree
* ``matched_fuzzy``     bank line has no usable reference; matched on amount and date
* ``timing``            same reference and amount, value date outside tolerance
* ``amount_mismatch``   same reference, different amount (e.g. a fee netted out)
* ``duplicate``         bank reported the same reference twice
* ``missing_in_bank``   ledger movement with no bank line
* ``missing_in_ledger`` bank line with no ledger movement (fees, unidentified wires)

:mod:`ledger.statement` builds a deterministic synthetic statement with known,
injected discrepancies, so the matcher here can be scored against ground truth.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date
from enum import StrEnum
from typing import Any, Final


class Recon(StrEnum):
    MATCHED = "matched"
    MATCHED_FUZZY = "matched_fuzzy"
    TIMING = "timing"
    AMOUNT_MISMATCH = "amount_mismatch"
    DUPLICATE = "duplicate"
    MISSING_IN_BANK = "missing_in_bank"
    MISSING_IN_LEDGER = "missing_in_ledger"


BREAKS: Final = (
    Recon.TIMING,
    Recon.AMOUNT_MISMATCH,
    Recon.DUPLICATE,
    Recon.MISSING_IN_BANK,
    Recon.MISSING_IN_LEDGER,
)
MATCHES: Final = (Recon.MATCHED, Recon.MATCHED_FUZZY)

DEFAULT_DATE_TOLERANCE_DAYS: Final = 1


@dataclass(frozen=True, slots=True)
class LedgerItem:
    """One movement on the ledger's cash account."""

    entry_id: str
    ref: str | None
    date: str
    amount: int  # signed minor units; + = cash in
    currency: str


@dataclass(frozen=True, slots=True)
class BankLine:
    """One line of the bank's statement for the same account."""

    line_id: str
    value_date: str
    amount: int  # signed minor units; + = credit to our account
    currency: str
    reference: str | None
    description: str


@dataclass(frozen=True, slots=True)
class InjectedBreak:
    """Ground truth recorded by the statement generator."""

    kind: Recon
    key: str  # ledger ref, or bank line id for bank-only lines


@dataclass(frozen=True, slots=True)
class Classified:
    """The matcher's verdict on one ledger item, bank line, or matched pair."""

    kind: Recon
    ref: str | None = None
    line_id: str | None = None
    entry_id: str | None = None
    ledger_amount: int | None = None
    bank_amount: int | None = None
    ledger_date: str | None = None
    bank_date: str | None = None

    @property
    def delta(self) -> int:
        """bank - ledger, the contribution of this item to the total difference."""
        return (self.bank_amount or 0) - (self.ledger_amount or 0)

    @property
    def key(self) -> str:
        return self.ref if self.ref is not None else str(self.line_id)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["kind"] = self.kind.value
        d["delta"] = self.delta
        if self.ledger_date and self.bank_date:
            d["days_apart"] = (
                date.fromisoformat(self.bank_date) - date.fromisoformat(self.ledger_date)
            ).days
        return d


@dataclass(slots=True)
class ReconReport:
    """Every classified item from one reconciliation run."""

    items: list[Classified] = field(default_factory=list)

    @property
    def breaks(self) -> list[Classified]:
        return [c for c in self.items if c.kind in BREAKS]

    def counts(self) -> dict[str, int]:
        c = Counter(i.kind.value for i in self.items)
        return {k.value: c.get(k.value, 0) for k in Recon}

    def summary(self, ledger_items: list[LedgerItem], lines: list[BankLine]) -> dict[str, Any]:
        ledger_total = sum(i.amount for i in ledger_items)
        bank_total = sum(b.amount for b in lines)
        explained = sum(c.delta for c in self.items)
        matched = sum(1 for c in self.items if c.kind in MATCHES)
        return {
            "ledger_items": len(ledger_items),
            "bank_lines": len(lines),
            "counts": self.counts(),
            "breaks": len(self.breaks),
            "auto_match_rate": round(matched / len(ledger_items), 4) if ledger_items else 0.0,
            "ledger_total": ledger_total,
            "bank_total": bank_total,
            "difference": bank_total - ledger_total,
            "explained_by_breaks": explained,
            "unexplained_difference": bank_total - ledger_total - explained,
        }


def _days_apart(a: str, b: str) -> int:
    return abs((date.fromisoformat(a) - date.fromisoformat(b)).days)


def reconcile(
    items: list[LedgerItem],
    lines: list[BankLine],
    *,
    date_tolerance_days: int = DEFAULT_DATE_TOLERANCE_DAYS,
) -> ReconReport:
    """Three-pass matcher.

    (1) exact reference, (2) amount + date for lines without a known reference,
    (3) leftovers on either side are breaks.
    """
    report = ReconReport()
    by_ref: dict[str, list[LedgerItem]] = defaultdict(list)
    for it in sorted(items, key=lambda i: (i.date, i.entry_id)):
        if it.ref is not None:
            by_ref[it.ref].append(it)
    used: set[str] = set()  # ledger entry ids already matched
    matched_by_ref: dict[str, LedgerItem] = {}
    deferred: list[BankLine] = []

    # Pass 1: reference match.
    for line in lines:
        cands = by_ref.get(line.reference or "", [])
        if not cands:
            deferred.append(line)
            continue
        free = [c for c in cands if c.entry_id not in used and c.currency == line.currency]
        if not free:
            orig = matched_by_ref.get(str(line.reference))
            report.items.append(
                Classified(
                    Recon.DUPLICATE,
                    line.reference,
                    line.line_id,
                    orig.entry_id if orig else None,
                    None,
                    line.amount,
                    orig.date if orig else None,
                    line.value_date,
                )
            )
            continue
        it = min(
            free, key=lambda c: (c.amount != line.amount, _days_apart(c.date, line.value_date))
        )
        used.add(it.entry_id)
        matched_by_ref[str(line.reference)] = it
        if it.amount != line.amount:
            kind = Recon.AMOUNT_MISMATCH
        elif _days_apart(it.date, line.value_date) > date_tolerance_days:
            kind = Recon.TIMING
        else:
            kind = Recon.MATCHED
        report.items.append(
            Classified(
                kind,
                it.ref,
                line.line_id,
                it.entry_id,
                it.amount,
                line.amount,
                it.date,
                line.value_date,
            )
        )

    # Pass 2: amount + date window for lines with no usable reference.
    pool: dict[tuple[int, str], list[LedgerItem]] = defaultdict(list)
    for it in items:
        if it.entry_id not in used:
            pool[(it.amount, it.currency)].append(it)
    for line in deferred:
        cands = [
            c
            for c in pool.get((line.amount, line.currency), [])
            if c.entry_id not in used
            and _days_apart(c.date, line.value_date) <= date_tolerance_days
        ]
        if cands:
            it = min(
                cands, key=lambda c: (_days_apart(c.date, line.value_date), c.ref or "", c.entry_id)
            )
            used.add(it.entry_id)
            report.items.append(
                Classified(
                    Recon.MATCHED_FUZZY,
                    it.ref,
                    line.line_id,
                    it.entry_id,
                    it.amount,
                    line.amount,
                    it.date,
                    line.value_date,
                )
            )
        else:
            report.items.append(
                Classified(
                    Recon.MISSING_IN_LEDGER,
                    line.reference,
                    line.line_id,
                    None,
                    None,
                    line.amount,
                    None,
                    line.value_date,
                )
            )

    # Pass 3: ledger items the bank never reported.
    for it in sorted(items, key=lambda i: (i.date, i.ref or "")):
        if it.entry_id not in used:
            report.items.append(
                Classified(
                    Recon.MISSING_IN_BANK, it.ref, None, it.entry_id, it.amount, None, it.date, None
                )
            )
    return report


def score(report: ReconReport, injected: list[InjectedBreak]) -> dict[str, Any]:
    """Precision/recall of break classification against injected ground truth."""
    truth = {(b.kind, b.key) for b in injected}
    found = {(c.kind, c.key) for c in report.breaks}
    per_kind: dict[str, dict[str, Any]] = {}
    for kind in BREAKS:
        t = {k for k in truth if k[0] is kind}
        f = {k for k in found if k[0] is kind}
        tp = len(t & f)
        per_kind[kind.value] = {
            "injected": len(t),
            "detected": len(f),
            "true_positive": tp,
            "precision": round(tp / len(f), 4) if f else 1.0,
            "recall": round(tp / len(t), 4) if t else 1.0,
        }
    tp = len(truth & found)
    return {
        "injected_breaks": len(truth),
        "reported_breaks": len(found),
        "correctly_classified": tp,
        "precision": round(tp / len(found), 4) if found else 1.0,
        "recall": round(tp / len(truth), 4) if truth else 1.0,
        "per_kind": per_kind,
    }


def ledger_items_from_activity(activity: list[dict[str, Any]]) -> list[LedgerItem]:
    """Adapt ``Ledger.account_activity`` rows (asset account: + = cash in)."""
    return [
        LedgerItem(
            a["entry_id"], a["external_ref"], a["effective_date"], a["amount"], a["currency"]
        )
        for a in activity
    ]
