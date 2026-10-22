"""Model training: gradient-boosted trees vs a logistic-regression baseline.

Also holds the perturbation-based reason-code explainer used by the scoring service.
Invariant: the model version is a content hash of hyperparameters, feature list, training
set shape and a score checksum, so the same data and code always give the same version.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Final

import numpy as np
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

from fraud.catalog import MCC_CODES, MCCS
from fraud.config import HGB_PARAMS, LOGREG_C, LOGREG_MAX_ITER, REASON_TOP_K
from fraud.features import CATEGORICAL, F_INDEX, FEATURES
from fraud.forest import CompiledForest

VERSION_PREFIX: Final = "hgb-"
VERSION_HASH_CHARS: Final = 10
FINGERPRINT_ROWS: Final = 2_000  # rows scored for the version checksum
LOGIT_CLIP: Final = 1e-9

# feature -> (reason code, neutral label). Direction ("high"/"low") is added per request,
# because a perturbation can show that a *low* value is what raised the score.
REASON_TEXT: Final[dict[str, tuple[str, str]]] = {
    "amount_log": ("A01", "Transaction amount"),
    "amount_z": ("A02", "Amount vs customer's history (z-score)"),
    "amount_vs_ema": ("A03", "Amount vs customer's typical ticket"),
    "cnt_7d": ("V06", "Transactions in last 7 days"),
    "cnt_1h": ("V01", "Transactions in last hour"),
    "cnt_24h": ("V02", "Transactions in last 24h"),
    "small_cnt_1h": ("V03", "Sub-$5 charges in last hour (card testing)"),
    "amt_sum_24h_log": ("V04", "Spend in last 24h"),
    "secs_since_last_log": ("V05", "Time since previous transaction"),
    "km_from_prev": ("G01", "Distance from previous transaction"),
    "speed_kmh": ("G02", "Implied travel speed from previous transaction"),
    "km_from_centroid": ("G03", "Distance from customer's usual locations"),
    "new_merchant": ("M01", "Merchant not used in last 60 days"),
    "new_device": ("D01", "Device never seen for this customer"),
    "device_n_customers": ("D02", "Other customers seen on this device"),
    "card_present": ("C01", "Channel"),
    "hour": ("T01", "Hour of day"),
    "merchant_cb_rate_30d": ("M02", "Merchant's recent chargeback rate"),
    "mcc": ("M03", "Merchant category"),
}


def describe(feature: str, value: float, reference: float) -> str:
    """Human-readable reason text, direction-aware for numeric features."""
    label = REASON_TEXT[feature][1]
    if feature == "card_present":
        return f"{label}: {'card present' if value > 0 else 'card not present (online)'}"
    if feature == "mcc":
        code = int(MCC_CODES[int(value)]) if 0 <= value < len(MCC_CODES) else -1
        return f"{label}: {MCCS[code][0] if code in MCCS else 'unknown'} ({code})"
    # Binary flags read as 'this happened' or 'it did not', not as high or low.
    if feature in ("new_merchant", "new_device"):
        return label if value > 0 else f"{label}: no"
    return f"{label}: {'high' if value > reference else 'low'}"


def _logit(p: np.ndarray) -> np.ndarray:
    """Log-odds of ``p``, with ``p`` clipped away from 0 and 1 so the result stays finite."""
    p = np.clip(p, LOGIT_CLIP, 1 - LOGIT_CLIP)
    return np.log(p / (1 - p))


def train_hgb(X: np.ndarray, y: np.ndarray) -> HistGradientBoostingClassifier:
    """Fit the production model; ``mcc`` is a native categorical feature."""
    # Tell sklearn which columns are categorical so it splits on category sets, not code order.
    cat = np.array([f in CATEGORICAL for f in FEATURES])
    model = HistGradientBoostingClassifier(categorical_features=cat, **HGB_PARAMS)
    model.fit(X, y)
    return model


def train_logreg(X: np.ndarray, y: np.ndarray) -> Pipeline:
    """Fit the baseline: standardised numerics + one-hot MCC into logistic regression."""
    # Trees need no scaling but the linear model does: scale numerics, one-hot the category.
    num = [i for i, f in enumerate(FEATURES) if f not in CATEGORICAL]
    cat = [F_INDEX[f] for f in CATEGORICAL]
    pre = ColumnTransformer(
        [
            ("num", StandardScaler(), num),
            ("cat", OneHotEncoder(handle_unknown="ignore"), cat),
        ]
    )
    model = Pipeline(
        [("pre", pre), ("lr", LogisticRegression(C=LOGREG_C, max_iter=LOGREG_MAX_ITER))]
    )
    model.fit(X, y)
    return model


@dataclass(slots=True)
class FraudModel:
    """A fitted classifier plus everything the online scorer needs to explain it."""

    estimator: HistGradientBoostingClassifier
    reference: np.ndarray  # per-feature "typical good transaction" values
    version: str
    trained_through_ts: int
    forest: CompiledForest = field(init=False)

    def __post_init__(self) -> None:
        """Compile the fitted estimator into the flat-array forest used for online scoring."""
        self.forest = CompiledForest(self.estimator)

    @classmethod
    def fit(cls, X: np.ndarray, y: np.ndarray, trained_through_ts: int) -> FraudModel:
        """Train on (X, y) and derive the reason-code reference row and the version hash.

        The reference is the per-feature median of good transactions (the modal MCC for the
        categorical one).
        """
        # Reference row is the per-feature median of good traffic; reasons swap features toward it.
        est = train_hgb(X, y)
        good = X[y == 0]
        ref = np.median(good, axis=0)
        # A median of category codes means nothing, so the reference is the most common good one.
        mcc_j = F_INDEX["mcc"]
        vals, counts = np.unique(good[:, mcc_j], return_counts=True)
        ref[mcc_j] = vals[np.argmax(counts)]
        # Version hashes params, features, data shape and a score checksum: same inputs, same id.
        fingerprint = json.dumps(
            {
                "params": HGB_PARAMS,
                "features": FEATURES,
                "n": len(y),
                "pos": int(y.sum()),
                "through": int(trained_through_ts),
                # Checksum of a training-score prefix: catches a silently changed fitted model.
                "train_score_sum": round(
                    float(est.predict_proba(X[:FINGERPRINT_ROWS])[:, 1].sum()), 6
                ),
            },
            sort_keys=True,
        )
        digest = hashlib.sha256(fingerprint.encode()).hexdigest()
        version = VERSION_PREFIX + digest[:VERSION_HASH_CHARS]
        return cls(est, ref, version, trained_through_ts)

    def predict(self, X: np.ndarray) -> np.ndarray:
        """Batch P(fraud) via sklearn (multi-threaded, fast for large X)."""
        return self.estimator.predict_proba(X)[:, 1]

    def predict_online(self, X: np.ndarray) -> np.ndarray:
        """Small-batch P(fraud) via the flat-array forest: same numbers, ~10x less overhead."""
        return self.forest.predict_proba1(X)

    def reasons(
        self, x: np.ndarray, score: float, top_k: int = REASON_TOP_K
    ) -> list[dict[str, Any]]:
        """Reason codes by single-feature perturbation.

        Contribution of feature j = logit(score) - logit(score with x_j set to the value of a
        typical good transaction). All perturbed rows go through one batched tree walk.
        Only features that push the score *up* are returned.
        """
        # One perturbed row per feature, so every contribution comes from one batched walk.
        n_f = len(x)
        batch = np.repeat(x[None, :], n_f, axis=0)
        # Row j resets only feature j to its reference value; the other features keep actual values.
        batch[np.arange(n_f), np.arange(n_f)] = self.reference
        p = self.predict_online(batch)
        # Positive contrib: a typical value here would lower the score, so the real one raised it.
        contrib = _logit(np.array([score]))[0] - _logit(p)
        # Largest push first; the top reasons are the features that moved the score the most.
        order = np.argsort(-contrib, kind="stable")
        out: list[dict[str, Any]] = []
        for j in order[:top_k]:
            # Sorted descending: once one feature stops pushing up, the rest will not either.
            if contrib[j] <= 0:
                break
            name = FEATURES[j]
            out.append(
                {
                    "code": REASON_TEXT[name][0],
                    "feature": name,
                    "description": describe(name, float(x[j]), float(self.reference[j])),
                    "value": float(x[j]),
                    "contribution_logit": round(float(contrib[j]), 4),
                }
            )
        return out
