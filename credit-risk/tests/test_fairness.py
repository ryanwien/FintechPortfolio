import numpy as np
import pytest

from credit.fairness import adverse_impact_ratio, equal_opportunity_gap, fairness_summary


def test_adverse_impact_ratio_hand_computed():
    group = np.array(["A"] * 10 + ["B"] * 10)
    approved = np.array([1] * 8 + [0] * 2 + [1] * 6 + [0] * 4)
    assert adverse_impact_ratio(approved, group, "B", "A") == pytest.approx(0.6 / 0.8)
    s = fairness_summary(approved, np.zeros(20), group)
    assert s["approval_rate"] == {"A": 0.8, "B": 0.6}
    assert s["passes_four_fifths"] is False  # 0.75 < 0.80


def test_equal_opportunity_gap_uses_only_good_applicants():
    group = np.array(["A"] * 4 + ["B"] * 4)
    y_bad = np.array([0, 0, 0, 1, 0, 0, 1, 1])
    approved = np.array([1, 1, 0, 0, 1, 0, 1, 1])
    # goods: A -> 2/3 approved, B -> 1/2 approved; bads are ignored
    assert equal_opportunity_gap(approved, y_bad, group, "B", "A") == pytest.approx(2 / 3 - 1 / 2)


def test_parity_gives_air_one():
    group = np.array(["A", "B"] * 50)
    approved = np.ones(100, dtype=bool)
    assert adverse_impact_ratio(approved, group, "B", "A") == 1.0


def test_four_fifths_boundary_passes():
    group = np.array(["A"] * 10 + ["B"] * 10)
    approved = np.array([1] * 10 + [1] * 8 + [0] * 2)
    assert fairness_summary(approved, np.zeros(20), group)["passes_four_fifths"] is True
