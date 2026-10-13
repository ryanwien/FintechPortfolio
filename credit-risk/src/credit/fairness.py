"""Fair-lending disparity metrics computed on decisions at a chosen cutoff.

The protected attribute is joined to decisions after scoring; it is never a model input.
"""

from __future__ import annotations

from typing import TypedDict

import numpy as np

from credit.config import FOUR_FIFTHS, PROTECTED_GROUP, REFERENCE_GROUP


class FairnessSummary(TypedDict):
    """Disparity metrics for one set of decisions (key order is the JSON order)."""

    approval_rate: dict[str, float]
    approval_rate_among_goods: dict[str, float]
    adverse_impact_ratio: float
    passes_four_fifths: bool
    equal_opportunity_gap: float


def approval_rates(approved: np.ndarray, group: np.ndarray) -> dict[str, float]:
    """Approval rate per group label, keyed in sorted label order."""
    approved = np.asarray(approved, dtype=bool)
    group = np.asarray(group)
    return {str(g): float(approved[group == g].mean()) for g in sorted(set(group.tolist()))}


def adverse_impact_ratio(
    approved: np.ndarray, group: np.ndarray, protected: str, reference: str
) -> float:
    """Approval rate of the protected group divided by that of the reference group."""
    rates = approval_rates(approved, group)
    return rates[protected] / rates[reference]


def equal_opportunity_gap(
    approved: np.ndarray, y_bad: np.ndarray, group: np.ndarray, protected: str, reference: str
) -> float:
    """Difference in approval rate among applicants who actually repaid (reference - protected).

    "Opportunity" in lending is being approved when creditworthy, so the positive class is a
    good (non-defaulting) loan and the rate compared is the true-positive rate of approval.
    """
    approved = np.asarray(approved, dtype=bool)
    good = np.asarray(y_bad, dtype=int) == 0
    group = np.asarray(group)
    tpr_ref = approved[good & (group == reference)].mean()
    tpr_prot = approved[good & (group == protected)].mean()
    return float(tpr_ref - tpr_prot)


def fairness_summary(
    approved: np.ndarray,
    y_bad: np.ndarray,
    group: np.ndarray,
    protected: str = PROTECTED_GROUP,
    reference: str = REFERENCE_GROUP,
) -> FairnessSummary:
    """Approval rates, AIR with the four-fifths screen, and the equal-opportunity gap."""
    rates = approval_rates(approved, group)
    air = rates[protected] / rates[reference]
    good = np.asarray(y_bad, dtype=int) == 0
    good_rates = approval_rates(np.asarray(approved)[good], np.asarray(group)[good])
    return {
        "approval_rate": rates,
        "approval_rate_among_goods": good_rates,
        "adverse_impact_ratio": air,
        "passes_four_fifths": bool(air >= FOUR_FIFTHS),
        "equal_opportunity_gap": equal_opportunity_gap(
            approved, y_bad, group, protected, reference
        ),
    }
