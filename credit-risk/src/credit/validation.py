"""Model evaluation: discrimination, stability, cutoff, fairness and adverse action evidence.

Each function scores already-fitted models on fixed samples; nothing here fits a model or
draws random numbers. Cutoffs are chosen on the in-time holdout from model PDs only, so no
out-of-time outcome leaks into a decision that is then evaluated out of time.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Final

import numpy as np
import pandas as pd

from credit.challenger import Challenger, monotonic_constraints
from credit.config import N_SAMPLE_NOTICES, SEARCH_CUTOFFS, TOP_N_REASONS
from credit.data import PROTECTED, TARGET
from credit.fairness import FairnessSummary, fairness_summary
from credit.metrics import discrimination, psi, psi_from_counts
from credit.reasons import STATEMENT_BY_CODE, reason_codes
from credit.scorecard import Scorecard
from credit.serialize import SCORE_DECIMALS, rnd
from credit.strategy import UnitEconomics, cutoff_table, profit_maximising_cutoff
from credit.woe import iv_strength

CENTS_PER_DOLLAR: Final[int] = 100


def loan_cents(df: pd.DataFrame) -> pd.Series:
    """Requested loan amount in integer cents."""
    return df["loan_amount"] * CENTS_PER_DOLLAR


@dataclass(frozen=True, slots=True)
class ScorecardEvaluation:
    """Validation results for one candidate scorecard."""

    features: list[str]
    dropped_for_sign: list[str]
    in_time: dict[str, float]
    oot: dict[str, float]
    score_psi_dev_vs_oot: float
    cutoff: int
    oot_approval_rate: float
    fairness: FairnessSummary
    scores_oot: np.ndarray
    pd_oot: np.ndarray


@dataclass(frozen=True, slots=True)
class ChallengerEvaluation:
    """Validation results for the challenger at the champion's approval rate."""

    features: list[str]
    monotonic_constraints: dict[str, int]
    in_time: dict[str, float]
    oot: dict[str, float]
    pd_psi_dev_vs_oot: float
    oot_approval_rate: float
    fairness: FairnessSummary


def evaluate_scorecard(
    sc: Scorecard,
    dev_tr: pd.DataFrame,
    dev_te: pd.DataFrame,
    oot: pd.DataFrame,
    econ: UnitEconomics,
) -> ScorecardEvaluation:
    """Score all samples, pick the profit cutoff on the holdout and evaluate it out of time."""
    s_tr, s_te, s_oot = sc.score(dev_tr), sc.score(dev_te), sc.score(oot)
    pd_te, pd_oot = sc.predict_pd(dev_te), sc.predict_pd(oot)
    search = cutoff_table(s_te, pd_te, dev_te[TARGET], loan_cents(dev_te), SEARCH_CUTOFFS, econ)
    cutoff = profit_maximising_cutoff(search)
    approved = s_oot >= cutoff
    return ScorecardEvaluation(
        features=sc.features,
        dropped_for_sign=sc.dropped_for_sign,
        in_time=discrimination(dev_te[TARGET], pd_te),
        oot=discrimination(oot[TARGET], pd_oot),
        score_psi_dev_vs_oot=psi(s_tr, s_oot),
        cutoff=cutoff,
        oot_approval_rate=float(approved.mean()),
        fairness=fairness_summary(approved, oot[TARGET], oot[PROTECTED].to_numpy()),
        scores_oot=s_oot,
        pd_oot=pd_oot,
    )


def evaluate_challenger(
    challenger: Challenger,
    dev_tr: pd.DataFrame,
    dev_te: pd.DataFrame,
    oot: pd.DataFrame,
    approval_rate: float,
) -> ChallengerEvaluation:
    """Evaluate the challenger, approving the same OOT share as the champion (like-for-like)."""
    pd_te = challenger.predict_pd(dev_te)
    pd_oot = challenger.predict_pd(oot)
    threshold = np.quantile(pd_oot, approval_rate)
    approved = pd_oot <= threshold
    return ChallengerEvaluation(
        features=challenger.features,
        monotonic_constraints=monotonic_constraints(challenger.numeric),
        in_time=discrimination(dev_te[TARGET], pd_te),
        oot=discrimination(oot[TARGET], pd_oot),
        pd_psi_dev_vs_oot=psi(challenger.predict_pd(dev_tr), pd_oot),
        oot_approval_rate=float(approved.mean()),
        fairness=fairness_summary(approved, oot[TARGET], oot[PROTECTED].to_numpy()),
    )


def feature_iv_rows(
    champion: Scorecard, dev_tr: pd.DataFrame, oot: pd.DataFrame
) -> list[dict[str, object]]:
    """IV, strength, coefficient and dev-vs-OOT CSI per candidate characteristic, by IV."""
    rows: list[dict[str, object]] = []
    for f, b in champion.binner.binnings.items():
        dev_cnt = np.bincount(b.bin_index(dev_tr[f]).clip(0), minlength=len(b.labels))
        oot_cnt = np.bincount(b.bin_index(oot[f]).clip(0), minlength=len(b.labels))
        rows.append(
            {
                "feature": f,
                "iv": rnd(b.iv),
                "strength": iv_strength(b.iv),
                "bins": len(b.labels),
                "in_model": f in champion.features,
                "coefficient": rnd(champion.coef_[f]) if f in champion.coef_ else None,
                "csi_dev_vs_oot": rnd(psi_from_counts(dev_cnt, oot_cnt)),
            }
        )
    rows.sort(key=lambda r: -r["iv"])
    return rows


def adverse_action_summary(
    champion: Scorecard, oot: pd.DataFrame, scores_oot: np.ndarray, cutoff: int
) -> dict[str, object]:
    """Reason-code mix for OOT declines at ``cutoff`` plus a few sample notices."""
    declined = oot[scores_oot < cutoff]
    reasons = reason_codes(champion.points_matrix(declined), champion.max_points)
    primary = Counter(r[0].code for r in reasons if r)
    any_top = Counter(x.code for r in reasons for x in r)
    samples = [
        {
            "application_id": int(declined.iloc[i]["application_id"]),
            "score": rnd(scores_oot[declined.index[i]], SCORE_DECIMALS),
            "reasons": [
                {
                    "code": r.code,
                    "statement": r.statement,
                    "points_lost": rnd(r.points_lost, SCORE_DECIMALS),
                }
                for r in reasons[i]
            ],
        }
        for i in range(N_SAMPLE_NOTICES)
    ]
    # Keys name the Reg B maximum of four reasons; TOP_N_REASONS is pinned to it in config.
    return {
        "declined_oot": len(declined),
        "declined_with_4_reasons": int(sum(len(r) == TOP_N_REASONS for r in reasons)),
        "primary_reason_counts": _reason_counts(primary),
        "top4_reason_counts": _reason_counts(any_top),
        "samples": samples,
    }


def _reason_counts(counts: Counter[str]) -> dict[str, dict[str, object]]:
    """Pair each adverse-action reason code with its statement and count, most frequent first.

    Raises KeyError for a code that has no entry in ``STATEMENT_BY_CODE``.
    """
    return {c: {"statement": STATEMENT_BY_CODE[c], "count": n} for c, n in counts.most_common()}
