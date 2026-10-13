"""HTTP contract of the FastAPI scoring service."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from fraud.api import create_app
from fraud.costs import Bands
from fraud.features import iter_events

N_TAIL_REQUESTS = 600


def _payload(**overrides: Any) -> dict[str, Any]:
    base = {
        "txn_id": 1,
        "ts": 1_000,
        "customer_id": 7,
        "merchant_id": 3,
        "mcc": 5411,
        "amount_cents": 4_250,
        "lat": 40.71,
        "lon": -74.0,
        "device_id": 0,
        "card_present": True,
    }
    return base | overrides


def test_health_reports_model_version(client, fresh_scorer):
    assert client.get("/health").json()["model_version"] == fresh_scorer.model.version


def test_score_response_contract(client):
    r = client.post("/score", json=_payload())
    assert r.status_code == 200
    body = r.json()
    assert set(body) >= {"txn_id", "score", "decision", "reasons", "model_version", "latency_ms"}
    assert 0.0 <= body["score"] <= 1.0
    assert body["decision"] in {"approve", "review", "decline"}
    assert body["model_version"].startswith("hgb-")
    assert body["features"] is None  # only with ?debug=true


def test_reason_codes_on_adverse_decisions(small_df, small_labels, fresh_scorer):
    """Warm state in-process, then replay the last transactions through the API: every
    review/decline carries reason codes, approvals carry none."""
    fresh_scorer.bands = Bands(review_at=0.02, decline_at=0.3)
    split = len(small_df) - N_TAIL_REQUESTS
    split_ts = int(small_df["ts"].iloc[split])
    for kind, ev in iter_events(small_df.iloc[:split], small_labels):
        if kind == "label":
            fresh_scorer.ingest_label(*ev)
        else:
            fresh_scorer.features(ev)
    client = TestClient(create_app(fresh_scorer))
    seen = {"approve": 0, "review": 0, "decline": 0}
    tail_labels = small_labels[small_labels["label_ts"] >= split_ts]
    for kind, ev in iter_events(small_df.iloc[split:], tail_labels):
        if kind == "label":
            client.post("/label", json={"merchant_id": ev[0], "label_ts": ev[1]})
            continue
        body = client.post("/score", json=ev._asdict()).json()
        seen[body["decision"]] += 1
        if body["decision"] == "approve":
            assert body["reasons"] == []
        else:
            assert 1 <= len(body["reasons"]) <= 3
            contribs = [r["contribution_logit"] for r in body["reasons"]]
            assert contribs == sorted(contribs, reverse=True)
            assert contribs[0] > 0
            assert all(r["code"] and r["description"] for r in body["reasons"])
    assert seen["decline"] > 0
    assert seen["approve"] > 0


def test_explain_always_returns_reason_list(client):
    body = client.post("/score", params={"explain": "always"}, json=_payload()).json()
    assert isinstance(body["reasons"], list)


@pytest.mark.parametrize(
    ("params", "overrides"),
    [
        ({}, {"amount_cents": 0}),
        ({}, {"lat": 123.0}),
        ({}, {"lon": -181.0}),
        ({}, {"ts": -1}),
        ({"explain": "sometimes"}, {}),
    ],
    ids=["zero-amount", "bad-lat", "bad-lon", "negative-ts", "unknown-explain"],
)
def test_invalid_requests_are_rejected_with_422(client, params, overrides):
    assert client.post("/score", params=params, json=_payload(**overrides)).status_code == 422


def test_label_endpoint_updates_merchant_risk(client):
    q = {"debug": "true"}
    first = client.post("/score", params=q, json=_payload(txn_id=1, ts=1_000)).json()
    assert first["features"]["merchant_cb_rate_30d"] == 0.0
    assert client.post("/label", json={"merchant_id": 3, "label_ts": 1_500}).status_code == 200
    second = client.post("/score", params=q, json=_payload(txn_id=2, ts=2_000, customer_id=8))
    assert second.json()["features"]["merchant_cb_rate_30d"] > 0.0
