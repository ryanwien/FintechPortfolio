"""ECOA / Regulation B adverse action reason codes from scorecard points.

For each declined applicant, points lost on attribute i = (max points attainable on i) -
(points the applicant received on i). The top four attributes by points lost become the
principal reasons, which Reg B (12 CFR 1002.9) requires to be specific; the Official Staff
Commentary notes that disclosing more than four is unlikely to be helpful.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

import numpy as np
import pandas as pd

from credit.config import TOP_N_REASONS

# Points lost at or below this are float noise, not a reason to cite.
_MIN_POINTS_LOST: Final[float] = 1e-9

# characteristic -> (reason code, Reg B statement)
REASON_CODES: Final[dict[str, tuple[str, str]]] = {
    "annual_income": ("R01", "Income insufficient for amount of credit requested"),
    "dti": ("R02", "Excessive obligations in relation to income"),
    "revolving_utilization": ("R03", "Proportion of revolving balances to credit limits too high"),
    "delinq_2y": ("R04", "Delinquent past or present credit obligations with others"),
    "inquiries_6m": ("R05", "Number of recent inquiries on credit bureau report"),
    "credit_history_months": ("R06", "Length of credit history insufficient"),
    "employment_years": ("R07", "Length of employment"),
    "loan_amount": ("R08", "Amount of credit requested"),
    "term_months": ("R09", "Requested repayment term"),
    "purpose": ("R10", "Purpose of loan"),
    "region": ("R11", "Location of residence"),
}
STATEMENT_BY_CODE: Final[dict[str, str]] = dict(REASON_CODES.values())


@dataclass(frozen=True, slots=True)
class Reason:
    """One adverse action reason cited for one applicant."""

    feature: str
    code: str
    statement: str
    points_lost: float


def reason_codes(
    points: pd.DataFrame, max_points: dict[str, float], top_n: int = TOP_N_REASONS
) -> list[list[Reason]]:
    """Return, for each row of ``points``, up to ``top_n`` reasons ordered by points lost.

    Ties are broken by column order so output is deterministic. Attributes on which the
    applicant lost no points are never cited.
    """
    cols = list(points.columns)
    maxes = np.array([max_points[c] for c in cols])
    lost = maxes[None, :] - points.to_numpy()
    out: list[list[Reason]] = []
    for row in lost:
        order = sorted(range(len(cols)), key=lambda j: (-row[j], j))
        reasons = []
        for j in order[:top_n]:
            if row[j] <= _MIN_POINTS_LOST:
                break
            code, text = REASON_CODES[cols[j]]
            reasons.append(Reason(cols[j], code, text, float(row[j])))
        out.append(reasons)
    return out
