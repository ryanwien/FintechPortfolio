"""Time split, evaluation helpers and report formatting."""

from __future__ import annotations

import dataclasses

import numpy as np
import pytest

from fraud.config import SimConfig
from fraud.evaluation import SplitMasks, TimeSplit, recall_ceiling
from fraud.report import usd


def test_default_split_matches_documented_layout():
    split = TimeSplit.from_config(SimConfig())
    assert split.warmup == (0, 1)
    assert split.train == (2, 3, 4)
    assert split.valid == (5,)
    assert split.test == (6, 7)
    assert split.shifted == 8


def test_split_rejects_a_gap_before_the_shifted_month():
    cfg = dataclasses.replace(SimConfig(), n_periods=10, shift_period=9)
    with pytest.raises(ValueError, match="shift_period"):
        TimeSplit.from_config(cfg)


def test_split_masks_are_disjoint_and_follow_periods():
    period = np.array([0, 2, 3, 5, 6, 7, 8, 8])
    masks = SplitMasks.from_periods(period, TimeSplit.from_config(SimConfig()))
    assert masks.train.tolist() == [False, True, True, False, False, False, False, False]
    assert masks["valid"].tolist() == [False, False, False, True, False, False, False, False]
    assert masks.test.sum() == 2
    assert masks.shifted.sum() == 2
    stacked = np.vstack([masks.train, masks.valid, masks.test, masks.shifted])
    assert (stacked.sum(axis=0) <= 1).all()


def test_recall_ceiling_is_capped_at_one():
    key = "recall_at_0.5pct_alerts"
    reports = {
        "hgb": {
            "test": {"n_fraud": 10, key: {"n_alerts": 5}},
            "shifted": {"n_fraud": 3, key: {"n_alerts": 5}},
        }
    }
    assert recall_ceiling(reports) == {"test": 0.5, "shifted": 1.0}


@pytest.mark.parametrize(
    ("cents", "expected"),
    [(0, "$0"), (12_345, "$123"), (123_456_789, "$1,234,568"), (-250_000, "-$2,500")],
)
def test_usd_formats_whole_dollars(cents, expected):
    assert usd(cents) == expected
