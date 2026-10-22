"""Scenario (rules) engine: structuring, velocity, rapid movement, high-risk geography.

Each rule scans per-customer activity with rolling windows and emits raw hits; hits that
overlap in time are merged into one alert ("episode") per customer and rule. Only
activity inside the monitoring window can raise an alert; the lookback month only feeds
the windows. Detectors read amounts, timestamps, channels, counterparties and KYC
profile only; they never read ``Txn.label``.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Final

from .activity import Activity
from .alerts import Alert, Hit, episodes, make_alert
from .config import (
    GeographyConfig,
    MonitoringConfig,
    RapidMovementConfig,
    StructuringConfig,
    VelocityConfig,
)
from .model import CASH, DAY, HOUR, Txn, World

# Alert-strength formulas: base + per-unit increments, clamped to 1 by ``make_alert``.
STR_BASE: Final = 0.55
STR_PER_EXTRA_DEPOSIT: Final = 0.05
STR_PER_EXTRA_BRANCH: Final = 0.08
STR_SAME_DAY_MIN_DEPOSITS: Final = 2
VEL_BASE: Final = 0.35
VEL_PER_LIMIT_MULTIPLE: Final = 0.1
VEL_MAX_LIMIT_MULTIPLE: Final = 6.0
VEL_EVIDENCE_TXNS: Final = 10
RMF_BASE: Final = 0.5
RMF_RATIO_WEIGHT: Final = 0.3
RMF_SPEED_WEIGHT: Final = 0.2
GEO_BASE: Final = 0.4
GEO_PER_COUNTRY: Final = 0.1
GEO_HIGH_RISK_WEIGHT: Final = 0.2
GEO_HIGH_RISK_SATURATION: Final = 2  # x high_risk_min_cents for the full high-risk weight
_EPS: Final = 1e-9


def structuring(
    world: World, acts: dict[str, Activity], cfg: StructuringConfig, version: str = ""
) -> list[Alert]:
    """STR-01: a series of near-CTR cash deposits, or same-day splits that sum past the CTR."""
    out: list[Alert] = []
    win = cfg.window_days * DAY
    for cid, act in acts.items():
        # Cash deposits only: the structuring signal is cash placed into the bank.
        deps = [t for t in act.inflows if t.channel == "cash_deposit"]
        # Band: deposits just under the CTR threshold and above a floor, which is where structuring
        # sits.
        band = [t for t in deps if cfg.band_low_cents <= t.amount_cents < cfg.ctr_threshold_cents]
        hits: list[Hit] = []
        # Two-pointer sliding window: i is the right edge, and j is the left edge that moves until
        # the span fits.
        j = 0
        running = 0
        for i, t in enumerate(band):
            # Add band[i] to the running window total; old deposits are subtracted as j advances.
            running += t.amount_cents
            while t.ts - band[j].ts > win:
                running -= band[j].amount_cents
                j += 1
            # Fire when the window holds enough deposits and enough money, and its last deposit is
            # inside the monitoring period.
            if (
                i - j + 1 >= cfg.min_count
                and running >= cfg.min_total_cents
                and t.ts >= world.monitor_start_ts
            ):
                hits.append((band[j : i + 1], {"pattern": "near_threshold_series"}))
        # Same-day splits: several sub-threshold deposits on one day that together cross the CTR.
        # These slip past the rolling window, so they are checked per day.
        by_day: dict[int, list[Txn]] = defaultdict(list)
        for t in deps:
            if cfg.daily_component_min_cents <= t.amount_cents < cfg.ctr_threshold_cents:
                by_day[t.ts // DAY].append(t)
        for day, ts in sorted(by_day.items()):
            # Require at least two deposits, a daily sum past the CTR, and a day inside the
            # monitoring period.
            if (
                len(ts) >= STR_SAME_DAY_MIN_DEPOSITS
                and sum(t.amount_cents for t in ts) >= cfg.ctr_threshold_cents
                and day * DAY >= world.monitor_start_ts
            ):
                hits.append((ts, {"pattern": "same_day_split"}))
        # Merge overlapping hits into one episode per customer.
        for ep, infos in episodes(hits):
            amounts = [t.amount_cents for t in ep]
            branches = sorted({t.branch or "?" for t in ep})
            # Score: a base for the rule, plus an increment for each deposit above the minimum and
            # each extra branch.
            score = (
                STR_BASE
                + STR_PER_EXTRA_DEPOSIT * (len(ep) - cfg.min_count)
                + STR_PER_EXTRA_BRANCH * (len(branches) - 1)
            )
            # Details keep the evidence: totals, extremes, branches and which pattern or patterns
            # fired.
            details = {
                "deposits": len(ep),
                "total_cents": sum(amounts),
                "min_cents": min(amounts),
                "max_cents": max(amounts),
                "branches": branches,
                "patterns": sorted({i["pattern"] for i in infos}),
                "ctr_threshold_cents": cfg.ctr_threshold_cents,
            }
            out.append(make_alert("STR-01", "structuring", cid, ep, score, details, version))
    return out


def velocity(
    world: World, acts: dict[str, Activity], cfg: VelocityConfig, version: str = ""
) -> list[Alert]:
    """VEL-01: rolling inflow above a multiple of the KYC-declared expected monthly inflow."""
    out: list[Alert] = []
    win = cfg.window_days * DAY
    for cid, act in acts.items():
        expected = world.customers[cid].expected_monthly_cents
        # Limit: a multiple of the declared monthly inflow, floored at a minimum so tiny profiles do
        # not alert on trivial sums.
        limit = max(cfg.min_volume_cents, int(cfg.volume_multiple * expected))
        ins = act.inflows
        hits: list[Hit] = []
        j, running = 0, 0
        # Same two-pointer sliding window as structuring, over all non-cash inflows.
        for i, t in enumerate(ins):
            running += t.amount_cents
            while t.ts - ins[j].ts > win:
                running -= ins[j].amount_cents
                j += 1
            # Fire when the windowed sum exceeds the limit, and only for activity inside the
            # monitoring period.
            if running > limit and t.ts >= world.monitor_start_ts:
                # keep evidence small: the largest contributors to the window
                window = sorted(ins[j : i + 1], key=lambda x: -x.amount_cents)[:VEL_EVIDENCE_TXNS]
                hits.append((window, {"peak": running}))
        for ep, infos in episodes(hits):
            peak = max(i["peak"] for i in infos)
            # Score rises with how far over the limit the peak is, capped at the maximum multiple.
            score = VEL_BASE + VEL_PER_LIMIT_MULTIPLE * min(VEL_MAX_LIMIT_MULTIPLE, peak / limit)
            details = {
                "peak_30d_inflow_cents": peak,
                "expected_monthly_cents": expected,
                "ratio_to_expected": round(peak / max(1, expected), 1),
                "limit_cents": limit,
            }
            out.append(make_alert("VEL-01", "velocity", cid, ep, score, details, version))
    return out


def rapid_movement(
    world: World, acts: dict[str, Activity], cfg: RapidMovementConfig, version: str = ""
) -> list[Alert]:
    """RMF-01: non-cash inbound funds mostly sent onward within the outbound window."""
    out: list[Alert] = []
    in_w, out_w = cfg.in_window_hours * HOUR, cfg.out_window_hours * HOUR
    for cid, act in acts.items():
        expected = world.customers[cid].expected_monthly_cents
        # Floor: the inbound must be large against the customer's profile, not just any amount.
        floor = max(cfg.min_inbound_cents, int(cfg.profile_fraction * expected))
        hits: list[Hit] = []
        # Anchor each episode on one inbound wire, then look at what follows it.
        for t in act.inflows:
            # Cash is not a funding source for this rule; only non-cash inflows count, and only
            # after monitoring starts.
            if t.src == CASH or t.ts < world.monitor_start_ts:
                continue
            # Inbound funds within the in-window, excluding cash, are what the outbound is measured
            # against.
            ins = [x for x in act.ins_between(t.ts, t.ts + in_w) if x.src != CASH]
            in_sum = sum(x.amount_cents for x in ins)
            # Ignore inbound totals below the floor.
            if in_sum < floor:
                continue
            # Outbound: money leaving within the out-window after the anchor.
            outs = act.outs_between(t.ts, t.ts + out_w)
            out_sum = sum(x.amount_cents for x in outs)
            # Pass-through test: most of the inbound must leave within the window.
            if out_sum < cfg.out_ratio * in_sum:
                continue
            # Find when cumulative outflow first reaches out_ratio times the inbound; that moment
            # sets the speed signal.
            target, cum, elapsed = cfg.out_ratio * in_sum, 0, out_w
            for x in outs:
                cum += x.amount_cents
                if cum >= target:
                    elapsed = x.ts - t.ts
                    break
            info = {
                "in_cents": in_sum,
                "out_cents": out_sum,
                "ratio": round(out_sum / in_sum, 3),
                "hours_to_threshold": round(elapsed / HOUR, 1),
            }
            # Keep both legs as evidence, so the alert shows what came in and what went out.
            hits.append((ins + outs, info))
        for ep, infos in episodes(hits):
            # The strongest hit represents the episode: highest ratio, then largest inbound.
            best = max(infos, key=lambda i: (i["ratio"], i["in_cents"]))
            r = best["ratio"]
            # Score: a base, plus a weight for how far the ratio exceeds out_ratio, plus a weight
            # for speed (faster means higher).
            score = (
                RMF_BASE
                + RMF_RATIO_WEIGHT * min(1.0, (r - cfg.out_ratio) / max(_EPS, 1 - cfg.out_ratio))
                + RMF_SPEED_WEIGHT * max(0.0, 1 - best["hours_to_threshold"] / cfg.out_window_hours)
            )
            out.append(make_alert("RMF-01", "rapid_movement", cid, ep, score, dict(best), version))
    return out


def geography(
    world: World, acts: dict[str, Activity], cfg: GeographyConfig, version: str = ""
) -> list[Alert]:
    """GEO-01: wire volume to high-risk jurisdictions, or spread over many elevated ones."""
    out: list[Alert] = []
    win = cfg.window_days * DAY
    tiers = cfg.country_tiers
    for cid, act in acts.items():
        # Only wires to medium- or high-risk countries are in scope; low-risk and domestic wires are
        # ignored.
        wires = sorted(
            (
                t
                for t in act.inflows + act.outflows
                if t.channel == "wire" and tiers.get(t.country, "medium") in ("medium", "high")
            ),
            key=lambda t: (t.ts, t.txn_id),
        )
        hits: list[Hit] = []
        # Walk the wires in time order; each one is tested against the window that ends at it.
        for i, t in enumerate(wires):
            if t.ts < world.monitor_start_ts:
                continue
            # Window: the wires that fall within the look-back window ending at this wire.
            window = [w for w in wires[: i + 1] if t.ts - w.ts <= win]
            # High-risk money in the window.
            high = sum(w.amount_cents for w in window if tiers.get(w.country) == "high")
            elevated = sum(w.amount_cents for w in window)
            countries = sorted({w.country for w in window})
            # Fire on either a large high-risk total, or several elevated countries carrying enough
            # money.
            if high >= cfg.high_risk_min_cents or (
                len(countries) >= cfg.elevated_min_countries and elevated >= cfg.elevated_min_cents
            ):
                # Evidence: the high-risk total, the elevated total and the tier of each country
                # seen.
                info = {
                    "high_risk_cents": high,
                    "elevated_cents": elevated,
                    "countries": {c: tiers.get(c, "medium") for c in countries},
                }
                hits.append((window, info))
        for ep, infos in episodes(hits):
            # Keep the strongest evidence in the episode: most elevated money, then most countries.
            info = max(infos, key=lambda i: (i["elevated_cents"], len(i["countries"])))
            # Score: a base, a bonus per country, and a high-risk term that saturates at twice the
            # threshold.
            score = (
                GEO_BASE
                + GEO_PER_COUNTRY * len(info["countries"])
                + GEO_HIGH_RISK_WEIGHT
                * min(
                    1.0,
                    info["high_risk_cents"] / (GEO_HIGH_RISK_SATURATION * cfg.high_risk_min_cents),
                )
            )
            out.append(make_alert("GEO-01", "geography", cid, ep, score, dict(info), version))
    return out


def run_rules(world: World, acts: dict[str, Activity], cfg: MonitoringConfig) -> list[Alert]:
    """All four scenario rules, stamped with the config tag."""
    # Stamp every rule's alerts with the config tag, so each alert records which configuration
    # produced it.
    tag = cfg.tag()
    return (
        structuring(world, acts, cfg.structuring, tag)
        + velocity(world, acts, cfg.velocity, tag)
        + rapid_movement(world, acts, cfg.rapid_movement, tag)
        + geography(world, acts, cfg.geography, tag)
    )
