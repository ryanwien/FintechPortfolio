"""JSON-ready rounding for ``metrics.json``.

Every float written to ``metrics.json`` passes through ``rnd`` so the file is stable across
platforms and diffs cleanly between runs. Rendering modules read only this rounded dict.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final, TypeAlias

from credit.fairness import FairnessSummary

Metrics: TypeAlias = dict[str, Any]
"""The nested metrics dictionary written to ``metrics.json`` and read by every renderer."""

METRIC_DECIMALS: Final[int] = 4
SCORE_DECIMALS: Final[int] = 1  # scores and points are reported to a tenth of a point


def rnd(x: float, nd: int = METRIC_DECIMALS) -> float:
    """Round to ``nd`` decimals as a plain Python float."""
    return round(float(x), nd)


def round_values(d: Mapping[str, float]) -> dict[str, float]:
    """Round every value of a flat ``{name: number}`` mapping."""
    return {k: rnd(v) for k, v in d.items()}


def round_row(r: Mapping[str, object]) -> dict[str, object]:
    """Round the float fields of a table row; ints, bools and strings pass through."""
    return {k: (rnd(v) if isinstance(v, float) else v) for k, v in r.items()}


def round_fairness(f: FairnessSummary) -> dict[str, object]:
    """Rounded copy of a fairness summary, preserving key order."""
    return {
        "approval_rate": round_values(f["approval_rate"]),
        "approval_rate_among_goods": round_values(f["approval_rate_among_goods"]),
        "adverse_impact_ratio": rnd(f["adverse_impact_ratio"]),
        "passes_four_fifths": f["passes_four_fifths"],
        "equal_opportunity_gap": rnd(f["equal_opportunity_gap"]),
    }
