import numpy as np
import pytest

from credit.metrics import calibration_by_decile, gini, ks, psi, psi_label


def test_psi_zero_for_identical_and_positive_for_shift():
    rng = np.random.default_rng(0)
    a = rng.normal(0, 1, 5_000)
    assert psi(a, a) == pytest.approx(0.0, abs=1e-12)
    assert psi(a, a + 0.5) > 0.1


@pytest.mark.parametrize(
    ("value", "label"),
    [
        (0.0, "stable"),
        (0.0999, "stable"),
        (0.10, "moderate shift - investigate"),
        (0.25, "significant shift - action required"),
    ],
)
def test_psi_label_bands(value, label):
    assert psi_label(value) == label


@pytest.mark.parametrize(
    ("pd_hat", "expected"),
    [([0.9, 0.8, 0.2, 0.1], 1.0), ([0.5, 0.5, 0.5, 0.5], 0.0)],
    ids=["perfect_ranking", "all_tied"],
)
def test_ks(pd_hat, expected):
    y = np.array([1, 1, 0, 0])
    assert ks(y, np.array(pd_hat)) == pytest.approx(expected)


def test_gini_of_perfect_ranking_is_one():
    assert gini(np.array([1, 0]), np.array([0.9, 0.1])) == pytest.approx(1.0)


def test_calibration_deciles_partition_sample():
    rng = np.random.default_rng(1)
    p = rng.uniform(0.01, 0.4, 1_000)
    y = (rng.random(1_000) < p).astype(int)
    c = calibration_by_decile(y, p)
    assert c["count"].sum() == 1_000
    assert list(c["decile"]) == list(range(1, 11))
    assert c["predicted_pd"].is_monotonic_decreasing
