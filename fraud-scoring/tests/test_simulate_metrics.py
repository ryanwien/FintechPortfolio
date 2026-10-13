"""Simulator invariants, alert-rate metrics, reliability bins and the rules baseline."""

from __future__ import annotations

import numpy as np
import pandas as pd

from fraud.metrics import recall_at_alert_rate, reliability
from fraud.rules import rule_score
from fraud.simulate import label_events, simulate


def test_simulation_is_deterministic_and_well_formed(small_df, small_cfg):
    again = simulate(small_cfg)
    pd.testing.assert_frame_equal(small_df, again)
    assert small_df["amount_cents"].dtype == np.int64  # money is integer cents
    assert (small_df["amount_cents"] > 0).all()
    assert small_df["ts"].is_monotonic_increasing
    assert (small_df["txn_id"].to_numpy() == np.arange(len(small_df))).all()
    fraud = small_df[small_df["is_fraud"]]
    assert (fraud["label_ts"] > fraud["ts"]).all()  # labels always arrive later
    assert (small_df.loc[~small_df["is_fraud"], "label_ts"] == -1).all()
    assert set(fraud["fraud_type"]) >= {"card_testing", "ato", "high_value"}
    labels = label_events(small_df)
    assert labels["label_ts"].is_monotonic_increasing


def test_recall_at_alert_rate():
    scores = np.array([0.9, 0.8, 0.7, 0.1, 0.05, 0.0, 0.0, 0.0, 0.0, 0.0])
    y = np.array([1, 0, 1, 1, 0, 0, 0, 0, 0, 0])
    r = recall_at_alert_rate(scores, y, 0.2)
    assert r["n_alerts"] == 2
    assert r["recall"] == 1 / 3
    assert r["precision"] == 0.5


def test_reliability_bins_cover_all_rows():
    rng = np.random.default_rng(0)
    p = rng.random(1_000) ** 4
    y = (rng.random(1_000) < p).astype(int)
    bins = reliability(p, y)
    assert sum(b["n"] for b in bins) == 1_000


def test_rules_score_fraud_higher_on_average(small_features, small_df):
    s = rule_score(small_features)
    fraud = small_df["is_fraud"].to_numpy()
    assert s[fraud].mean() > s[~fraud].mean()
