from decimal import Decimal

import numpy as np
import pytest

from credit.config import EAD_FRACTION, LGD, LIFETIME_MARGIN
from credit.strategy import (
    UnitEconomics,
    cutoff_table,
    expected_loss_cents,
    expected_profit_cents,
    fmt_usd,
    profit_maximising_cutoff,
    realized_profit_cents,
)


@pytest.fixture
def econ() -> UnitEconomics:
    """10% margin, 60% LGD, 50% EAD: a defaulted loan loses 30% of principal."""
    return UnitEconomics(Decimal("0.10"), Decimal("0.60"), Decimal("0.50"))


@pytest.mark.parametrize(
    ("defaulted", "profit"), [(False, 100_000), (True, -300_000)], ids=["repaid", "defaulted"]
)
def test_realized_profit_in_cents(econ, defaulted, profit):
    assert realized_profit_cents(1_000_000, defaulted, econ) == profit


def test_expected_profit_is_zero_at_breakeven_pd(econ):
    assert econ.breakeven_pd == Decimal("0.10") / Decimal("0.40")
    assert expected_profit_cents(1_000_000, 0.25, econ) == 0


def test_expected_loss_is_pd_times_loss_given_default(econ):
    assert expected_loss_cents(1_000_000, 0.10, econ) == 30_000


def test_unit_economics_defaults_come_from_config():
    d = UnitEconomics().as_dict()
    assert d["lifetime_margin"] == str(LIFETIME_MARGIN)
    assert d["lgd"] == str(LGD)
    assert d["ead_fraction"] == str(EAD_FRACTION)
    assert set(d) == {"lifetime_margin", "lgd", "ead_fraction", "breakeven_pd"}


@pytest.mark.parametrize(
    ("cents", "text"),
    [(-123_456, "-$1,234.56"), (0, "$0.00"), (5, "$0.05"), (100_000_000, "$1,000,000.00")],
)
def test_fmt_usd(cents, text):
    assert fmt_usd(cents) == text


def test_cutoff_table_and_profit_cutoff():
    e = UnitEconomics(Decimal("0.10"), Decimal("1"), Decimal("1"))
    scores = np.array([700, 650, 600, 550])
    pd_hat = np.array([0.01, 0.05, 0.08, 0.50])
    y = np.array([0, 0, 1, 1])
    loans = np.array([100_000] * 4)
    t = cutoff_table(scores, pd_hat, y, loans, [500, 575, 625, 675], e)
    assert [r["approval_rate"] for r in t] == [1.0, 0.75, 0.5, 0.25]
    assert t[1]["bad_rate_approved"] == pytest.approx(1 / 3)
    assert all(isinstance(r["realized_profit_cents"], int) for r in t)
    # 0.5 PD loan loses money in expectation; 0.08 PD loan still earns (breakeven ~0.0909)
    assert profit_maximising_cutoff(t) == 575


def test_profit_cutoff_ties_go_to_the_more_conservative_cutoff():
    e = UnitEconomics()
    t = cutoff_table(np.array([600]), np.array([0.01]), np.array([0]), np.array([1]), [500, 550], e)
    assert t[0]["expected_profit_cents"] == t[1]["expected_profit_cents"]
    assert profit_maximising_cutoff(t) == 550
