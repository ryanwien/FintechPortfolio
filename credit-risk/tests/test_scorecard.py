import math

import numpy as np
import pytest

from credit.challenger import Challenger
from credit.config import BASE_ODDS, BASE_SCORE, PDO
from credit.data import (
    ALL_FEATURES,
    CATEGORICAL_FEATURES,
    NUMERIC_FEATURES,
    PROTECTED,
    TARGET,
    generate_applicants,
)
from credit.scorecard import ProtectedAttributeError, Scaling, Scorecard


@pytest.mark.parametrize(
    ("odds", "score"),
    [(30, 600), (60, 620), (15, 580)],
    ids=["base_odds", "double_odds_adds_pdo", "half_odds_subtracts_pdo"],
)
def test_scaling_anchor_points(odds, score):
    assert Scaling(600, 30, 20).score_from_odds(odds) == pytest.approx(score)


def test_scaling_factor_offset_and_pd():
    s = Scaling(600, 30, 20)
    assert s.factor == pytest.approx(20 / math.log(2))
    assert s.offset == pytest.approx(600 - 20 / math.log(2) * math.log(30))
    assert s.pd_from_score(600) == pytest.approx(1 / 31)


def test_scaling_defaults_come_from_config():
    s = Scaling()
    assert s.base_score == BASE_SCORE
    assert s.base_odds == BASE_ODDS
    assert s.pdo == PDO


def test_scaling_round_trip():
    s = Scaling()
    scores = np.array([450.0, 555.5, 700.0])
    assert np.allclose(s.score_from_odds(s.odds_from_score(scores)), scores)


def test_points_sum_to_model_log_odds(fitted):
    sc, dev = fitted.scorecard, fitted.dev
    X = sc.binner.transform(dev)[sc.features].to_numpy()
    pd_lr = sc.model.predict_proba(X)[:, 1]
    expected = sc.scaling.score_from_odds((1 - pd_lr) / pd_lr)
    assert np.allclose(sc.score(dev), expected, atol=1e-6)
    assert np.allclose(sc.predict_pd(dev), pd_lr, atol=1e-9)


def test_coefficients_negative_after_sign_screen(fitted):
    sc = fitted.scorecard
    assert sc.coefficients_all_negative()
    for f in sc.dropped_for_sign:
        assert f not in sc.features


def test_max_points_are_attainable(fitted):
    sc = fitted.scorecard
    for f in sc.features:
        assert sc.max_points[f] == pytest.approx(sc.points[f].max())


def test_points_table_has_one_row_per_bin(fitted):
    sc = fitted.scorecard
    n_bins = sum(len(sc.binner.binnings[f].labels) for f in sc.features)
    assert len(sc.points_table) == n_bins


def test_protected_attribute_never_a_model_input(fitted):
    sc = fitted.scorecard
    assert PROTECTED not in ALL_FEATURES
    assert PROTECTED not in sc.features
    assert PROTECTED not in sc.binner.binnings


@pytest.mark.parametrize(
    "build",
    [
        lambda: Scorecard(NUMERIC_FEATURES, [*CATEGORICAL_FEATURES, PROTECTED]),
        lambda: Challenger([*NUMERIC_FEATURES, PROTECTED], []),
    ],
    ids=["scorecard", "challenger"],
)
def test_model_constructors_reject_protected_attribute(build):
    with pytest.raises(ProtectedAttributeError):
        build()


def test_challenger_ignores_protected_column_values():
    df = generate_applicants(6_000, seed=5)
    ch = Challenger(NUMERIC_FEATURES, ["purpose"]).fit(df, df[TARGET])
    flipped = df.assign(**{PROTECTED: np.where(df[PROTECTED] == "A", "B", "A")})
    assert np.array_equal(ch.predict_pd(df), ch.predict_pd(flipped))


def test_generator_is_deterministic():
    a = generate_applicants(2_000, seed=3)
    b = generate_applicants(2_000, seed=3)
    assert a.equals(b)
    assert not a.equals(generate_applicants(2_000, seed=4))
