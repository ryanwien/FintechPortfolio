import pytest

from aml.config import MonitoringConfig


@pytest.fixture
def cfg() -> MonitoringConfig:
    return MonitoringConfig()
