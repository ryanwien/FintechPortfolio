"""Shared fixtures: one small simulated world, its features and a model trained on it.

Session-scoped so the (relatively slow) simulate -> features -> fit chain runs once.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from fraud.api import create_app
from fraud.config import SimConfig
from fraud.costs import Bands
from fraud.features import FeatureEngine, build_features
from fraud.model import FraudModel
from fraud.service import Scorer
from fraud.simulate import label_events, simulate

SMALL = SimConfig(
    seed=11,
    n_customers=150,
    n_merchants=200,
    n_periods=4,
    warmup_periods=1,
    shift_period=3,
    card_testing_per_day=0.3,
    ato_per_day=0.4,
    high_value_per_day=0.4,
    compromised_merchants_per_period=1,
)
SMALL_TRAIN_LAST_PERIOD = 2


@pytest.fixture(scope="session")
def small_cfg() -> SimConfig:
    return SMALL


@pytest.fixture(scope="session")
def small_df() -> pd.DataFrame:
    return simulate(SMALL)


@pytest.fixture(scope="session")
def small_labels(small_df: pd.DataFrame) -> pd.DataFrame:
    return label_events(small_df)


@pytest.fixture(scope="session")
def small_features(small_df: pd.DataFrame, small_labels: pd.DataFrame) -> np.ndarray:
    return build_features(small_df, small_labels)


@pytest.fixture(scope="session")
def small_model(small_df: pd.DataFrame, small_features: np.ndarray) -> FraudModel:
    train = small_df["period"].to_numpy() <= SMALL_TRAIN_LAST_PERIOD
    cutoff = (SMALL_TRAIN_LAST_PERIOD + 1) * SMALL.period_seconds
    y = (small_df["is_fraud"] & (small_df["label_ts"] <= cutoff)).to_numpy().astype(int)
    return FraudModel.fit(small_features[train], y[train], trained_through_ts=cutoff)


@pytest.fixture
def fresh_scorer(small_model: FraudModel) -> Scorer:
    """A scorer with empty feature state (no history)."""
    return Scorer(small_model, FeatureEngine(), Bands(review_at=0.05, decline_at=0.5))


@pytest.fixture
def client(fresh_scorer: Scorer) -> TestClient:
    """HTTP client bound to ``fresh_scorer`` (mutating the scorer affects the app)."""
    return TestClient(create_app(fresh_scorer))
