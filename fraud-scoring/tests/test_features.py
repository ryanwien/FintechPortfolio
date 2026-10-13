"""Point-in-time correctness: a feature may only depend on strictly earlier events."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from fraud.features import (
    DAY,
    F_INDEX,
    HOUR,
    MERCHANT_LOOKBACK,
    MERCHANT_WINDOW,
    SMALL_TICKET_CENTS,
    FeatureEngine,
    Txn,
    build_features,
    haversine_km,
)


def test_prefix_invariance_future_cannot_change_past(small_df, small_labels, small_features):
    """Features for the first k transactions are identical whether the future is absent
    or scrambled (amounts, devices, geo, fraud flags, chargeback arrivals)."""
    k = len(small_df) // 2
    t_k = int(small_df["ts"].iloc[k - 1])

    head = small_df.iloc[:k]
    X_head = build_features(head, small_labels[small_labels["label_ts"] <= t_k])
    np.testing.assert_array_equal(X_head, small_features[:k])

    future = small_df.copy()
    tail = future.index >= k
    future.loc[tail, "amount_cents"] = future.loc[tail, "amount_cents"] * 97
    future.loc[tail, "device_id"] = 424242
    future.loc[tail, "lat"] = future.loc[tail, "lat"] + 10
    future.loc[tail, "is_fraud"] = ~future.loc[tail, "is_fraud"]
    labels = small_labels.copy()
    late = (labels["label_ts"] > t_k).to_numpy()
    rng = np.random.default_rng(0)
    labels.loc[late, "merchant_id"] = rng.integers(0, 50, int(late.sum()))
    X_future = build_features(future, labels)
    np.testing.assert_array_equal(X_future[:k], small_features[:k])


def _brute_force(df: pd.DataFrame, labels: pd.DataFrame, i: int) -> dict[str, float]:
    """Recompute features for row i from scratch with pandas, using only the past."""
    row = df.iloc[i]
    ts = int(row["ts"])
    prior = df.iloc[:i]  # stream order == row order; strictly earlier rows only
    cust = prior[prior["customer_id"] == row["customer_id"]]
    out: dict[str, float] = {
        "cnt_1h": float((cust["ts"] > ts - HOUR).sum()),
        "cnt_24h": float((cust["ts"] > ts - DAY).sum()),
        "cnt_7d": float((cust["ts"] > ts - 7 * DAY).sum()),
        "small_cnt_1h": float(
            ((cust["ts"] > ts - HOUR) & (cust["amount_cents"] < SMALL_TICKET_CENTS)).sum()
        ),
        "amt_sum_24h_log": math.log1p(
            cust.loc[cust["ts"] > ts - DAY, "amount_cents"].sum() / 100.0
        ),
    }
    if len(cust):
        last = cust.iloc[-1]
        out["secs_since_last_log"] = math.log1p(min(ts - int(last["ts"]), 30 * DAY))
        out["km_from_prev"] = haversine_km(last["lat"], last["lon"], row["lat"], row["lon"])
    else:
        out["secs_since_last_log"] = math.log1p(30 * DAY)
        out["km_from_prev"] = 0.0
    seen = cust[
        (cust["merchant_id"] == row["merchant_id"]) & (cust["ts"] >= ts - MERCHANT_LOOKBACK)
    ]
    out["new_merchant"] = 0.0 if len(seen) else 1.0
    if row["card_present"] or row["device_id"] == 0:
        out["new_device"] = 0.0
        out["device_n_customers"] = 0.0
    else:
        cnp = cust[~cust["card_present"] & (cust["device_id"] != 0)]
        out["new_device"] = 0.0 if (cnp["device_id"] == row["device_id"]).any() else 1.0
        dev = prior[(prior["device_id"] == row["device_id"]) & ~prior["card_present"]]
        others = dev.loc[dev["customer_id"] != row["customer_id"], "customer_id"]
        out["device_n_customers"] = float(others.nunique())
    merch_txns = prior[
        (prior["merchant_id"] == row["merchant_id"]) & (prior["ts"] > ts - MERCHANT_WINDOW)
    ]
    lab = labels[
        (labels["merchant_id"] == row["merchant_id"])
        & (labels["label_ts"] <= ts)
        & (labels["label_ts"] > ts - MERCHANT_WINDOW)
    ]
    out["merchant_cb_rate_30d"] = len(lab) / (len(merch_txns) + 20.0)
    return out


def test_engine_matches_brute_force_point_in_time(small_df, small_labels, small_features):
    rng = np.random.default_rng(1)
    rows = rng.choice(len(small_df), size=200, replace=False)
    fraud_rows = np.flatnonzero(small_df["is_fraud"].to_numpy())[:100]  # bursts, label-heavy
    for i in np.concatenate([rows, fraud_rows]):
        expected = _brute_force(small_df, small_labels, int(i))
        for name, val in expected.items():
            got = small_features[i, F_INDEX[name]]
            assert math.isclose(got, val, rel_tol=1e-9, abs_tol=1e-9), (i, name, got, val)


def _txn(tid: int, ts: int, cust: int = 1, merchant: int = 5, amount: int = 2_000) -> Txn:
    return Txn(tid, ts, cust, merchant, 5411, amount, 40.7, -74.0, 0, True)


def test_current_transaction_is_excluded_from_its_own_features():
    eng = FeatureEngine()
    f = eng.process(_txn(0, 1_000))
    assert f[F_INDEX["cnt_1h"]] == 0
    assert f[F_INDEX["cnt_24h"]] == 0
    assert f[F_INDEX["new_merchant"]] == 1
    assert f[F_INDEX["amt_sum_24h_log"]] == 0
    f2 = eng.process(_txn(1, 1_060))
    assert f2[F_INDEX["cnt_1h"]] == 1
    assert f2[F_INDEX["new_merchant"]] == 0
    assert math.isclose(f2[F_INDEX["amt_sum_24h_log"]], math.log1p(20.0))


def test_window_boundaries_are_exclusive():
    eng = FeatureEngine()
    eng.process(_txn(0, 0))
    assert eng.process(_txn(1, HOUR))[F_INDEX["cnt_1h"]] == 0  # exactly 1h old: out
    eng2 = FeatureEngine()
    eng2.process(_txn(0, 0))
    assert eng2.process(_txn(1, HOUR - 1))[F_INDEX["cnt_1h"]] == 1


def test_chargeback_invisible_until_it_arrives():
    """A label that arrives at T only affects transactions scored at or after T."""
    j = F_INDEX["merchant_cb_rate_30d"]
    eng = FeatureEngine()
    eng.process(_txn(0, 0, cust=1))
    assert eng.process(_txn(1, 999, cust=2))[j] == 0.0
    eng.ingest_label(merchant_id=5, label_ts=1_000)
    assert eng.process(_txn(2, 1_000, cust=3))[j] > 0.0

    # build_features applies a label before a same-second transaction, never earlier.
    df = pd.DataFrame(
        [_txn(0, 0)._asdict(), _txn(1, 999, 2)._asdict(), _txn(2, 1_000, 3)._asdict()]
    )
    labels = pd.DataFrame({"label_ts": [1_000], "merchant_id": [5], "txn_id": [0]})
    X = build_features(df, labels)
    assert X[1, j] == 0.0
    assert X[2, j] > 0.0
