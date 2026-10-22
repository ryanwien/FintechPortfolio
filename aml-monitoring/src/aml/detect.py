"""Detection pipeline: rules + graph analytics -> deduplicated, id-stamped alerts.

Order matters: community detection runs last because it only looks around customers that
the rules and the other graph detectors already alerted on.
"""

from __future__ import annotations

from dataclasses import dataclass

from .activity import Activity, index_activity
from .alerts import Alert, dedupe_alerts
from .config import MonitoringConfig
from .graph import (
    CycleHit,
    Network,
    cycle_alerts,
    degree_baselines,
    detect_communities,
    detect_cycles,
    detect_fans,
)
from .model import World
from .rules import run_rules


@dataclass(frozen=True, slots=True)
class DetectionResult:
    """Output of ``run_detection``: merged alerts, flagged networks and the indexed activity.

    ``acts`` is reused by ``rule_sweeps`` so threshold sweeps do not re-index the world.
    """

    alerts: list[Alert]
    networks: list[Network]
    cycles: list[CycleHit]
    degree_thresholds: dict[str, int]
    acts: dict[str, Activity]

    def network_of(self) -> dict[str, str]:
        """Customer id -> id of the flagged network it belongs to."""
        return {m: n.network_id for n in self.networks for m in n.members}


def run_detection(world: World, cfg: MonitoringConfig) -> DetectionResult:
    """Run every detector over ``world`` and merge the alerts."""
    tag = cfg.tag()
    acts = index_activity(world)
    raw = run_rules(world, acts, cfg)
    cycles = detect_cycles(world, cfg.graph)
    raw += cycle_alerts(cycles, cfg.graph, tag)
    thresholds = degree_baselines(world, cfg.graph)
    raw += detect_fans(world, acts, cfg.graph, tag, thresholds)
    alerted = {a.customer_id for a in raw}
    com_alerts, networks = detect_communities(world, alerted, cfg.graph, tag)
    alerts = dedupe_alerts(raw + com_alerts, cfg.scoring.alert_merge_gap_days)
    return DetectionResult(alerts, networks, cycles, thresholds, acts)
