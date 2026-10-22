"""Showcase-site feed (``results/site.json``) built from the metrics dict.

Schema per the repo conventions: slug, title, tagline, lane, stack, metrics, highlights,
limitations, series. Every number is formatted from ``metrics.json``; nothing is hand-written.
"""

from __future__ import annotations

from typing import Any


def site_json(m: dict[str, Any]) -> dict[str, Any]:
    """Build the site feed entry for this project."""
    # Every number on the page is read from the metrics dict, never typed in by hand.
    mr = m["models"]
    sv = m["decisioning"]["savings"]
    lat = m["latency"]["in_process_production"]
    sk = m["feature_engine"]["train_serve_skew"]
    hgb_t, rules_t = mr["hgb"]["test"], mr["rules"]["test"]
    rt = m["recall_by_fraud_type_0.5pct"]["hgb"]
    return {
        "slug": "fraud",
        "title": "Real-time card fraud scoring",
        "tagline": "Streaming point-in-time features, gradient-boosted scoring and "
        "cost-optimised approve/review/decline bands behind a FastAPI service.",
        "lane": "Risk & payments ML",
        "stack": ["Python", "scikit-learn", "FastAPI", "NumPy", "pandas"],
        "metrics": [
            {
                "label": "PR-AUC (held-out months)",
                "value": f"{hgb_t['pr_auc']:.3f}",
                "unit": "",
                "note": f"logistic {mr['logreg']['test']['pr_auc']:.3f}, rules "
                f"{rules_t['pr_auc']:.3f}",
            },
            {
                "label": "Recall @ 0.5% alert rate",
                "value": f"{hgb_t['recall_at_0.5pct_alerts']['recall'] * 100:.1f}",
                "unit": "%",
                "note": f"ceiling {m['recall_ceiling_0.5pct']['test'] * 100:.1f}%; rules "
                f"{rules_t['recall_at_0.5pct_alerts']['recall'] * 100:.1f}%",
            },
            {
                "label": "Expected loss saved vs rules",
                "value": f"{sv['hgb_vs_rules_cents_per_month'] / 100:,.0f}",
                "unit": "USD/month",
                "note": f"{sv['hgb_vs_rules_pct'] * 100:.0f}% lower on held-out months",
            },
            {
                "label": "p99 scoring latency",
                "value": f"{lat['p99_ms']:.2f}",
                "unit": "ms",
                "note": "in-process: features + model + reason codes on adverse decisions",
            },
            {
                "label": "Train/serve feature skew",
                "value": f"{sk['max_abs_feature_diff_in_process']:g}",
                "unit": "",
                "note": f"max abs diff over {sk['rows_compared_in_process']:,} replayed txns",
            },
        ],
        "highlights": [
            "One streaming feature engine for training and serving; tests prove online "
            "features equal batch features and that no future event leaks into a feature.",
            "Time-based split with realistic chargeback delay: "
            f"{m['data']['train_fraud_unlabelled_share'] * 100:.0f}% of training fraud was "
            "still unlabelled at the cut-off.",
            "Approve/review/decline bands chosen by minimising expected dollar loss under a "
            "review-capacity limit, then applied unchanged to later months.",
            "Drift monitor: population PSI flags seasonal spend drift; fraud-only PSI catches "
            f"an account-takeover tactic change that cut ATO recall from "
            f"{rt['test']['ato'] * 100:.0f}% to {rt['shifted']['ato'] * 100:.0f}%.",
            "FastAPI /score returns score, decision, reason codes and model version; trees "
            "are compiled to flat arrays for sub-millisecond online inference.",
        ],
        # Limitations sit beside the claims so the page does not overstate the results.
        "limitations": [
            "Synthetic data: fraud patterns are designed, so absolute scores flatter the model "
            "compared with a real portfolio.",
            "Feature state lives in one process's memory; production needs a shared "
            "low-latency feature store and idempotent, ordered event ingestion.",
            "Perturbation reason codes are a cheap approximation, not exact SHAP values.",
        ],
        "series": m["series"],
    }
