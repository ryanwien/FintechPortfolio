"""Evaluation against ground truth, plus threshold tuning (above/below-the-line).

Definitions (customer-level ground truth: a customer is "suspicious" if it is a member
of any injected typology instance):

* alert precision      = alerts on suspicious customers / all alerts
* false-positive rate  = benign customers with >= 1 alert / benign customers
* instance detection   = typology instances with >= 1 member alerted (or, "at the line",
                         with >= 1 member in an above-the-line case)
* alerts per 1k        = alerts / customers * 1000 (analyst workload proxy)
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import replace
from typing import Any, Final, TypeVar

from .alerts import Alert, dedupe_alerts
from .cases import Case
from .config import MonitoringConfig
from .detect import DetectionResult
from .graph import degree_baselines, detect_fans
from .model import DAY, World
from .rules import geography, rapid_movement, structuring, velocity

RATE_DECIMALS: Final = 4
PER_1K: Final = 1000
CENTS_PER_DOLLAR: Final = 100
# Case-score lines for the above/below-the-line sweep: 0, 5, ..., 95.
LINE_SWEEP_POINTS: Final = tuple(float(x) for x in range(0, 100, 5))
# One-rule-at-a-time threshold grids (values in the config's units, cents or counts).
STR_BAND_LOW_GRID: Final = (6_000_00, 7_000_00, 8_000_00, 9_000_00)
STR_MIN_COUNT_GRID: Final = (2, 3, 4, 5)
RMF_OUT_WINDOW_HOURS_GRID: Final = (24, 48, 72, 96)
RMF_OUT_RATIO_GRID: Final = (0.6, 0.7, 0.8, 0.9)
VEL_VOLUME_MULTIPLE_GRID: Final = (2.0, 3.0, 4.0, 6.0, 8.0)
GEO_HIGH_RISK_MIN_GRID: Final = (10_000_00, 20_000_00, 40_000_00, 80_000_00)
FAN_IN_MIN_GRID: Final = (3, 4, 5, 6, 8)

_V = TypeVar("_V")


def _rate(num: int, den: int) -> float:
    """Ratio rounded to ``RATE_DECIMALS``; 0.0 when the denominator is zero."""
    return round(num / den, RATE_DECIMALS) if den else 0.0


def evaluate(
    world: World, det: DetectionResult, cases: list[Case], cfg: MonitoringConfig
) -> dict[str, Any]:
    """World, alert, detection, case, look-alike and analyst metrics for ``metrics.json``."""
    truth = world.truth()
    n_cust = len(world.customers)
    benign = [c for c in world.customers if c not in truth]
    alerts = det.alerts
    alerted = {a.customer_id for a in alerts}
    tp_alerts = sum(a.customer_id in truth for a in alerts)

    per_rule: dict[str, dict[str, Any]] = {}
    for rule in sorted({a.rule_id for a in alerts}):
        ra = [a for a in alerts if a.rule_id == rule]
        tp = sum(a.customer_id in truth for a in ra)
        per_rule[rule] = {"alerts": len(ra), "true_positive": tp, "precision": _rate(tp, len(ra))}

    atl = [c for c in cases if c.above_line]
    atl_subjects = {s for c in atl for s in c.subjects}
    per_typology: dict[str, dict[str, Any]] = {}
    for typ in sorted({i.typology for i in world.instances}):
        insts = [i for i in world.instances if i.typology == typ]
        members = [m for i in insts for m in i.members]
        variants: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
        for i in insts:
            v = variants[i.variant if typ != "mule_network" else "ring"]
            v[0] += 1
            v[1] += any(m in alerted for m in i.members)
            v[2] += any(m in atl_subjects for m in i.members)
        per_typology[typ] = {
            "instances": len(insts),
            "detection_rate_alerts": _rate(
                sum(any(m in alerted for m in i.members) for i in insts), len(insts)
            ),
            "detection_rate_at_line": _rate(
                sum(any(m in atl_subjects for m in i.members) for i in insts), len(insts)
            ),
            "member_coverage_at_line": _rate(sum(m in atl_subjects for m in members), len(members)),
            "by_variant": {
                k: {"instances": v[0], "alerted": v[1], "at_line": v[2]}
                for k, v in sorted(variants.items())
            },
        }
    n_inst = len(world.instances)
    det_alerts = sum(any(m in alerted for m in i.members) for i in world.instances)
    det_line = sum(any(m in atl_subjects for m in i.members) for i in world.instances)

    lookalikes = {
        seg: {
            "customers": len(ms),
            "alerted": sum(m in alerted for m in ms),
            "in_case_above_line": sum(m in atl_subjects for m in ms),
        }
        for seg, ms in world.lookalikes.items()
    }

    def case_true(c: Case) -> bool:
        """True when any subject of the case is a labelled suspicious customer."""
        return any(s in truth for s in c.subjects)

    reviewed = [c for c in atl if c.disposition is not None]
    sars = [c for c in reviewed if c.disposition == "SAR_FILED"]
    analyst = {
        "cases_reviewed": len(reviewed),
        "sars_filed": len(sars),
        "sar_conversion_rate": _rate(len(sars), len(reviewed)),
        "sars_on_true_cases": sum(case_true(c) for c in sars),
        "sars_on_benign_cases": sum(not case_true(c) for c in sars),
        "true_cases_closed_without_sar": sum(
            case_true(c) and c.disposition != "SAR_FILED" for c in reviewed
        ),
        "decision_accuracy": _rate(
            sum(case_true(c) == (c.disposition == "SAR_FILED") for c in reviewed), len(reviewed)
        ),
    }
    return {
        "world": {
            "customers": n_cust,
            "accounts": len(world.accounts),
            "transactions": len(world.txns),
            "suspicious_customers": len(truth),
            "typology_instances": n_inst,
            "monitoring_days": (world.end_ts - world.monitor_start_ts) // DAY,
        },
        "alerts": {
            "total": len(alerts),
            "true_positive": tp_alerts,
            "precision": _rate(tp_alerts, len(alerts)),
            "per_1k_customers": round(PER_1K * len(alerts) / n_cust, 1),
            "customers_alerted": len(alerted),
            "customer_recall": _rate(sum(c in alerted for c in truth), len(truth)),
            "false_positive_rate": _rate(sum(c in alerted for c in benign), len(benign)),
            "per_rule": per_rule,
        },
        "detection": {
            "instance_detection_rate_alerts": _rate(det_alerts, n_inst),
            "instance_detection_rate_at_line": _rate(det_line, n_inst),
            "per_typology": per_typology,
        },
        "cases": {
            "total": len(cases),
            "above_line": len(atl),
            "below_line": len(cases) - len(atl),
            "network_cases": sum(c.network_id is not None for c in cases),
            "precision_above_line": _rate(sum(case_true(c) for c in atl), len(atl)),
            "per_1k_customers_above_line": round(PER_1K * len(atl) / n_cust, 1),
            "line": cfg.scoring.case_line,
            "by_priority": {
                p: sum(c.priority == p for c in atl) for _, p in cfg.scoring.priority_bands
            },
        },
        "benign_lookalikes": lookalikes,
        "analyst_simulation": analyst,
    }


def line_sweep(
    world: World, cases: list[Case], lines: Iterable[float] = LINE_SWEEP_POINTS
) -> list[dict[str, Any]]:
    """Move the case-score line: workload vs typology detection (the trade-off curve)."""
    truth = world.truth()
    n_cust = len(world.customers)
    out = []
    for line in lines:
        atl = [c for c in cases if c.score >= line]
        subj = {s for c in atl for s in c.subjects}
        detected = sum(any(m in subj for m in i.members) for i in world.instances)
        tp = sum(any(s in truth for s in c.subjects) for c in atl)
        out.append(
            {
                "line": line,
                "cases": len(atl),
                "cases_per_1k": round(PER_1K * len(atl) / n_cust, 1),
                "detection_rate": _rate(detected, len(world.instances)),
                "case_precision": _rate(tp, len(atl)),
            }
        )
    return out


def _rule_point(world: World, alerts: list[Alert], target: str, gap_days: int) -> dict[str, Any]:
    """Alert volume, precision and target-typology detection for one sweep value."""
    truth = world.truth()
    alerts = dedupe_alerts(alerts, gap_days)
    alerted = {a.customer_id for a in alerts}
    insts = [i for i in world.instances if i.typology == target]
    tp = sum(a.customer_id in truth for a in alerts)
    return {
        "alerts": len(alerts),
        "true_positive": tp,
        "precision": _rate(tp, len(alerts)),
        "target_typology": target,
        "target_detection_rate": _rate(
            sum(any(m in alerted for m in i.members) for i in insts), len(insts)
        ),
    }


def rule_sweeps(
    world: World, det: DetectionResult, cfg: MonitoringConfig
) -> dict[str, list[dict[str, Any]]]:
    """Above/below-the-line testing: re-run one rule at a time with shifted thresholds."""
    acts = det.acts
    gap = cfg.scoring.alert_merge_gap_days
    out: dict[str, list[dict[str, Any]]] = {}

    def sweep(
        name: str,
        values: Sequence[_V],
        run: Callable[[_V], list[Alert]],
        target: str,
        fmt: Callable[[_V], object] = lambda v: v,
    ) -> None:
        """Run ``run`` at each value and store the points under ``name``; ``fmt`` labels them."""
        pts = []
        for v in values:
            p = _rule_point(world, run(v), target, gap)
            p["value"] = fmt(v)
            pts.append(p)
        out[name] = pts

    s = cfg.structuring
    sweep(
        "STR-01.band_low_dollars",
        STR_BAND_LOW_GRID,
        lambda v: structuring(world, acts, replace(s, band_low_cents=v)),
        "structuring",
        lambda v: v // CENTS_PER_DOLLAR,
    )
    sweep(
        "STR-01.min_count",
        STR_MIN_COUNT_GRID,
        lambda v: structuring(world, acts, replace(s, min_count=v)),
        "structuring",
    )
    r = cfg.rapid_movement
    sweep(
        "RMF-01.out_window_hours",
        RMF_OUT_WINDOW_HOURS_GRID,
        lambda v: rapid_movement(world, acts, replace(r, out_window_hours=v)),
        "rapid_movement",
    )
    sweep(
        "RMF-01.out_ratio",
        RMF_OUT_RATIO_GRID,
        lambda v: rapid_movement(world, acts, replace(r, out_ratio=v)),
        "rapid_movement",
    )
    vcfg = cfg.velocity
    sweep(
        "VEL-01.volume_multiple",
        VEL_VOLUME_MULTIPLE_GRID,
        lambda v: velocity(world, acts, replace(vcfg, volume_multiple=v)),
        "rapid_movement",
    )
    g = cfg.geography
    sweep(
        "GEO-01.high_risk_min_dollars",
        GEO_HIGH_RISK_MIN_GRID,
        lambda v: geography(world, acts, replace(g, high_risk_min_cents=v)),
        "high_risk_layering",
        lambda v: v // CENTS_PER_DOLLAR,
    )
    gc = cfg.graph

    def fans(v: int) -> list[Alert]:
        """Fan-in (GRA-FIN) alerts with ``fan_in_min`` set to ``v``, for one sweep point."""
        c2 = replace(gc, fan_in_min=v)
        return [
            a
            for a in detect_fans(world, acts, c2, "", degree_baselines(world, c2))
            if a.rule_id == "GRA-FIN"
        ]

    sweep("GRA-FIN.fan_in_min", FAN_IN_MIN_GRID, fans, "mule_network")
    return out
