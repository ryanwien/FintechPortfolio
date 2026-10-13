"""PSI drift metric and status thresholds."""

from __future__ import annotations

import numpy as np
import pytest

from fraud.drift import PSI_ALERT, PSI_WARN, psi, psi_table, status


def test_psi_near_zero_for_same_distribution():
    rng = np.random.default_rng(0)
    a, b = rng.normal(size=50_000), rng.normal(size=50_000)
    assert psi(a, b) < 0.01


def test_psi_flags_a_real_shift():
    rng = np.random.default_rng(1)
    base = rng.normal(size=50_000)
    assert PSI_WARN <= psi(base, rng.normal(0.4, 1, 50_000)) < PSI_ALERT
    assert psi(base, rng.normal(1.0, 1, 50_000)) >= PSI_ALERT


def test_psi_handles_discrete_and_constant_features():
    rng = np.random.default_rng(2)
    flag = (rng.random(20_000) < 0.05).astype(float)
    assert psi(flag, flag[::-1]) < 0.01
    more = (rng.random(20_000) < 0.30).astype(float)
    assert psi(flag, more) >= PSI_ALERT
    const = np.zeros(1_000)
    assert psi(const, const) == 0.0
    assert np.isfinite(psi(const, np.ones(1_000)))


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0.0, "ok"),
        (0.05, "ok"),
        (PSI_WARN, "warn"),
        (0.2, "warn"),
        (PSI_ALERT, "ALERT"),
        (0.3, "ALERT"),
    ],
)
def test_status_thresholds(value, expected):
    assert status(value) == expected


def test_psi_table_sorts_most_drifted_first():
    rng = np.random.default_rng(3)
    ref = rng.normal(size=(10_000, 2))
    cur = np.column_stack([rng.normal(size=10_000), rng.normal(1.5, 1, 10_000)])
    rows = psi_table(ref, cur, ["stable", "shifted"])
    assert rows[0]["feature"] == "shifted"
    assert rows[0]["status"] == "ALERT"
    assert rows[1]["status"] == "ok"
