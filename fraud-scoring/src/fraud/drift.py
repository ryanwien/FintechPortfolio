"""Population Stability Index (PSI) drift monitoring.

Bins are always fitted on the reference (training) sample, so the same reference gives the
same bins for every comparison. Empty bins are floored at a small epsilon to keep PSI finite.
"""

from __future__ import annotations

from typing import Any, Final, Literal

import numpy as np

from fraud.config import PSI_ALERT, PSI_BINS, PSI_WARN

__all__ = ["PSI_ALERT", "PSI_WARN", "DriftStatus", "psi", "psi_table", "status"]

DriftStatus = Literal["ok", "warn", "ALERT"]
_EPS: Final = 1e-4


def psi(expected: np.ndarray, actual: np.ndarray, n_bins: int = PSI_BINS) -> float:
    """PSI with bins fitted on ``expected``.

    Continuous features use quantile bins. Low-cardinality features (flags, small counts)
    get one bin per observed value: quantile edges would collapse a 5%-ones flag into a
    single bin and hide any shift in it.
    """
    expected = np.asarray(expected, dtype=np.float64)
    actual = np.asarray(actual, dtype=np.float64)
    # Bin edges come from the reference only, so every comparison uses the same bins.
    uniq = np.unique(expected)
    # Few distinct values: put an edge midway between each pair so each value gets a bin.
    if len(uniq) <= n_bins:
        edges = (uniq[:-1] + uniq[1:]) / 2.0
    else:
        # Interior quantiles give roughly equal-mass bins; np.unique drops edges repeated by ties.
        qs = np.linspace(0.0, 1.0, n_bins + 1)[1:-1]
        edges = np.unique(np.quantile(expected, qs))
    # Both samples use the reference edges, so their bin shares are directly comparable.
    e_bin = np.searchsorted(edges, expected, side="right")
    a_bin = np.searchsorted(edges, actual, side="right")
    k = len(edges) + 1
    # Counts to shares; minlength keeps empty bins so the two arrays line up.
    e = np.bincount(e_bin, minlength=k) / max(1, len(expected))
    a = np.bincount(a_bin, minlength=k) / max(1, len(actual))
    # Floor empty bins so log(a / e) stays finite when a bin is empty in one sample.
    e = np.clip(e, _EPS, None)
    a = np.clip(a, _EPS, None)
    # PSI = sum of (actual - expected) * ln(actual / expected); zero when the samples match.
    return float(np.sum((a - e) * np.log(a / e)))


def status(value: float) -> DriftStatus:
    """Map a PSI value to ``ok`` / ``warn`` / ``ALERT``."""
    if value >= PSI_ALERT:
        return "ALERT"
    if value >= PSI_WARN:
        return "warn"
    return "ok"


def psi_table(
    reference: np.ndarray, current: np.ndarray, names: list[str], n_bins: int = PSI_BINS
) -> list[dict[str, Any]]:
    """Per-column PSI rows ``{feature, psi, status}``, most-drifted first."""
    rows = []
    for j, name in enumerate(names):
        v = psi(reference[:, j], current[:, j], n_bins)
        rows.append({"feature": name, "psi": v, "status": status(v)})
    # Most-drifted feature first, so the report leads with the worst shift.
    return sorted(rows, key=lambda r: -r["psi"])
