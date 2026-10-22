"""Cutoff strategy: approval rate vs loss trade-off and a profit-maximising cutoff.

All money is integer cents; per-loan expected values are computed in ``Decimal`` and
rounded half-even to the cent before aggregation.

Simple unit economics (documented assumptions, not a pricing model):
  * a good loan earns ``lifetime_margin`` x principal: net interest income over its life
    after funding and servicing cost;
  * a bad loan loses ``ead_fraction`` x principal (average balance outstanding at default)
    x ``lgd`` (loss given default after recoveries), and earns nothing.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Final, TypedDict

import numpy as np

from credit.config import EAD_FRACTION, LGD, LIFETIME_MARGIN

CENT: Final[Decimal] = Decimal("1")  # quantum for amounts already held in cents
_PD_DECIMALS: Final[int] = 6  # model PD precision carried into Decimal arithmetic
_BREAKEVEN_QUANTUM: Final[Decimal] = Decimal("0.0001")


@dataclass(frozen=True, slots=True)
class UnitEconomics:
    """Per-loan economics as fractions of principal."""

    lifetime_margin: Decimal = LIFETIME_MARGIN
    lgd: Decimal = LGD
    ead_fraction: Decimal = EAD_FRACTION

    @property
    def breakeven_pd(self) -> Decimal:
        """PD at which a loan's expected profit is zero."""
        loss = self.lgd * self.ead_fraction
        return self.lifetime_margin / (self.lifetime_margin + loss)

    def as_dict(self) -> dict[str, str]:
        """Assumptions plus break-even PD as strings (exact, JSON-safe)."""
        d = {k: str(v) for k, v in asdict(self).items()}
        d["breakeven_pd"] = str(self.breakeven_pd.quantize(_BREAKEVEN_QUANTUM))
        return d


class CutoffRow(TypedDict):
    """Outcome of approving every applicant with ``score >= cutoff`` (key order = JSON order)."""

    cutoff: int
    approval_rate: float
    approved: int
    bad_rate_approved: float
    booked_cents: int
    expected_loss_rate: float
    realized_loss_rate: float
    expected_profit_cents: int
    realized_profit_cents: int


def _cents(x: Decimal) -> int:
    """Round a Decimal amount to whole cents with half-to-even rounding, as an int."""
    return int(x.quantize(CENT, rounding=ROUND_HALF_EVEN))


def _decimal_pd(pd_bad: float) -> Decimal:
    """Model PD as an exact Decimal, rounded so float noise cannot change a cent."""
    return Decimal(repr(round(float(pd_bad), _PD_DECIMALS)))


def realized_profit_cents(loan_cents: int, defaulted: bool, econ: UnitEconomics) -> int:
    """Realised profit (negative for a loss) on one loan given its observed outcome."""
    principal = Decimal(int(loan_cents))
    if defaulted:
        return -_cents(principal * econ.ead_fraction * econ.lgd)
    return _cents(principal * econ.lifetime_margin)


def expected_profit_cents(loan_cents: int, pd_bad: float, econ: UnitEconomics) -> int:
    """Expected profit on one loan at model PD ``pd_bad``."""
    principal = Decimal(int(loan_cents))
    p = _decimal_pd(pd_bad)
    gain = (1 - p) * principal * econ.lifetime_margin
    loss = p * principal * econ.ead_fraction * econ.lgd
    return _cents(gain - loss)


def expected_loss_cents(loan_cents: int, pd_bad: float, econ: UnitEconomics) -> int:
    """Expected credit loss on one loan at model PD ``pd_bad``."""
    p = _decimal_pd(pd_bad)
    return _cents(p * Decimal(int(loan_cents)) * econ.ead_fraction * econ.lgd)


def cutoff_table(
    scores: np.ndarray,
    pd_hat: np.ndarray,
    y_bad: np.ndarray,
    loan_cents: np.ndarray,
    cutoffs: Sequence[int],
    econ: UnitEconomics,
) -> list[CutoffRow]:
    """One row per cutoff: approve when score >= cutoff."""
    scores = np.asarray(scores, dtype=float)
    y = np.asarray(y_bad, dtype=int)
    loans = np.asarray(loan_cents, dtype=np.int64)
    exp_profit = np.array(
        [expected_profit_cents(int(c), p, econ) for c, p in zip(loans, pd_hat, strict=True)],
        dtype=np.int64,
    )
    exp_loss = np.array(
        [expected_loss_cents(int(c), p, econ) for c, p in zip(loans, pd_hat, strict=True)],
        dtype=np.int64,
    )
    real_profit = np.array(
        [realized_profit_cents(int(c), bool(b), econ) for c, b in zip(loans, y, strict=True)],
        dtype=np.int64,
    )
    real_loss = np.where(y == 1, -real_profit, 0)
    rows: list[CutoffRow] = []
    n = len(scores)
    for cut in cutoffs:
        a = scores >= cut
        k = int(a.sum())
        booked = int(loans[a].sum())
        rows.append(
            {
                "cutoff": int(cut),
                "approval_rate": k / n,
                "approved": k,
                "bad_rate_approved": float(y[a].mean()) if k else 0.0,
                "booked_cents": booked,
                "expected_loss_rate": int(exp_loss[a].sum()) / booked if booked else 0.0,
                "realized_loss_rate": int(real_loss[a].sum()) / booked if booked else 0.0,
                "expected_profit_cents": int(exp_profit[a].sum()),
                "realized_profit_cents": int(real_profit[a].sum()),
            }
        )
    return rows


def profit_maximising_cutoff(table: Sequence[CutoffRow]) -> int:
    """Cutoff with the highest expected profit (ties -> the more conservative cutoff)."""
    best = max(table, key=lambda r: (r["expected_profit_cents"], r["cutoff"]))
    return int(best["cutoff"])


def fmt_usd(cents: int) -> str:
    """Format integer cents as ``-$1,234.56``."""
    sign = "-" if cents < 0 else ""
    d, c = divmod(abs(int(cents)), 100)
    return f"{sign}${d:,}.{c:02d}"
