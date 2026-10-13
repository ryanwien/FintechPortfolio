"""The flat-array forest must reproduce sklearn exactly; it relies on private attributes."""

from __future__ import annotations

import numpy as np
import sklearn
from sklearn.ensemble import HistGradientBoostingClassifier

from fraud.forest import SKLEARN_VERSION, CompiledForest


def test_installed_sklearn_matches_the_pinned_version():
    """CompiledForest reads private estimator attributes; any upgrade must be deliberate."""
    assert sklearn.__version__ == SKLEARN_VERSION


def test_compiled_forest_matches_sklearn_including_edge_cases():
    rng = np.random.default_rng(0)
    n = 6_000
    X = rng.normal(size=(n, 5))
    X[:, 4] = rng.integers(0, 13, n)
    X[rng.random(n) < 0.02, 1] = np.nan
    y = (X[:, 0] + 1.5 * (X[:, 4] % 3 == 0) + rng.normal(size=n) > 2.0).astype(int)
    est = HistGradientBoostingClassifier(
        categorical_features=[False] * 4 + [True], max_iter=60, max_depth=6, random_state=0
    ).fit(X, y)
    cf = CompiledForest(est)
    X_test = rng.normal(size=(3_000, 5))
    X_test[:, 4] = rng.integers(-1, 16, 3_000)  # includes unseen and negative categories
    X_test[rng.random(3_000) < 0.05, 4] = np.nan
    X_test[rng.random(3_000) < 0.05, 2] = np.nan
    expected = est.predict_proba(X_test)[:, 1]
    np.testing.assert_allclose(cf.predict_proba1(X_test), expected, rtol=0, atol=1e-12)
    np.testing.assert_allclose(cf.predict_proba1(X_test[:1]), expected[:1], rtol=0, atol=1e-12)
    np.testing.assert_allclose(cf.raw(X_test), est.decision_function(X_test), rtol=0, atol=1e-12)


def test_compiled_forest_chunks_large_batches_consistently():
    rng = np.random.default_rng(1)
    X = rng.normal(size=(2_000, 3))
    y = (X[:, 0] > 0.5).astype(int)
    cf = CompiledForest(HistGradientBoostingClassifier(max_iter=20, random_state=0).fit(X, y))
    big = rng.normal(size=(9_000, 3))  # > one chunk
    whole = cf.predict_proba1(big)
    parts = np.concatenate([cf.predict_proba1(big[i : i + 1_000]) for i in range(0, 9_000, 1_000)])
    np.testing.assert_array_equal(whole, parts)
