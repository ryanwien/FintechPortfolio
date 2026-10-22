"""Cost-sensitive decisioning: pick approve / review / decline bands by expected dollar loss.

All money is integer cents. Rates are integer basis points so per-transaction costs stay exact.

Per-transaction cost of each action (y = 1 for fraud):

* approve: ``y * amount``                          - the fraud is charged back to us
* review:  ``review_cost + y * amount * (1 - catch) + (1 - y) * review_friction``
* decline: ``(1 - y) * (amount * margin + decline_friction)`` - lost margin + customer friction

Bands are chosen on one period and frozen; :func:`evaluate_bands` then reports what they cost
on later periods.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Literal

import numpy as np

from fraud.config import BPS, CostConfig

__all__ = [
    "Bands",
    "CostConfig",
    "Decision",
    "action_costs",
    "approve_all_loss",
    "candidate_thresholds",
    "decline_curve",
    "evaluate_bands",
    "optimize_bands",
]

Decision = Literal["approve", "review", "decline"]
APPROVE: Final = 0  # column / action index in the cost matrix
REVIEW: Final = 1
DECLINE: Final = 2
N_GRID: Final = 250  # candidate thresholds per band edge
GRID_TAIL_LOG10: Final = -4.5  # densest candidate sits at the top 10^-4.5 of scores
CURVE_POINTS: Final = 40
CURVE_TAIL_LOG10: Final = -4.0
CURVE_MAX_DECLINE_SHARE: Final = 0.3


def action_costs(y: np.ndarray, amount: np.ndarray, cfg: CostConfig) -> np.ndarray:
    """(n, 3) int64 matrix of costs in cents for [approve, review, decline]."""
    y = y.astype(np.int64)
    amount = amount.astype(np.int64)
    approve = y * amount
    review = (
        cfg.review_cost_cents
        + y * (amount * (BPS - cfg.review_catch_bps) // BPS)
        + (1 - y) * cfg.review_friction_cents
    )
    decline = (1 - y) * (amount * cfg.decline_margin_bps // BPS + cfg.decline_friction_cents)
    return np.column_stack([approve, review, decline]).astype(np.int64)


@dataclass(frozen=True, slots=True)
class Bands:
    """Score cut-offs: ``[review_at, decline_at)`` -> review, ``>= decline_at`` -> decline."""

    review_at: float  # score >= review_at -> review (unless >= decline_at)
    decline_at: float  # score >= decline_at -> decline

    def decide(self, score: float) -> Decision:
        """Return the decision for one score: decline first, then review, otherwise approve."""
        if score >= self.decline_at:
            return "decline"
        if score >= self.review_at:
            return "review"
        return "approve"

    def decide_many(self, scores: np.ndarray) -> np.ndarray:
        """Vectorised :meth:`decide` as action indices (APPROVE / REVIEW / DECLINE)."""
        out = np.full(len(scores), APPROVE, dtype=np.int64)
        out[scores >= self.review_at] = REVIEW
        out[scores >= self.decline_at] = DECLINE
        return out


def candidate_thresholds(scores: np.ndarray, n_grid: int = N_GRID) -> np.ndarray:
    """Threshold grid: every unique score if few, else quantiles dense in the upper tail.

    Always ends with ``inf`` (= "nobody in this band").
    """
    uniq = np.unique(scores)
    if len(uniq) <= n_grid:
        cands = uniq
    else:
        # Dense in the upper tail where decisions happen.
        q = 1.0 - np.logspace(GRID_TAIL_LOG10, np.log10(0.5), n_grid)
        cands = np.unique(np.quantile(scores, q, method="higher"))
    return np.append(cands, np.inf)  # inf = "nobody in this band"


def optimize_bands(
    scores: np.ndarray, y: np.ndarray, amount: np.ndarray, cfg: CostConfig, n_grid: int = N_GRID
) -> tuple[Bands, int]:
    """Exhaustive search over (review_at <= decline_at) pairs; returns bands and loss cents."""
    costs = action_costs(y, amount, cfg)
    order = np.argsort(scores, kind="stable")
    s = scores[order]
    c = costs[order]
    prefix = np.vstack([np.zeros((1, 3), dtype=np.int64), np.cumsum(c, axis=0)])
    cands = candidate_thresholds(scores, n_grid)
    idx = np.searchsorted(s, cands, side="left")  # number of txns with score < threshold
    n = len(s)
    ir = idx[:, None]  # review threshold on rows
    idd = idx[None, :]  # decline threshold on columns
    approve = prefix[ir, APPROVE]
    review = prefix[idd, REVIEW] - prefix[ir, REVIEW]
    decline = prefix[n, DECLINE] - prefix[idd, DECLINE]
    loss = approve + review + decline
    valid = (cands[:, None] <= cands[None, :]) & ((idd - ir) <= cfg.max_review_rate * n)
    loss = np.where(valid, loss, np.iinfo(np.int64).max)
    r, d = np.unravel_index(int(np.argmin(loss)), loss.shape)
    return Bands(float(cands[r]), float(cands[d])), int(loss[r, d])


def evaluate_bands(
    scores: np.ndarray, y: np.ndarray, amount: np.ndarray, bands: Bands, cfg: CostConfig
) -> dict[str, Any]:
    """Expected loss, action counts and fraud dollars by action for fixed ``bands``."""
    costs = action_costs(y, amount, cfg)
    act = bands.decide_many(scores)
    y = y.astype(bool)
    amount = amount.astype(np.int64)
    loss = int(costs[np.arange(len(act)), act].sum())
    fraud_cents = int(amount[y].sum())
    approved, reviewed, declined = act == APPROVE, act == REVIEW, act == DECLINE
    out: dict[str, Any] = {
        "expected_loss_cents": loss,
        "approve": int(approved.sum()),
        "review": int(reviewed.sum()),
        "decline": int(declined.sum()),
        "review_rate": float(reviewed.mean()),
        "decline_rate": float(declined.mean()),
        "fraud_txns": int(y.sum()),
        "fraud_cents": fraud_cents,
        "fraud_cents_declined": int(amount[y & declined].sum()),
        "fraud_cents_reviewed": int(amount[y & reviewed].sum()),
        "fraud_cents_approved": int(amount[y & approved].sum()),
        "false_declines": int((~y & declined).sum()),
        "good_reviews": int((~y & reviewed).sum()),
    }
    leaked = amount * (BPS - cfg.review_catch_bps) // BPS
    out["loss_breakdown_cents"] = {
        "fraud_missed": int(amount[y & approved].sum() + leaked[y & reviewed].sum()),
        "false_decline": int(costs[declined, DECLINE].sum()),
        "review_ops": int(
            reviewed.sum() * cfg.review_cost_cents
            + (~y & reviewed).sum() * cfg.review_friction_cents
        ),
    }
    return out


def approve_all_loss(y: np.ndarray, amount: np.ndarray) -> int:
    """Loss in cents with no fraud screening at all: every fraud is charged back."""
    return int(amount.astype(np.int64)[y.astype(bool)].sum())


def decline_curve(
    scores: np.ndarray,
    y: np.ndarray,
    amount: np.ndarray,
    cfg: CostConfig,
    n_points: int = CURVE_POINTS,
) -> tuple[list[float], list[int]]:
    """Expected loss (cents) vs a single decline threshold, no review band."""
    costs = action_costs(y, amount, cfg)
    q = 1.0 - np.logspace(CURVE_TAIL_LOG10, np.log10(CURVE_MAX_DECLINE_SHARE), n_points)
    ths = np.unique(np.quantile(scores, q, method="higher"))
    xs, ys = [], []
    for t in ths:
        dec = scores >= t
        xs.append(float(t))
        ys.append(int(costs[~dec, APPROVE].sum() + costs[dec, DECLINE].sum()))
    return xs, ys
