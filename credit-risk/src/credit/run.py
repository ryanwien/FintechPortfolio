"""End-to-end run: data -> champion/challenger -> validation -> fairness -> strategy -> docs.

python -m credit.run

Reads top to bottom as the model-development story. Fitting and evaluation live in their
own modules; this file decides between candidates, assembles ``metrics.json`` (its key
order is the published schema) and writes every file under ``results/``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final

import numpy as np
import pandas as pd

from credit import model_card, report, site
from credit.challenger import Challenger
from credit.config import (
    APPROVE_ALL_CUTOFF,
    CHALLENGER_MIN_UPLIFT,
    CHALLENGER_SEED,
    DEV_MONTHS,
    HOLDOUT_FRACTION,
    N_APPLICANTS,
    PROTECTED_GROUP,
    PROXY_AUC_TOLERANCE,
    REFERENCE_GROUP,
    RESULTS_DIR,
    SEED,
    TABLE_CUTOFFS,
)
from credit.data import (
    CATEGORICAL_FEATURES,
    MONTH,
    NUMERIC_FEATURES,
    PROTECTED,
    PROXY_FEATURES,
    TARGET,
    generate_applicants,
    split_dev_oot,
)
from credit.metrics import calibration_by_decile, psi_label
from credit.scorecard import Scaling, Scorecard
from credit.serialize import (
    SCORE_DECIMALS,
    Metrics,
    rnd,
    round_fairness,
    round_row,
    round_values,
)
from credit.strategy import UnitEconomics, cutoff_table
from credit.validation import (
    ChallengerEvaluation,
    ScorecardEvaluation,
    adverse_action_summary,
    evaluate_challenger,
    evaluate_scorecard,
    feature_iv_rows,
    loan_cents,
)

WOE_DECIMALS: Final[int] = 4


def main() -> Metrics:
    """Run the full pipeline, write ``results/`` and return the metrics dictionary."""
    econ = UnitEconomics()
    df = generate_applicants(N_APPLICANTS, seed=SEED)
    dev, oot = split_dev_oot(df)
    dev_tr, dev_te = _holdout_split(dev)

    # --- Candidate scorecards: with and without the geographic proxy -----------------
    full_cats = CATEGORICAL_FEATURES
    mitigated_cats = [c for c in CATEGORICAL_FEATURES if c not in PROXY_FEATURES]
    sc_full = Scorecard(NUMERIC_FEATURES, full_cats).fit(dev_tr, dev_tr[TARGET])
    sc_mit = Scorecard(NUMERIC_FEATURES, mitigated_cats).fit(dev_tr, dev_tr[TARGET])
    ev_full = evaluate_scorecard(sc_full, dev_tr, dev_te, oot, econ)
    ev_mit = evaluate_scorecard(sc_mit, dev_tr, dev_te, oot, econ)
    auc_cost = ev_full.oot["auc"] - ev_mit.oot["auc"]
    drop_proxy = auc_cost <= PROXY_AUC_TOLERANCE
    champion, ev = (sc_mit, ev_mit) if drop_proxy else (sc_full, ev_full)

    # --- Challenger on the same permitted feature set --------------------------------
    # Sign-dropping is a linear-model artefact, so the GBM keeps every numeric feature.
    ch_cat = mitigated_cats if drop_proxy else full_cats
    challenger = Challenger(NUMERIC_FEATURES, ch_cat, seed=CHALLENGER_SEED)
    challenger.fit(dev_tr, dev_tr[TARGET])
    ch_ev = evaluate_challenger(challenger, dev_tr, dev_te, oot, ev.oot_approval_rate)
    uplift = ch_ev.oot["auc"] - ev.oot["auc"]
    adopt_challenger = uplift >= CHALLENGER_MIN_UPLIFT and ch_ev.fairness["passes_four_fifths"]

    # --- Strategy: one pass over OOT loans for the table, the cutoff and approve-all ----
    *oot_table, at_cut, approve_all = cutoff_table(
        ev.scores_oot,
        ev.pd_oot,
        oot[TARGET],
        loan_cents(oot),
        [*TABLE_CUTOFFS, ev.cutoff, APPROVE_ALL_CUTOFF],
        econ,
    )
    breakeven_pd = econ.breakeven_pd
    breakeven_odds = float((1 - breakeven_pd) / breakeven_pd)

    metrics: Metrics = {
        "data": _data_section(df, dev, dev_tr, dev_te, oot),
        "scaling": _scaling_section(champion.scaling),
        "champion": _champion_section(champion, ev, drop_proxy, dev_tr, oot),
        "challenger": _challenger_section(ch_ev, uplift),
        "decision": {
            "challenger_min_auc_uplift": CHALLENGER_MIN_UPLIFT,
            "adopt_challenger": bool(adopt_challenger),
            "champion": "challenger GBM" if adopt_challenger else "WoE scorecard",
        },
        "fairness": {
            "protected_attribute_used_as_input": False,
            "reference_group": REFERENCE_GROUP,
            "protected_group": PROTECTED_GROUP,
            "with_proxy": _candidate_fairness(ev_full),
            "proxy_removed": _candidate_fairness(ev_mit),
            "mitigation_auc_cost": rnd(auc_cost),
            "proxy_auc_tolerance": PROXY_AUC_TOLERANCE,
            "mitigation_adopted": bool(drop_proxy),
        },
        "strategy": {
            "unit_economics": econ.as_dict(),
            "cutoff_selected_on": "development holdout, expected profit from model PD",
            "profit_cutoff": ev.cutoff,
            "breakeven_score": rnd(
                champion.scaling.score_from_odds(breakeven_odds), SCORE_DECIMALS
            ),
            "oot_at_cutoff": round_row(at_cut),
            "oot_approve_all": round_row(approve_all),
            "oot_cutoff_table": [round_row(r) for r in oot_table],
        },
        "adverse_action": adverse_action_summary(champion, oot, ev.scores_oot, ev.cutoff),
    }

    _write_results(metrics, champion, RESULTS_DIR)
    _print_summary(metrics)
    return metrics


def _holdout_split(dev: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Random in-time holdout of the development window (train, holdout)."""
    rng = np.random.default_rng(SEED)
    holdout = rng.random(len(dev)) < HOLDOUT_FRACTION
    return dev[~holdout].reset_index(drop=True), dev[holdout].reset_index(drop=True)


