"""End-to-end run: simulate -> features -> train -> evaluate -> decide -> drift -> serve.

``python -m fraud.run`` writes results/metrics.json, results/REPORT.md and results/site.json,
plus artifacts/scorer.pkl (git-ignored) for the API.

Every value in the outputs is deterministic for a given seed except wall-clock timings
(``run``, stage seconds, throughput, latency percentiles and the example's ``latency_ms``).
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

import numpy as np

from fraud.config import (
    DAY,
    HGB_PARAMS,
    RESULTS_DIR,
    SCORER_ARTIFACT,
    CostConfig,
    SimConfig,
)
from fraud.costs import Bands
from fraud.evaluation import (
    SplitMasks,
    TimeSplit,
    decide_per_model,
    drift_report,
    model_reports,
    recall_by_fraud_type,
    recall_ceiling,
    savings,
    validation_decline_curve,
)
from fraud.features import FEATURES, build_features
from fraud.model import FraudModel, train_logreg
from fraud.replay import latency_summary, replay_http, replay_in_process
from fraud.report import render_report
from fraud.rules import RULES, rule_score
from fraud.service import Scorer
from fraud.simulate import label_events, simulate
from fraud.site_feed import site_json


class StageTimer:
    """Collects wall-clock seconds per named pipeline stage, in execution order."""

    def __init__(self) -> None:
        """Start with no stages recorded."""
        self.seconds: dict[str, float] = {}

    @contextmanager
    def __call__(self, name: str) -> Iterator[None]:
        """Time the enclosed block and store its wall-clock seconds under ``name``."""
        # perf_counter is monotonic, so the measured duration is immune to clock changes.
        t0 = time.perf_counter()
        try:
            yield
        finally:
            # Recorded in finally, so a stage that raises still leaves its timing behind.
            self.seconds[name] = time.perf_counter() - t0


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write ``payload`` to ``path`` as indented UTF-8 JSON with a trailing newline."""
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def main() -> dict[str, Any]:
    """Run the whole pipeline, write ``results/`` and the scorer artifact; return metrics."""
    t_start = time.perf_counter()
    stage = StageTimer()
    cfg = SimConfig()
    cost_cfg = CostConfig()
    split = TimeSplit.from_config(cfg)

    # ---------------------------------------------------------------- data
    with stage("simulate"):
        df = simulate(cfg)
        labels = label_events(df)
    # Pull columns into plain numpy once; every metric below works on these arrays and masks.
    period = df["period"].to_numpy()
    y = df["is_fraud"].to_numpy().astype(np.int64)
    amount = df["amount_cents"].to_numpy().astype(np.int64)
    ftype = df["fraud_type"].to_numpy()
    masks = SplitMasks.from_periods(period, split)
    print(f"simulated {len(df):,} txns, fraud rate {y.mean():.4f}")

    # ---------------------------------------------------------------- features (batch replay)
    with stage("features_batch"):
        # Replays the event stream in serving order, so these are point-in-time features.
        X = build_features(df, labels)
    feat_secs = stage.seconds["features_batch"]
    print(f"features {X.shape} in {feat_secs:.1f}s")

    # Label delay: train only on labels that had arrived by the training cut-off.
    cutoff = split.valid[0] * cfg.period_seconds
    label_ts = df["label_ts"].to_numpy()
    y_known = ((y == 1) & (label_ts <= cutoff)).astype(np.int64)
    train_fraud = int(y[masks.train].sum())
    train_known = int(y_known[masks.train].sum())
    # Chargeback delay in days, fraud rows only; feeds the label-delay stats in the report.
    delays = (label_ts - df["ts"].to_numpy())[y == 1] / DAY

    # ---------------------------------------------------------------- models
    with stage("train_hgb"):
        hgb = FraudModel.fit(X[masks.train], y_known[masks.train], trained_through_ts=cutoff)
    with stage("train_logreg"):
        lr = train_logreg(X[masks.train], y_known[masks.train])
    print(f"trained; model version {hgb.version}")

    # Score every row once; the split masks pick out each evaluation window afterwards.
    scores = {
        "hgb": hgb.predict(X),
        "logreg": lr.predict_proba(X)[:, 1],
        "rules": rule_score(X),
    }
    reports = model_reports(scores, y, masks)
    by_type = recall_by_fraud_type(scores, ftype, masks, reports)

    # ---------------------------------------------------------------- decisioning
    # Bands are chosen on validation inside decide_per_model, then frozen for later months.
    decisions = decide_per_model(scores, y, amount, masks, cost_cfg)
    loss_savings = savings(decisions, y, amount, masks, n_test_months=len(split.test))
    series = validation_decline_curve(scores["hgb"], y, amount, masks, cost_cfg)

    # ---------------------------------------------------------------- drift
    drift = drift_report(X, scores["hgb"], y, masks)

    # ---------------------------------------------------------------- online replay
    # Transaction ids where the timed test window and the shifted month begin.
    test_start = int(df.loc[masks.test, "txn_id"].min())
    shift_start = int(df.loc[masks.shifted, "txn_id"].min())
    with stage("online_replay"):
        online = replay_in_process(
            df, labels, hgb, Bands(**decisions["hgb"]["bands"]), test_start, shift_start
        )
    # Train/serve skew check: online features and scores must match batch values on every row.
    feat_diff = float(np.max(np.abs(online.features - X)))
    idx = np.array(sorted(online.scores))
    online_s = np.array([online.scores[i] for i in idx])
    score_diff = float(np.max(np.abs(online_s - scores["hgb"][idx])))
    # Save the scorer with its warmed state, so the API starts with history up to stream end.
    online.scorer.save(SCORER_ARTIFACT)

    with stage("http_replay"):
        # Second path: the same serving logic through the FastAPI app, with JSON in the loop.
        http = replay_http(
            df,
            labels,
            X,
            Scorer(hgb, online.engine_at_shift, online.scorer.bands),
            masks.shifted,
            shift_start,
        )

    # ---------------------------------------------------------------- results
    runtime = time.perf_counter() - t_start
    metrics = {
        "run": {"runtime_seconds": runtime, "stage_seconds": stage.seconds, "seed": cfg.seed},
        "config": {
            "simulation": asdict(cfg),
            "costs": cost_cfg.as_dict(),
            "hgb_params": HGB_PARAMS,
            "rules": [{"name": n, "points": p, "condition": c} for n, p, c in RULES],
        },
        "data": {
            "n_txns": len(df),
            "n_customers": cfg.n_customers,
            "fraud_rate": float(y.mean()),
            "fraud_txns": int(y.sum()),
            "fraud_by_type": {
                str(k): int(v) for k, v in df.loc[y == 1, "fraud_type"].value_counts().items()
            },
            "fraud_rate_by_period": [float(y[period == p].mean()) for p in range(cfg.n_periods)],
            "split": {
                "warmup_periods": list(split.warmup),
                "train_periods": list(split.train),
                "valid_periods": list(split.valid),
                "test_periods": list(split.test),
                "shifted_period": split.shifted,
                "period_days": cfg.period_days,
                "n_train": int(masks.train.sum()),
                "n_valid": int(masks.valid.sum()),
                "n_test": int(masks.test.sum()),
                "n_shifted": int(masks.shifted.sum()),
            },
            "label_delay_days": {
                "median": float(np.median(delays)),
                "p90": float(np.percentile(delays, 90)),
            },
            "train_fraud_total": train_fraud,
            "train_fraud_labelled_at_cutoff": train_known,
            "train_fraud_unlabelled_share": 1 - train_known / train_fraud,
        },
        "feature_engine": {
            "n_features": len(FEATURES),
            "features": FEATURES,
            "batch_seconds": feat_secs,
            "throughput_txn_per_sec": len(df) / feat_secs,
            "train_serve_skew": {
                "rows_compared_in_process": len(df),
                "max_abs_feature_diff_in_process": feat_diff,
                "rows_scored_in_process": len(idx),
                "max_abs_score_diff_in_process": score_diff,
                "rows_compared_http": len(http.latency_ns),
                "max_abs_feature_diff_http": http.max_abs_feature_diff,
            },
        },
        "models": reports,
        "recall_by_fraud_type_0.5pct": by_type,
        "recall_ceiling_0.5pct": recall_ceiling(reports),
        "decisioning": {"per_model": decisions, "savings": loss_savings},
        "drift": drift,
        "latency": {
            "in_process_production": latency_summary(online.latency_production_ns),
            "in_process_reasons_always": latency_summary(online.latency_reasons_always_ns),
            "http_testclient": latency_summary(http.latency_ns),
            "http_decisions": http.decisions,
        },
        "model_version": hgb.version,
        "example_response": http.example_response,
        "series": series,
    }

    # All three outputs are written from the same metrics dict, so they cannot disagree.
    RESULTS_DIR.mkdir(exist_ok=True)
    _write_json(RESULTS_DIR / "metrics.json", metrics)
    (RESULTS_DIR / "REPORT.md").write_text(render_report(metrics), encoding="utf-8")
    _write_json(RESULTS_DIR / "site.json", site_json(metrics))
    print(f"done in {runtime:.1f}s -> {RESULTS_DIR}")
    return metrics


if __name__ == "__main__":
    main()
