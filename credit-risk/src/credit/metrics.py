"""Discrimination, calibration and stability metrics used in model validation.

All functions take the bad flag (1 = default) and a predicted PD where higher means riskier.
PSI bands follow the industry convention used in the monitoring plan of the model card.
"""

from __future__ import annotations

from typing import Final

import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score

PSI_AMBER: Final[float] = 0.10  # at or above: moderate shift, investigate
PSI_RED: Final[float] = 0.25  # at or above: significant shift, act
_PSI_SMOOTHING: Final[float] = 0.5  # added to each bin count so empty bins stay finite
N_DECILES: Final[int] = 10


def auc(y_bad: np.ndarray, pd_hat: np.ndarray) -> float:
    """Area under the ROC curve."""
    return float(roc_auc_score(np.asarray(y_bad, dtype=int), np.asarray(pd_hat, dtype=float)))


def gini(y_bad: np.ndarray, pd_hat: np.ndarray) -> float:
    """Gini coefficient (accuracy ratio), 2 * AUC - 1."""
    return 2.0 * auc(y_bad, pd_hat) - 1.0


def ks(y_bad: np.ndarray, pd_hat: np.ndarray) -> float:
    """Max distance between cumulative bad and good distributions over the risk ranking."""
    y = np.asarray(y_bad, dtype=int)
    p = np.asarray(pd_hat, dtype=float)
    order = np.argsort(-p, kind="mergesort")
    y_sorted = y[order]
    p_sorted = p[order]
    cum_bad = np.cumsum(y_sorted) / max(y.sum(), 1)
    cum_good = np.cumsum(1 - y_sorted) / max((1 - y).sum(), 1)
    # evaluate only at the last index of each tied block of scores
    last_of_tie = np.r_[p_sorted[1:] != p_sorted[:-1], True]
    return float(np.max(np.abs(cum_bad - cum_good)[last_of_tie]))


def calibration_by_decile(
    y_bad: np.ndarray, pd_hat: np.ndarray, n: int = N_DECILES
) -> pd.DataFrame:
    """Predicted vs observed default rate by predicted-PD decile (decile 1 = riskiest)."""
    df = pd.DataFrame({"y": np.asarray(y_bad, dtype=int), "p": np.asarray(pd_hat, dtype=float)})
    ranks = df["p"].rank(method="first", ascending=False)
    df["decile"] = np.ceil(ranks / len(df) * n).astype(int).clip(1, n)
    g = df.groupby("decile").agg(
        count=("y", "size"), predicted_pd=("p", "mean"), observed=("y", "mean")
    )
    g["ratio_obs_to_pred"] = g["observed"] / g["predicted_pd"]
    return g.reset_index()


def psi(expected: np.ndarray, actual: np.ndarray, n_bins: int = N_DECILES) -> float:
    """Population Stability Index of ``actual`` vs ``expected`` on expected-quantile bins."""
    e = np.asarray(expected, dtype=float)
    a = np.asarray(actual, dtype=float)
    edges = np.unique(np.quantile(e, np.linspace(0, 1, n_bins + 1)[1:-1]))
    e_cnt = np.bincount(np.searchsorted(edges, e, side="left"), minlength=len(edges) + 1)
    a_cnt = np.bincount(np.searchsorted(edges, a, side="left"), minlength=len(edges) + 1)
    return psi_from_counts(e_cnt, a_cnt)


def psi_from_counts(expected_counts: np.ndarray, actual_counts: np.ndarray) -> float:
    """PSI from pre-binned counts (also used as the characteristic stability index, CSI)."""
    e = np.asarray(expected_counts, dtype=float) + _PSI_SMOOTHING
    a = np.asarray(actual_counts, dtype=float) + _PSI_SMOOTHING
    e /= e.sum()
    a /= a.sum()
    return float(np.sum((a - e) * np.log(a / e)))


def psi_label(value: float) -> str:
    """Traffic-light interpretation of a PSI value."""
    if value < PSI_AMBER:
        return "stable"
    if value < PSI_RED:
        return "moderate shift - investigate"
    return "significant shift - action required"


def discrimination(y_bad: np.ndarray, pd_hat: np.ndarray) -> dict[str, float]:
    """AUC, Gini and KS in one dict (the order used in every report table)."""
    a = auc(y_bad, pd_hat)
    return {"auc": a, "gini": 2 * a - 1, "ks": ks(y_bad, pd_hat)}
