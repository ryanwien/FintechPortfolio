"""Ranking and calibration metrics for rare-event classifiers.

Alert-rate metrics answer the operational question "if analysts can look at the top r% of
traffic, how much fraud do they see?"; ties are broken by input order (stable sort).
"""

from __future__ import annotations

import math
from typing import Any, Final

import numpy as np
from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score

from fraud.config import ALERT_RATE_PRIMARY, ALERT_RATE_WIDE

# Uneven bins: almost all mass sits below 5%, so uniform deciles would hide the action.
RELIABILITY_EDGES: Final = [0.0, 0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.4, 0.7, 1.0]
# Report keys; the names are part of the metrics.json contract.
KEY_RECALL_PRIMARY: Final = "recall_at_0.5pct_alerts"
KEY_RECALL_WIDE: Final = "recall_at_2pct_alerts"


def recall_at_alert_rate(scores: np.ndarray, y: np.ndarray, rate: float) -> dict[str, Any]:
    """Recall and precision when alerting on the top ``rate`` share of traffic (counts only)."""
    n_alert = max(1, math.ceil(rate * len(scores)))
    order = np.argsort(-scores, kind="stable")
    top = order[:n_alert]
    caught = int(y[top].sum())
    return {
        "alert_rate": rate,
        "n_alerts": n_alert,
        "recall": caught / max(1, int(y.sum())),
        "precision": caught / n_alert,
        "threshold": float(scores[top[-1]]),
    }


def reliability(
    scores: np.ndarray, y: np.ndarray, edges: list[float] | None = None
) -> list[dict[str, Any]]:
    """Calibration table: mean predicted vs observed rate per score bin (empty bins dropped)."""
    edges = edges or RELIABILITY_EDGES
    idx = np.clip(np.searchsorted(edges, scores, side="right") - 1, 0, len(edges) - 2)
    out = []
    for b in range(len(edges) - 1):
        mask = idx == b
        n = int(mask.sum())
        if n == 0:
            continue
        out.append(
            {
                "bin": [edges[b], edges[b + 1]],
                "n": n,
                "mean_predicted": float(scores[mask].mean()),
                "observed_rate": float(y[mask].mean()),
            }
        )
    return out


def classification_report(scores: np.ndarray, y: np.ndarray, probabilistic: bool) -> dict[str, Any]:
    """PR/ROC-AUC and alert-rate recall; plus Brier and reliability for probabilistic scores."""
    rep: dict[str, Any] = {
        "n": len(y),
        "n_fraud": int(y.sum()),
        "pr_auc": float(average_precision_score(y, scores)),
        "roc_auc": float(roc_auc_score(y, scores)),
        KEY_RECALL_PRIMARY: recall_at_alert_rate(scores, y, ALERT_RATE_PRIMARY),
        # 0.5% alerts cannot cover a ~1.5% fraud rate; 2% shows the headroom beyond it.
        KEY_RECALL_WIDE: recall_at_alert_rate(scores, y, ALERT_RATE_WIDE),
    }
    if probabilistic:
        rep["brier"] = float(brier_score_loss(y, scores))
        rep["brier_base_rate"] = float(brier_score_loss(y, np.full(len(y), y.mean())))
        rep["reliability"] = reliability(scores, y)
    return rep
