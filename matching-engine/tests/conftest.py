"""Shared fixtures for the matching-engine test suite."""

from __future__ import annotations

import pytest

from exchange import Engine
from exchange.sim import MarketConfig


@pytest.fixture
def eng() -> Engine:
    """A fresh engine that keeps every emitted event (``eng.events``)."""
    return Engine(keep_events=True)


@pytest.fixture(scope="session")
def market_cfg() -> MarketConfig:
    """Small, fixed-seed background market used by the backtest tests."""
    return MarketConfig(seed=3)
