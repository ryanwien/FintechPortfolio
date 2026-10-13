"""Risk scoring: combine alert signals with customer KYC risk to prioritise cases.

Per rule, the strongest alert counts (repeats add a small bonus); rules combine as
independent evidence ``1 - prod(1 - w_r * s_r)``; corroboration across distinct
typology families adds a bonus; the result is scaled by the highest KYC risk among the
case subjects and capped at 100.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Final

from .alerts import Alert
from .config import ScoringConfig

DEFAULT_RULE_WEIGHT: Final = 0.3
MAX_SCORE: Final = 100.0
MAX_CORROBORATING_FAMILIES: Final = 3  # extra families beyond the first that earn a bonus
LOWEST_PRIORITY: Final = "P4"


def case_score(
    alerts: list[Alert], kyc_levels: list[str], cfg: ScoringConfig
) -> tuple[float, dict[str, Any]]:
    """Case score (0-100, one decimal) and its breakdown for the case file."""
    best: dict[str, float] = defaultdict(float)
    count: dict[str, int] = defaultdict(int)
    for a in alerts:
        best[a.rule_id] = max(best[a.rule_id], a.score)
        count[a.rule_id] += 1
    p_miss = 1.0
    contributions = {}
    for rule in sorted(best):
        s = min(1.0, best[rule] * (1 + cfg.repeat_bonus * (count[rule] - 1)))
        c = cfg.rule_weights.get(rule, DEFAULT_RULE_WEIGHT) * s
        contributions[rule] = round(MAX_SCORE * c, 1)
        p_miss *= 1 - c
    base = MAX_SCORE * (1 - p_miss)
    families = sorted({a.family for a in alerts})
    bonus = cfg.multi_family_bonus * max(0, min(MAX_CORROBORATING_FAMILIES, len(families) - 1))
    mult = max((cfg.kyc_multiplier[k] for k in kyc_levels), default=1.0)
    score = round(min(MAX_SCORE, (base + bonus) * mult), 1)
    breakdown = {
        "rule_contributions": contributions,
        "families": families,
        "corroboration_bonus": bonus,
        "kyc_multiplier": mult,
    }
    return score, breakdown


def priority(score: float, cfg: ScoringConfig) -> str:
    """Priority band label for a case score (``P1`` highest)."""
    for floor, label in cfg.priority_bands:
        if score >= floor:
            return label
    return LOWEST_PRIORITY
