"""Shared fixtures: small deterministic samples and one fitted scorecard per session."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
import pandas as pd
import pytest

from credit.data import NUMERIC_FEATURES, TARGET, generate_applicants, split_dev_oot
from credit.scorecard import Scorecard


@dataclass(frozen=True)
class FittedScorecard:
    scorecard: Scorecard
    dev: pd.DataFrame
    oot: pd.DataFrame


@pytest.fixture(scope="session")
def fitted() -> FittedScorecard:
    """Scorecard on numeric features + purpose, fitted on a 12k-applicant development window."""
    df = generate_applicants(12_000, seed=11)
    dev, oot = split_dev_oot(df)
    sc = Scorecard(NUMERIC_FEATURES, ["purpose"]).fit(dev, dev[TARGET])
    return FittedScorecard(sc, dev, oot)


@pytest.fixture
def monotone_data() -> Callable[..., tuple[pd.Series, pd.Series]]:
    """Factory for (x, y) whose default risk is monotone in x in expectation.

    The sine term adds non-monotone fine structure that the binner has to merge away.
    """

    def make(n: int = 20_000, seed: int = 0, sign: int = 1) -> tuple[pd.Series, pd.Series]:
        rng = np.random.default_rng(seed)
        x = rng.normal(0, 1, n)
        logit = -2 + sign * 0.8 * x + 0.3 * np.sin(6 * x)
        y = (rng.random(n) < 1 / (1 + np.exp(-logit))).astype(int)
        return pd.Series(x), pd.Series(y)

    return make
