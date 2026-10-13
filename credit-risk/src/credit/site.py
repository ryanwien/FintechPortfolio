"""Render ``site.json``, this project's entry in the portfolio dashboard feed.

The schema (slug, title, tagline, lane, stack, metrics, highlights, limitations, series) is
shared by every project in the repo; values are read from ``metrics``.
"""

from __future__ import annotations

from credit.markdown import adopted_fairness
from credit.serialize import METRIC_DECIMALS, Metrics


def site_json(m: Metrics) -> Metrics:
    """Dashboard card: headline metrics, highlights, limitations and a cutoff trade-off series."""
    c, ch, f, st, dec = m["champion"], m["challenger"], m["fairness"], m["strategy"], m["decision"]
    sc = m["scaling"]
    fair = adopted_fairness(m)
    at = st["oot_at_cutoff"]
    table = st["oot_cutoff_table"]
    return {
        "slug": "credit",
        "title": "Credit underwriting scorecard with model-risk documentation",
        "tagline": (
            "WoE logistic scorecard vs monotonic GBM challenger, validated out-of-time, with "
            "fair-lending tests, Reg B reason codes and a generated SR 11-7 model card."
        ),
        "lane": "Lending & model risk",
        "stack": ["Python", "scikit-learn", "pandas", "NumPy"],
        "metrics": [
            {
                "label": "OOT AUC (champion)",
                "value": f"{c['oot']['auc']:.3f}",
                "unit": "",
                "note": f"Gini {c['oot']['gini']:.3f}, KS {c['oot']['ks']:.3f}",
            },
            {
                "label": "Score PSI dev vs OOT",
                "value": f"{c['score_psi_dev_vs_oot']:.3f}",
                "unit": "",
                "note": c["score_psi_label"],
            },
            {
                "label": "Adverse impact ratio",
                "value": f"{fair['adverse_impact_ratio']:.3f}",
                "unit": "",
                "note": "four-fifths rule "
                + ("passed" if fair["passes_four_fifths"] else "failed")
                + f"; proxy removal cost {f['mitigation_auc_cost']:.4f} AUC",
            },
            {
                "label": "Approval at profit cutoff",
                "value": f"{100 * at['approval_rate']:.1f}",
                "unit": "%",
                "note": f"bad rate {100 * at['bad_rate_approved']:.2f}% at cutoff "
                f"{st['profit_cutoff']}",
            },
            {
                "label": "Challenger AUC uplift",
                "value": f"{ch['oot_auc_uplift_vs_champion']:+.4f}",
                "unit": "",
                "note": f"champion kept: {dec['champion']}",
            },
        ],
        "highlights": [
            f"Monotonic WoE binning and {sc['base_score']:.0f} @ {sc['base_odds']:.0f}:1, "
            f"PDO {sc['pdo']:.0f} points scaling, unit-tested",
            "Out-of-time validation: AUC, Gini, KS, calibration by decile, PSI and per-feature CSI",
            "Four-fifths rule and equal-opportunity gap, with a proxy-removal mitigation and "
            "its AUC cost",
            "Top-4 ECOA / Reg B adverse action reasons from points lost per characteristic",
            "Profit-based cutoff in integer cents and a generated SR 11-7 style model card",
        ],
        "limitations": [
            "Synthetic applicants with a logit-additive default process favour the scorecard",
            "No reject inference; PD under-predicts in the stressed OOT window",
            "Single binary protected attribute; flat unit economics",
        ],
        "series": {
            "name": "OOT approval rate vs bad rate of approved loans by cutoff",
            "x": [round(r["approval_rate"], METRIC_DECIMALS) for r in table],
            "y": [round(r["bad_rate_approved"], METRIC_DECIMALS) for r in table],
        },
    }
