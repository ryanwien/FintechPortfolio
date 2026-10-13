"""In-process online scorer: feature engine state + model + decision bands.

Invariant: every request goes through :meth:`FeatureEngine.process`, the same call that built
the training matrix, under one lock, so concurrent requests see a consistent event order.
The pickled artifact holds exactly ``(model, engine, bands)``.
"""

from __future__ import annotations

import pickle
import threading
from pathlib import Path
from typing import Any, Literal

import numpy as np

from fraud.costs import Bands
from fraud.features import FeatureEngine, Txn
from fraud.model import FraudModel

ExplainMode = Literal["adverse", "always", "never"]


class Scorer:
    """Stateful scorer. One lock serialises state updates so concurrent requests are safe."""

    def __init__(self, model: FraudModel, engine: FeatureEngine, bands: Bands) -> None:
        self.model = model
        self.engine = engine
        self.bands = bands
        self._lock = threading.Lock()

    def features(self, txn: Txn) -> list[float]:
        """Point-in-time features for ``txn``; folds it into state (same call as training)."""
        with self._lock:
            return self.engine.process(txn)

    def score(self, txn: Txn, explain: ExplainMode = "adverse") -> dict[str, Any]:
        """Score one transaction.

        ``explain``: "adverse" (default) attaches reason codes only to review/decline
        decisions - the ones a customer or analyst needs explained - so the common approve
        path pays for a single tree walk; "always" / "never" force the choice.
        """
        feats = self.features(txn)
        x = np.asarray(feats, dtype=np.float64)
        score = float(self.model.predict_online(x[None, :])[0])
        decision = self.bands.decide(score)
        reasons: list[dict[str, Any]] = []
        if explain == "always" or (explain == "adverse" and decision != "approve"):
            reasons = self.model.reasons(x, score)
        return {
            "txn_id": txn.txn_id,
            "score": score,
            "decision": decision,
            "reasons": reasons,
            "model_version": self.model.version,
            "features": feats,
        }

    def ingest_label(self, merchant_id: int, label_ts: int) -> None:
        """Apply a chargeback arrival to merchant risk state."""
        with self._lock:
            self.engine.ingest_label(merchant_id, label_ts)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as fh:
            pickle.dump((self.model, self.engine, self.bands), fh)

    @classmethod
    def load(cls, path: Path) -> Scorer:
        """Load an artifact written by :meth:`save` (trusted input: our own pickle)."""
        with path.open("rb") as fh:
            model, engine, bands = pickle.load(fh)  # noqa: S301 - our own artifact
        return cls(model, engine, bands)
