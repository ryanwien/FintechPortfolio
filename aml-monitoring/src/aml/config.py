"""Versioned, configurable detection thresholds.

Every alert carries ``MonitoringConfig.version`` and the config fingerprint (a hash of
all thresholds), so a model-risk reviewer can tie any alert back to the exact
parameters that produced it. Overrides are loaded from JSON (stdlib only).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, fields, is_dataclass, replace
from pathlib import Path
from typing import Any, Final

from .model import COUNTRY_TIERS

# Run defaults for ``python -m aml.run`` and ``generate_world``. They are not part of
# ``MonitoringConfig`` because the config fingerprint must cover thresholds only.
DEFAULT_SEED: Final = 20261013
DEFAULT_CUSTOMERS: Final = 4000
FINGERPRINT_HEX_CHARS: Final = 12


class UnknownConfigKeyError(KeyError):
    """Raised when an override names a field the config section does not have."""


@dataclass(frozen=True, slots=True)
class StructuringConfig:
    """STR-01: near-CTR cash deposit series and same-day splits."""

    window_days: int = 10
    ctr_threshold_cents: int = 10_000_00
    band_low_cents: int = 8_000_00  # "just under the CTR line" band: [band_low, ctr)
    min_count: int = 3
    min_total_cents: int = 20_000_00
    # same-day aggregation: >= 2 sub-CTR deposits of at least this size summing >= CTR
    daily_component_min_cents: int = 3_000_00


@dataclass(frozen=True, slots=True)
class VelocityConfig:
    """VEL-01: rolling inflow vs the KYC-declared expected monthly inflow."""

    window_days: int = 30
    volume_multiple: float = 4.0  # x declared expected monthly inflow
    min_volume_cents: int = 25_000_00


@dataclass(frozen=True, slots=True)
class RapidMovementConfig:
    """RMF-01: inbound funds sent onward within a short window."""

    in_window_hours: int = 24
    out_window_hours: int = 48
    min_inbound_cents: int = 7_500_00
    out_ratio: float = 0.80
    profile_fraction: float = 0.5  # inbound must also be >= this x expected monthly


@dataclass(frozen=True, slots=True)
class GeographyConfig:
    """GEO-01: wire volume to elevated / high-risk jurisdictions."""

    window_days: int = 30
    high_risk_min_cents: int = 20_000_00
    elevated_min_countries: int = 3
    elevated_min_cents: int = 15_000_00
    country_tiers: dict[str, str] = field(default_factory=lambda: dict(COUNTRY_TIERS))


@dataclass(frozen=True, slots=True)
class GraphConfig:
    """GRA-*: temporal cycles, fan-in/out baselines and Louvain communities."""

    cycle_window_days: int = 21
    cycle_max_len: int = 5
    cycle_min_amount_cents: int = 10_000_00
    cycle_min_retention: float = 0.80
    cycle_max_retention: float = 1.05
    fan_window_days: int = 7
    fan_in_min: int = 5
    fan_out_min: int = 5
    fan_min_volume_cents: int = 5_000_00
    degree_mad_k: float = 6.0
    new_counterparty_share: float = 0.6
    pass_through_ratio: float = 0.6
    pass_through_hours: int = 72
    community_min_edge_cents: int = 2_000_00
    community_min_size: int = 3
    community_min_alerted: int = 2
    community_seed: int = 7


@dataclass(frozen=True, slots=True)
class ScoringConfig:
    """Case scoring weights, the line and priority bands."""

    rule_weights: dict[str, float] = field(
        default_factory=lambda: {
            "STR-01": 0.55,
            "VEL-01": 0.30,
            "RMF-01": 0.45,
            "GEO-01": 0.40,
            "GRA-CYC": 0.65,
            "GRA-FIN": 0.50,
            "GRA-FOUT": 0.45,
            "GRA-COM": 0.35,
        }
    )
    kyc_multiplier: dict[str, float] = field(
        default_factory=lambda: {"low": 0.9, "medium": 1.0, "high": 1.15}
    )
    repeat_bonus: float = 0.10
    multi_family_bonus: float = 6.0
    case_line: float = 40.0  # "the line": cases scoring below it are suppressed
    priority_bands: tuple[tuple[float, str], ...] = ((80.0, "P1"), (60.0, "P2"), (40.0, "P3"))
    alert_merge_gap_days: int = 3


@dataclass(frozen=True, slots=True)
class AnalystConfig:
    """Simulated analyst behaviour: dispositions follow ground truth with noise."""

    p_escalate_if_suspicious: float = 0.92
    p_escalate_if_benign: float = 0.12
    p_file_if_suspicious: float = 0.95
    p_file_if_benign: float = 0.35
    seed: int = 11


@dataclass(frozen=True, slots=True)
class MonitoringConfig:
    """All detection, scoring and simulation parameters; ``tag`` identifies a version."""

    version: str = "2026.10-r4"
    structuring: StructuringConfig = field(default_factory=StructuringConfig)
    velocity: VelocityConfig = field(default_factory=VelocityConfig)
    rapid_movement: RapidMovementConfig = field(default_factory=RapidMovementConfig)
    geography: GeographyConfig = field(default_factory=GeographyConfig)
    graph: GraphConfig = field(default_factory=GraphConfig)
    scoring: ScoringConfig = field(default_factory=ScoringConfig)
    analyst: AnalystConfig = field(default_factory=AnalystConfig)

    def fingerprint(self) -> str:
        """Short SHA-256 of every parameter (canonical JSON)."""
        # Serialise every parameter with sorted keys, so the same settings always produce the same
        # bytes and fingerprint.
        blob = json.dumps(asdict(self), sort_keys=True, default=list)
        return hashlib.sha256(blob.encode()).hexdigest()[:FINGERPRINT_HEX_CHARS]

    def tag(self) -> str:
        """``<version>+<fingerprint>``, stamped on every alert and audit entry."""
        return f"{self.version}+{self.fingerprint()}"


def _apply[C](obj: C, overrides: dict[str, Any]) -> C:
    """Recursively apply JSON overrides to a frozen config dataclass."""
    changes: dict[str, Any] = {}
    # Valid keys at this level; anything else is rejected rather than silently ignored.
    names = {f.name for f in fields(obj)}
    # Walk the overrides one key at a time, checking each against the dataclass fields first.
    for key, value in overrides.items():
        if key not in names:
            raise UnknownConfigKeyError(f"unknown config key {key!r} for {type(obj).__name__}")
        current = getattr(obj, key)
        # Nested sections recurse, so an override can change one threshold inside a section.
        if is_dataclass(current) and isinstance(value, dict):
            changes[key] = _apply(current, value)
        elif isinstance(current, tuple):
            # JSON gives lists where the config uses tuples; convert them back, or equality and
            # hashing break.
            changes[key] = tuple(tuple(v) if isinstance(v, list) else v for v in value)
        else:
            changes[key] = value
    # Build a new frozen instance; the original config is never mutated.
    return replace(obj, **changes)


def load_config(path: str | Path | None = None) -> MonitoringConfig:
    """Default config, optionally overridden by a JSON file of nested sections."""
    cfg = MonitoringConfig()
    # No overrides file means the defaults are used unchanged.
    if path is None:
        return cfg
    # The overrides file is nested JSON with the same shape as the dataclasses.
    return _apply(cfg, json.loads(Path(path).read_text(encoding="utf-8")))
