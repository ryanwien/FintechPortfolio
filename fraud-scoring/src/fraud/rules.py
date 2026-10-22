"""Hand-written rules baseline - the kind of rule set a fraud ops team starts with.

Scores are integer points; higher means riskier. The same cost-based band optimiser used for
the models picks the review/decline point cut-offs, so the comparison is like-for-like.
Rules read the same feature matrix as the models, so they share the point-in-time guarantee.
"""

from __future__ import annotations

from typing import Final

import numpy as np

from fraud.catalog import MCC_ELECTRONICS, MCC_GIFT_CARD, MCC_INDEX, MCC_JEWELRY
from fraud.features import F_INDEX

HIGH_RISK_MCC: Final = [MCC_INDEX[m] for m in (MCC_ELECTRONICS, MCC_JEWELRY, MCC_GIFT_CARD)]
FAR_KM: Final = 300
IMPOSSIBLE_SPEED_KMH: Final = 800

# (name, points, condition). Order matches the columns of :func:`rule_hits`.
RULES: Final[list[tuple[str, int, str]]] = [
    ("velocity", 1, "3+ transactions in the last hour"),
    ("card_testing", 2, "2+ sub-$5 charges in the last hour"),
    ("impossible_travel", 2, ">300 km from previous txn at >800 km/h"),
    ("amount_spike", 1, "amount z-score > 3 and > $300"),
    ("new_device_big", 1, "new device and amount > $200"),
    ("risky_mcc_new_merchant", 1, "electronics/jewelry/gift card, new merchant, > $500"),
    ("shared_device", 2, "device already used by another customer"),
    ("away_new_merchant", 1, ">300 km from usual locations, new merchant, > $150"),
]


def rule_hits(X: np.ndarray) -> np.ndarray:
    """(n, len(RULES)) boolean matrix: which rule fires for which transaction."""
    f = F_INDEX
    # Features hold log1p(dollars); invert it so the rule thresholds are in dollars.
    dollars = np.expm1(X[:, f["amount_log"]])
    # One column per rule, in RULES order; rule_score relies on that order for weights.
    return np.column_stack(
        [
            X[:, f["cnt_1h"]] >= 3,
            X[:, f["small_cnt_1h"]] >= 2,
            (X[:, f["km_from_prev"]] > FAR_KM) & (X[:, f["speed_kmh"]] > IMPOSSIBLE_SPEED_KMH),
            (X[:, f["amount_z"]] > 3) & (dollars > 300),
            (X[:, f["new_device"]] > 0) & (dollars > 200),
            np.isin(X[:, f["mcc"]], HIGH_RISK_MCC)
            & (X[:, f["new_merchant"]] > 0)
            & (dollars > 500),
            X[:, f["device_n_customers"]] >= 1,
            (X[:, f["km_from_centroid"]] > FAR_KM)
            & (X[:, f["new_merchant"]] > 0)
            & (dollars > 150),
        ]
    )


def rule_score(X: np.ndarray) -> np.ndarray:
    """Sum of points of the rules that fire, per transaction."""
    # Points per rule, in the same order as the columns of rule_hits.
    weights = np.array([w for _, w, _ in RULES], dtype=np.float64)
    return rule_hits(X).astype(np.float64) @ weights
