"""Gradient-boosted challenger with economically motivated monotonic constraints.

Categorical levels are fixed to the generator's vocabulary so encoding never depends on
which levels happen to appear in a given frame. The protected attribute is rejected as input.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier

from credit.config import CHALLENGER_SEED
from credit.data import PURPOSES, REGIONS
from credit.scorecard import assert_no_protected

# +1: PD must not decrease as the feature rises; -1: must not increase; 0: unconstrained.
MONOTONE: Final[dict[str, int]] = {
    "annual_income": -1,
    "dti": 1,
    "revolving_utilization": 1,
    "delinq_2y": 1,
    "inquiries_6m": 1,
    "credit_history_months": -1,
    "employment_years": -1,
    "loan_amount": 0,  # risk depends on amount relative to income, so left free
    "term_months": 1,
}
_CATEGORIES: Final[dict[str, list[str]]] = {"purpose": PURPOSES, "region": REGIONS}

# Conservative, fixed hyper-parameters (no early stopping, so the fit is deterministic).
_GBM_PARAMS: Final[dict[str, object]] = {
    "learning_rate": 0.05,
    "max_iter": 300,
    "max_leaf_nodes": 15,
    "min_samples_leaf": 200,
    "l2_regularization": 1.0,
    "early_stopping": False,
}


def monotonic_constraints(numeric: Sequence[str]) -> dict[str, int]:
    """Constraint per numeric feature (0 when the feature has no declared direction)."""
    return {f: MONOTONE.get(f, 0) for f in numeric}


class Challenger:
    """Monotone-constrained histogram GBM on raw (un-binned) features."""

    def __init__(
        self, numeric: Sequence[str], categorical: Sequence[str], seed: int = CHALLENGER_SEED
    ) -> None:
        assert_no_protected([*numeric, *categorical])
        self.numeric = list(numeric)
        self.categorical = list(categorical)
        cst = monotonic_constraints(self.numeric)
        cst.update(dict.fromkeys(self.categorical, 0))
        self.model = HistGradientBoostingClassifier(
            **_GBM_PARAMS,
            categorical_features="from_dtype",
            monotonic_cst=cst,
            random_state=seed,
        )

    @property
    def features(self) -> list[str]:
        return self.numeric + self.categorical

    def _frame(self, df: pd.DataFrame) -> pd.DataFrame:
        X = df[self.numeric].astype(float).copy()
        for f in self.categorical:
            X[f] = pd.Categorical(df[f].astype(str), categories=_CATEGORIES[f])
        return X[self.features]

    def fit(self, df: pd.DataFrame, y: pd.Series) -> Challenger:
        """Fit on ``df[self.features]`` against the bad flag ``y``."""
        self.model.fit(self._frame(df), np.asarray(y, dtype=int))
        return self

    def predict_pd(self, df: pd.DataFrame) -> np.ndarray:
        """Predicted probability of default per row."""
        return self.model.predict_proba(self._frame(df))[:, 1]
