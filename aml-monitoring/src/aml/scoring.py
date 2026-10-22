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
    # Per rule, keep the strongest alert and count how many fired.
    count: dict[str, int] = defaultdict(int)
    for a in alerts:
        best[a.rule_id] = max(best[a.rule_id], a.score)
        count[a.rule_id] += 1
    # p_miss is the probability that every rule is wrong, treating each rule as independent
    # evidence.
    p_miss = 1.0
    contributions = {}
    for rule in sorted(best):
        # Repeats strengthen a rule but are capped at full strength.
        s = min(1.0, best[rule] * (1 + cfg.repeat_bonus * (count[rule] - 1)))
        # A rule's weight times its signal is its contribution to the miss probability.
        c = cfg.rule_weights.get(rule, DEFAULT_RULE_WEIGHT) * s
        contributions[rule] = round(MAX_SCORE * c, 1)
        # Independent evidence: multiply the miss probabilities together.
        p_miss *= 1 - c
    # Convert back: the base score is one minus p_miss, on a 0-100 scale.
    base = MAX_SCORE * (1 - p_miss)
    families = sorted({a.family for a in alerts})
    # Corroboration: each extra typology family beyond the first adds a bonus, up to a cap.
    bonus = cfg.multi_family_bonus * max(0, min(MAX_CORROBORATING_FAMILIES, len(families) - 1))
    # KYC multiplier: the highest risk among the subjects scales the score; with no levels it is
    # 1.0.
    mult = max((cfg.kyc_multiplier[k] for k in kyc_levels), default=1.0)
    # Clamp to the maximum score after applying the bonus and the multiplier.
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
    # Bands run from the highest floor down; the first floor the score reaches gives the label.
    for floor, label in cfg.priority_bands:
        if score >= floor:
            return label
    return LOWEST_PRIORITY
