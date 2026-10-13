"""Serving-path replays: prove there is no train/serve skew and measure scoring latency.

* :func:`replay_in_process` drives the whole event stream through a fresh :class:`Scorer`
  (the production object). History before the test window only warms state; the first
  ``N_LATENCY`` test transactions are scored with the production explain setting and the next
  ``N_LATENCY_ALWAYS`` with reason codes forced on, and every request is timed.
* :func:`replay_http` drives the start of the shifted month through the FastAPI app with JSON
  payloads, from a scorer snapshot taken at the start of that month, interleaving chargeback
  events exactly as they arrive.

Skew values (max abs differences) are deterministic; latencies are wall-clock.
"""

from __future__ import annotations

import copy
import time
from dataclasses import dataclass, field
from typing import Any, Final

import numpy as np
import pandas as pd
from fastapi.testclient import TestClient

from fraud.api import create_app
from fraud.config import N_HTTP, N_LATENCY, N_LATENCY_ALWAYS
from fraud.costs import Bands
from fraud.features import FEATURES, FeatureEngine, iter_events
from fraud.model import FraudModel
from fraud.service import Scorer

NS_PER_MS: Final = 1e6


@dataclass(slots=True)
class InProcessReplay:
    """Outputs of the in-process replay."""

    scorer: Scorer  # warmed through the end of the stream
    engine_at_shift: FeatureEngine  # state snapshot just before the first shifted txn
    features: np.ndarray  # online features for every transaction
    scores: dict[int, float]  # txn_id -> online score, for timed requests
    latency_production_ns: list[int] = field(default_factory=list)
    latency_reasons_always_ns: list[int] = field(default_factory=list)


@dataclass(slots=True)
class HttpReplay:
    """Outputs of the HTTP replay."""

    latency_ns: list[int] = field(default_factory=list)
    max_abs_feature_diff: float = 0.0
    decisions: dict[str, int] = field(default_factory=dict)
    example_response: dict[str, Any] = field(default_factory=dict)  # highest-scoring response


def replay_in_process(
    df: pd.DataFrame,
    labels: pd.DataFrame,
    model: FraudModel,
    bands: Bands,
    test_start: int,
    shift_start: int,
) -> InProcessReplay:
    """Replay every event through a fresh scorer; time requests from ``test_start`` on."""
    scorer = Scorer(model, FeatureEngine(), bands)
    t_adverse_end = test_start + N_LATENCY
    t_always_end = t_adverse_end + N_LATENCY_ALWAYS
    online_feats = np.empty((len(df), len(FEATURES)), dtype=np.float64)
    online_scores: dict[int, float] = {}
    lat_adverse: list[int] = []
    lat_always: list[int] = []
    engine_at_shift: FeatureEngine | None = None
    for kind, ev in iter_events(df, labels):
        if kind == "label":
            scorer.ingest_label(*ev)
            continue
        tid = ev.txn_id
        if tid == shift_start:
            engine_at_shift = copy.deepcopy(scorer.engine)
        if test_start <= tid < t_always_end:
            production = tid < t_adverse_end
            s0 = time.perf_counter_ns()
            res = scorer.score(ev, explain="adverse" if production else "always")
            (lat_adverse if production else lat_always).append(time.perf_counter_ns() - s0)
            online_feats[tid] = res["features"]
            online_scores[tid] = res["score"]
        else:
            online_feats[tid] = scorer.features(ev)
    if engine_at_shift is None:
        raise RuntimeError(f"shift_start txn_id {shift_start} never appeared in the stream")
    return InProcessReplay(
        scorer=scorer,
        engine_at_shift=engine_at_shift,
        features=online_feats,
        scores=online_scores,
        latency_production_ns=lat_adverse,
        latency_reasons_always_ns=lat_always,
    )


def replay_http(
    df: pd.DataFrame,
    labels: pd.DataFrame,
    X: np.ndarray,
    scorer: Scorer,
    shifted: np.ndarray,
    shift_start: int,
) -> HttpReplay:
    """POST the first ``N_HTTP`` shifted-month transactions to ``/score?debug=true``.

    ``scorer`` must hold state as of ``shift_start``; ``X`` is the batch feature matrix the
    echoed features are compared against.
    """
    client = TestClient(create_app(scorer))
    head = df[shifted].iloc[:N_HTTP]
    snap_ts = int(df.loc[shift_start, "ts"])
    head_labels = labels[
        (labels["label_ts"] > snap_ts) & (labels["label_ts"] <= int(head["ts"].iloc[-1]))
    ]
    out = HttpReplay()
    for kind, ev in iter_events(head, head_labels):
        if kind == "label":
            client.post("/label", json={"merchant_id": ev[0], "label_ts": ev[1]})
            continue
        payload = ev._asdict()
        s0 = time.perf_counter_ns()
        r = client.post("/score", params={"debug": "true"}, json=payload)
        out.latency_ns.append(time.perf_counter_ns() - s0)
        body = r.json()
        out.decisions[body["decision"]] = out.decisions.get(body["decision"], 0) + 1
        got = np.array([body["features"][f] for f in FEATURES])
        diff = float(np.max(np.abs(got - X[payload["txn_id"]])))
        out.max_abs_feature_diff = max(out.max_abs_feature_diff, diff)
        if body["score"] > out.example_response.get("score", -1.0):
            out.example_response = {k: v for k, v in body.items() if k != "features"}
    return out


def latency_summary(latency_ns: list[int]) -> dict[str, Any]:
    """Count, p50/p95/p99 and mean in milliseconds."""
    lat_ms = np.array(latency_ns) / NS_PER_MS
    return {
        "n": len(lat_ms),
        "p50_ms": float(np.percentile(lat_ms, 50)),
        "p95_ms": float(np.percentile(lat_ms, 95)),
        "p99_ms": float(np.percentile(lat_ms, 99)),
        "mean_ms": float(lat_ms.mean()),
    }
