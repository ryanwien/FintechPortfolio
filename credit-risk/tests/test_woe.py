import numpy as np
import pandas as pd
import pytest

from credit.woe import OTHER, bin_categorical, bin_numeric, iv_strength, woe_iv


def test_woe_sign_convention():
    woe, iv = woe_iv(np.array([900, 100]), np.array([50, 50]))
    # bin 0 is mostly goods -> positive WoE (safer); bin 1 mostly bads -> negative
    assert woe[0] > 0 > woe[1]
    assert np.all(iv >= 0)


@pytest.mark.parametrize(
    ("sign", "seed", "woe_step"),
    [(1, 0, -1), (-1, 1, 1)],
    ids=["risk_rises_with_x", "risk_falls_with_x"],
)
def test_numeric_binning_is_monotonic(monotone_data, sign, seed, woe_step):
    x, y = monotone_data(sign=sign, seed=seed)
    b = bin_numeric(x, y, "x", max_bins=12, min_bin_frac=0.05)
    assert b.direction == sign
    assert np.all(np.sign(np.diff(b.woe)) == woe_step)


def test_bins_cover_population_and_respect_min_size(monotone_data):
    x, y = monotone_data(seed=2)
    b = bin_numeric(x, y, "x", max_bins=10, min_bin_frac=0.05)
    assert b.counts.sum() == len(x)
    assert b.counts.min() >= 0.05 * len(x)
    assert b.iv > 0


def test_values_outside_training_range_land_in_edge_bins(monotone_data):
    x, y = monotone_data(seed=2)
    b = bin_numeric(x, y, "x", max_bins=10, min_bin_frac=0.05)
    idx = b.bin_index(pd.Series([-1e9, 0.0, 1e9]))
    assert idx.min() == 0
    assert idx.max() == len(b.labels) - 1


def test_transform_matches_bin_woe(monotone_data):
    x, y = monotone_data(seed=3)
    b = bin_numeric(x, y, "x")
    w = b.transform(x)
    assert set(np.round(w, 10)) == set(np.round(b.woe, 10))


def test_numeric_labels_are_contiguous_intervals(monotone_data):
    x, y = monotone_data(seed=4)
    b = bin_numeric(x, y, "x")
    assert b.labels[0].startswith("(-inf, ")
    assert b.labels[-1].endswith(", inf]")
    assert len(b.labels) == len(b.edges) + 1


def test_categorical_pools_sparse_levels_and_handles_unseen():
    x = pd.Series(["a"] * 500 + ["b"] * 480 + ["rare"] * 5)
    y = pd.Series([0] * 450 + [1] * 50 + [0] * 380 + [1] * 100 + [1] * 5)
    b = bin_categorical(x, y, "c", min_bin_frac=0.02)
    assert OTHER in b.labels
    assert b.category_map["rare"] == b.category_map[OTHER]
    # unseen level maps to the pooled OTHER bin
    assert b.bin_index(pd.Series(["never_seen"]))[0] == b.category_map[OTHER]
    # labels are ordered riskiest first
    assert np.all(np.diff(b.woe) >= 0)


@pytest.mark.parametrize(
    ("iv", "label"),
    [
        (0.0, "unpredictive"),
        (0.019, "unpredictive"),
        (0.02, "weak"),
        (0.1, "medium"),
        (0.3, "strong"),
        (0.5, "suspicious (check for leakage)"),
    ],
)
def test_iv_strength_bands(iv, label):
    assert iv_strength(iv) == label