def _data_section(
    df: pd.DataFrame,
    dev: pd.DataFrame,
    dev_tr: pd.DataFrame,
    dev_te: pd.DataFrame,
    oot: pd.DataFrame,
) -> Metrics:
    return {
        "seed": SEED,
        "applicants": len(df),
        "development": len(dev),
        "development_train": len(dev_tr),
        "development_holdout": len(dev_te),
        "out_of_time": len(oot),
        "dev_months": f"0-{DEV_MONTHS - 1}",
        "oot_months": f"{DEV_MONTHS}-{int(df[MONTH].max())}",
        "bad_rate_dev": rnd(dev[TARGET].mean()),
        "bad_rate_oot": rnd(oot[TARGET].mean()),
        "protected_group_share_B": rnd((df[PROTECTED] == PROTECTED_GROUP).mean()),
    }


def _scaling_section(s: Scaling) -> Metrics:
    return {
        "base_score": s.base_score,
        "base_odds": s.base_odds,
        "pdo": s.pdo,
        "factor": rnd(s.factor),
        "offset": rnd(s.offset),
    }


def _champion_section(
    champion: Scorecard,
    ev: ScorecardEvaluation,
    drop_proxy: bool,
    dev_tr: pd.DataFrame,
    oot: pd.DataFrame,
) -> Metrics:
    calib = calibration_by_decile(oot[TARGET], ev.pd_oot)
    obs_rate, pred_rate = float(oot[TARGET].mean()), float(ev.pd_oot.mean())
    return {
        "type": "WoE logistic scorecard",
        "features": champion.features,
        "dropped_for_sign": champion.dropped_for_sign,
        "dropped_for_low_iv": champion.dropped_for_iv,
        "min_iv": champion.min_iv,
        "geographic_proxy_removed": drop_proxy,
        "intercept": rnd(champion.intercept_),
        "coefficients": round_values(champion.coef_),
        "all_coefficients_negative": champion.coefficients_all_negative(),
        "in_time": round_values(ev.in_time),
        "oot": round_values(ev.oot),
        "score_psi_dev_vs_oot": rnd(ev.score_psi_dev_vs_oot),
        "score_psi_label": psi_label(ev.score_psi_dev_vs_oot),
        "oot_observed_bad_rate": rnd(obs_rate),
        "oot_mean_predicted_pd": rnd(pred_rate),
        "oot_calibration_ratio_obs_to_pred": rnd(obs_rate / pred_rate),
        "calibration_by_decile": [
            {k: (rnd(v) if isinstance(v, float) else int(v)) for k, v in row.items()}
            for row in calib.to_dict("records")
        ],
        "feature_iv": feature_iv_rows(champion, dev_tr, oot),
    }


