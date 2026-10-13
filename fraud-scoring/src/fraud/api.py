"""FastAPI scoring service.

``python -m fraud.api`` serves the scorer saved by ``python -m fraud.run``
(``artifacts/scorer.pkl``, warmed with history up to the end of the simulated stream).

Endpoints: ``POST /score`` (decision, reason codes, model version), ``POST /label``
(chargeback arrival), ``GET /health``. Pydantic validates every request at the boundary.
"""

from __future__ import annotations

import time
from typing import Final

from fastapi import FastAPI
from pydantic import BaseModel, Field

from fraud.config import SCORER_ARTIFACT
from fraud.costs import Decision
from fraud.features import FEATURES, Txn
from fraud.service import ExplainMode, Scorer

HOST: Final = "127.0.0.1"
PORT: Final = 8000


class TxnIn(BaseModel):
    """Authorization request as received on the wire."""

    txn_id: int
    ts: int = Field(ge=0, description="seconds since epoch")
    customer_id: int
    merchant_id: int
    mcc: int
    amount_cents: int = Field(gt=0)
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    device_id: int = 0
    card_present: bool


class Reason(BaseModel):
    code: str
    feature: str
    description: str
    value: float
    contribution_logit: float


class ScoreOut(BaseModel):
    txn_id: int
    score: float
    decision: Decision
    reasons: list[Reason]
    model_version: str
    latency_ms: float
    features: dict[str, float] | None = None


class LabelIn(BaseModel):
    merchant_id: int
    label_ts: int


def create_app(scorer: Scorer) -> FastAPI:
    """Build the app around an existing (possibly pre-warmed) scorer."""
    app = FastAPI(title="fraud-scoring", version=scorer.model.version)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "model_version": scorer.model.version}

    @app.post("/score", response_model=ScoreOut)
    def score(txn: TxnIn, explain: ExplainMode = "adverse", debug: bool = False) -> ScoreOut:
        t0 = time.perf_counter()
        res = scorer.score(Txn(**txn.model_dump()), explain=explain)
        return ScoreOut(
            txn_id=res["txn_id"],
            score=res["score"],
            decision=res["decision"],
            reasons=[Reason(**r) for r in res["reasons"]],
            model_version=res["model_version"],
            latency_ms=(time.perf_counter() - t0) * 1000.0,
            features=dict(zip(FEATURES, res["features"], strict=True)) if debug else None,
        )

    @app.post("/label")
    def label(ev: LabelIn) -> dict[str, str]:
        scorer.ingest_label(ev.merchant_id, ev.label_ts)
        return {"status": "ingested"}

    return app


def main() -> None:  # pragma: no cover - manual entry point
    import uvicorn

    if not SCORER_ARTIFACT.exists():
        raise SystemExit("No scorer artifact. Run `python -m fraud.run` first.")
    uvicorn.run(create_app(Scorer.load(SCORER_ARTIFACT)), host=HOST, port=PORT)


if __name__ == "__main__":  # pragma: no cover
    main()
