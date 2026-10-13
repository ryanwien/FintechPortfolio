"""Versioned config: JSON overrides and the fingerprint that tags every alert."""

import json

import pytest

from aml.config import MonitoringConfig, UnknownConfigKeyError, load_config


def test_default_config_without_overrides(cfg):
    assert load_config() == cfg
    assert cfg.tag() == f"{cfg.version}+{cfg.fingerprint()}"


def test_nested_overrides_change_the_fingerprint(tmp_path, cfg):
    path = tmp_path / "overrides.json"
    overrides = {
        "structuring": {"band_low_cents": 700000},
        "scoring": {"case_line": 50, "priority_bands": [[90, "P1"], [50, "P2"]]},
    }
    path.write_text(json.dumps(overrides), encoding="utf-8")
    loaded = load_config(path)
    assert loaded.structuring.band_low_cents == 700000
    assert loaded.scoring.case_line == 50
    assert loaded.scoring.priority_bands == ((90, "P1"), (50, "P2"))
    assert loaded.velocity == cfg.velocity
    assert loaded.fingerprint() != cfg.fingerprint()


@pytest.mark.parametrize(
    "overrides",
    [{"no_such_section": {}}, {"structuring": {"no_such_key": 1}}],
    ids=["section", "field"],
)
def test_unknown_override_key_is_rejected(tmp_path, overrides):
    path = tmp_path / "overrides.json"
    path.write_text(json.dumps(overrides), encoding="utf-8")
    with pytest.raises(UnknownConfigKeyError, match="unknown config key"):
        load_config(path)


def test_config_is_immutable(cfg):
    with pytest.raises(AttributeError):
        cfg.version = "tampered"  # type: ignore[misc]
    assert isinstance(cfg, MonitoringConfig)