def _challenger_section(ch_ev: ChallengerEvaluation, uplift: float) -> Metrics:
    return {
        "type": "HistGradientBoosting with monotonic constraints",
        "features": ch_ev.features,
        "monotonic_constraints": ch_ev.monotonic_constraints,
        "in_time": round_values(ch_ev.in_time),
        "oot": round_values(ch_ev.oot),
        "pd_psi_dev_vs_oot": rnd(ch_ev.pd_psi_dev_vs_oot),
        "oot_auc_uplift_vs_champion": rnd(uplift),
        "fairness_at_matched_approval": round_fairness(ch_ev.fairness),
    }


def _candidate_fairness(ev: ScorecardEvaluation) -> Metrics:
    return {
        "features": ev.features,
        "oot_auc": rnd(ev.oot["auc"]),
        "cutoff": ev.cutoff,
        **round_fairness(ev.fairness),
    }


def _write_results(metrics: Metrics, champion: Scorecard, results: Path) -> None:
    """Write metrics, the points table and every rendered document under ``results``."""
    results.mkdir(parents=True, exist_ok=True)
    _write_json(results / "metrics.json", metrics)
    champion.points_table.assign(
        woe=lambda d: d["woe"].round(WOE_DECIMALS),
        points=lambda d: d["points"].round(SCORE_DECIMALS),
    ).to_csv(results / "scorecard_points.csv", index=False, lineterminator="\n")
    (results / "REPORT.md").write_text(report.render_report(metrics), encoding="utf-8")
    (results / "MODEL_CARD.md").write_text(model_card.render_model_card(metrics), encoding="utf-8")
    _write_json(results / "site.json", site.site_json(metrics))


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _print_summary(m: Metrics) -> None:
    c, ch, f, st = m["champion"], m["challenger"], m["fairness"], m["strategy"]
    print(
        f"champion OOT  AUC {c['oot']['auc']:.4f}  Gini {c['oot']['gini']:.4f}  "
        f"KS {c['oot']['ks']:.4f}  PSI {c['score_psi_dev_vs_oot']:.4f}"
    )
    print(
        f"challenger OOT AUC {ch['oot']['auc']:.4f}  "
        f"uplift {ch['oot_auc_uplift_vs_champion']:+.4f}  -> champion: {m['decision']['champion']}"
    )
    print(
        f"AIR with proxy {f['with_proxy']['adverse_impact_ratio']:.4f}  "
        f"without {f['proxy_removed']['adverse_impact_ratio']:.4f}  "
        f"AUC cost {f['mitigation_auc_cost']:.4f}"
    )
    print(
        f"profit cutoff {st['profit_cutoff']}  OOT approval "
        f"{st['oot_at_cutoff']['approval_rate']:.4f}  "
        f"bad rate {st['oot_at_cutoff']['bad_rate_approved']:.4f}"
    )
    print(f"wrote {RESULTS_DIR}")


if __name__ == "__main__":
    main()
