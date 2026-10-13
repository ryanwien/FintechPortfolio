"""No train/serve skew: the serving path reproduces training features exactly."""

from __future__ import annotations

import numpy as np
from fastapi.testclient import TestClient

from fraud.api import create_app
from fraud.features import FEATURES, iter_events


def test_online_scorer_equals_batch_features_and_scores(
    small_df, small_labels, small_features, small_model, fresh_scorer
):
    online = np.empty_like(small_features)
    scored, scores = [], []
    for kind, ev in iter_events(small_df, small_labels):
        if kind == "label":
            fresh_scorer.ingest_label(*ev)
        elif ev.txn_id % 5 == 0:  # full scoring path on a fifth of requests (speed)
            res = fresh_scorer.score(ev, explain="never")
            online[ev.txn_id] = res["features"]
            scored.append(ev.txn_id)
            scores.append(res["score"])
        else:
            online[ev.txn_id] = fresh_scorer.features(ev)
    np.testing.assert_array_equal(online, small_features)
    # Online uses the flat-array tree walk, batch uses sklearn: same numbers.
    batch = small_model.predict(small_features[scored])
    np.testing.assert_allclose(np.array(scores), batch, rtol=0, atol=1e-12)


def test_http_json_path_equals_batch_features(small_df, small_labels, small_features, fresh_scorer):
    """Warm the scorer in-process, then send the last 300 transactions as JSON over HTTP,
    interleaving chargeback events as they arrive."""
    split = len(small_df) - 300
    split_ts = int(small_df["ts"].iloc[split])
    head, tail = small_df.iloc[:split], small_df.iloc[split:]
    for kind, ev in iter_events(head, small_labels[small_labels["label_ts"] < split_ts]):
        if kind == "label":
            fresh_scorer.ingest_label(*ev)
        else:
            fresh_scorer.features(ev)
    client = TestClient(create_app(fresh_scorer))
    n = 0
    for kind, ev in iter_events(tail, small_labels[small_labels["label_ts"] >= split_ts]):
        if kind == "label":
            r = client.post("/label", json={"merchant_id": ev[0], "label_ts": ev[1]})
            assert r.status_code == 200
            continue
        body = client.post("/score", params={"debug": "true"}, json=ev._asdict()).json()
        got = np.array([body["features"][f] for f in FEATURES])
        np.testing.assert_array_equal(got, small_features[ev.txn_id])
        n += 1
    assert n == 300
