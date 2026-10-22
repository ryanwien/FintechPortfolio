"""Template-based SAR-candidate narrative drafts.

Deterministic templates filled only with evidence the system holds (alerts, their
transactions, KYC profile). The draft is a starting point for the analyst, never a
filing: it states who / what / when / where / why / how and lists the transactions.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from .alerts import Alert
from .model import EXTERNAL_PREFIX, World, fmt_cents, fmt_date, fmt_ts, is_external

if TYPE_CHECKING:  # cases imports this module to draft narratives
    from .cases import Case

DEFAULT_MAX_ROWS: Final = 15
EXTERNAL_PREFIX_LEN: Final = len(EXTERNAL_PREFIX)

TYPOLOGY_TEXT: Final[dict[str, str]] = {
    "structuring": "structuring of cash deposits to avoid Currency Transaction Report filing",
    "velocity": "activity volume materially inconsistent with the declared customer profile",
    "rapid_movement": "rapid movement of funds (funds received and sent onward within hours)",
    "geography": "funds moved to or from higher-risk jurisdictions",
    "round_tripping": "circular movement of funds returning to the originator (round-tripping)",
    "fan": "fan-in / fan-out pattern consistent with money-mule collection or dispersal",
    "network": "membership in a connected group of accounts that exchanged funds",
}


def _party_label(world: World, party: str) -> str:
    """Customer name and id for internal accounts, ``external <id>`` otherwise."""
    # Internal accounts map to a named customer; anything else is labelled as external or left raw.
    cid = world.owner(party)
    if cid:
        c = world.customers[cid]
        return f"{c.name} ({cid})"
    if is_external(party):
        return f"external {party[EXTERNAL_PREFIX_LEN:]}"
    return party


def _paragraph(world: World, a: Alert) -> str:
    """One evidence sentence per alert, worded for its rule."""
    # Each rule has its own sentence template that reads the details the rule recorded.
    d = a.details
    # The time-span phrase is shared by every template.
    span = f"between {fmt_date(a.ts_start)} and {fmt_date(a.ts_end)}"
    if a.rule_id == "STR-01":
        return (
            f"{span}, the subject made {d['deposits']} cash deposits totalling "
            f"{fmt_cents(d['total_cents'])}, each between {fmt_cents(d['min_cents'])} and "
            f"{fmt_cents(d['max_cents'])} - below the {fmt_cents(d['ctr_threshold_cents'])} "
            f"CTR threshold - at {len(d['branches'])} branch(es) ({', '.join(d['branches'])})."
        )
    if a.rule_id == "VEL-01":
        return (
            f"{span}, 30-day inflows peaked at {fmt_cents(d['peak_30d_inflow_cents'])}, "
            f"{d['ratio_to_expected']}x the declared expected monthly activity of "
            f"{fmt_cents(d['expected_monthly_cents'])}."
        )
    if a.rule_id == "RMF-01":
        # Rapid movement: inbound and outbound totals, and how long the outflow took to reach the
        # threshold.
        return (
            f"{span}, the subject received {fmt_cents(d['in_cents'])} and outflows of "
            f"{fmt_cents(d['out_cents'])} followed ({d['ratio']:.0%} of the inbound amount); "
            f"the outflow threshold was reached {d['hours_to_threshold']} hours after receipt."
        )
    if a.rule_id == "GEO-01":
        # Geography: elevated and high-risk totals, with the tier of each country seen.
        countries = ", ".join(f"{c} ({t} risk)" for c, t in d["countries"].items())
        return (
            f"{span}, wires totalling {fmt_cents(d['elevated_cents'])} involved elevated-risk "
            f"jurisdictions {countries}; {fmt_cents(d['high_risk_cents'])} involved "
            f"high-risk jurisdictions."
        )
    if a.rule_id == "GRA-CYC":
        # Cycle: the loop path, with internal parties named and external ones kept as ids.
        path = " -> ".join(
            _party_label(world, p) if not is_external(p) else p[EXTERNAL_PREFIX_LEN:]
            for p in d["path"]
        )
        return (
            f"{span}, {fmt_cents(d['start_cents'])} moved along the loop {path} and "
            f"{fmt_cents(d['end_cents'])} ({d['retention']:.0%}) returned to the originator "
            f"within {d['hours']} hours ({d['loops']} loop(s) observed)."
        )
    if a.rule_id in ("GRA-FIN", "GRA-FOUT"):
        # Fan-in and fan-out share one template; only the verb and the flow phrase change with
        # direction.
        verb = "received funds from" if a.rule_id == "GRA-FIN" else "sent funds to"
        flow = "forwarded" if a.rule_id == "GRA-FIN" else "had been funded by inflows"
        return (
            f"{span}, the subject {verb} {d['distinct_counterparties']} distinct "
            f"counterparties ({d['new_share']:.0%} never seen before), volume "
            f"{fmt_cents(d['volume_cents'])}; pass-through ratio {d['pass_through']:.2f} "
            f"({flow} within the review window)."
        )
    if a.rule_id == "GRA-COM":
        # Community: members, how many were alerted independently, and the intra-network flow.
        return (
            f"{span}, the subject belonged to {d['network_id']}, a group of {d['size']} "
            f"accounts ({d['alerted_members']} independently alerted) that exchanged "
            f"{fmt_cents(d['intra_flow_cents'])} among themselves."
        )
    # Fallback for a rule without a template: state only that it fired.
    return f"{span}, rule {a.rule_id} fired."


def draft_narrative(world: World, case: Case, max_rows: int = DEFAULT_MAX_ROWS) -> str:
    """Markdown SAR-candidate draft for ``case`` with its largest ``max_rows`` transactions."""
    subjects = [world.customers[s] for s in case.subjects]
    # Distinct transactions behind the case's alerts, in time order.
    txns = sorted(
        {tid for a in case.alerts for tid in a.txn_ids}, key=lambda t: (world.txn_by_id[t].ts, t)
    )
    rows = [world.txn_by_id[t] for t in txns]
    # Total amount across all of the case's evidence transactions.
    total = sum(t.amount_cents for t in rows)
    # the evidence table shows the largest transactions, in time order
    key_rows = sorted(
        sorted(rows, key=lambda t: (-t.amount_cents, t.txn_id))[:max_rows],
        key=lambda t: (t.ts, t.txn_id),
    )
    # Span covering every alert in the case, used in the summary.
    start, end = min(a.ts_start for a in case.alerts), max(a.ts_end for a in case.alerts)
    # Indicator names, one per rule family in the case.
    typologies = "; ".join(TYPOLOGY_TEXT[f] for f in case.families)
    names = ", ".join(f"{c.name} ({c.customer_id})" for c in subjects)

    lines = [
        f"### SAR candidate narrative - {case.case_id} (DRAFT - analyst review required)",
        "",
        # The summary quotes the config tag; every alert from one run carries the same tag.
        f"**Summary.** Automated monitoring (config `{case.alerts[0].rule_version}`) identified "
        f"activity by {names} between {fmt_date(start)} and {fmt_date(end)} involving "
        f"{len(rows)} transactions totalling {fmt_cents(total)}. Indicators: {typologies}.",
        "",
        "**Subject profile.**",
    ]
    # Subject profile: the KYC rating and declared expectations that analysts compare activity
    # against.
    for c in subjects:
        lines.append(
            f"- {c.name} ({c.customer_id}): {c.kind}, {c.segment.replace('_', ' ')}, KYC risk "
            f"{c.kyc_risk}, declared expected monthly activity "
            f"{fmt_cents(c.expected_monthly_cents)}, home branch {c.home_branch}."
        )
    lines += ["", "**Activity.**"]
    # Community alerts are summarised once at the end, not one line per member.
    community = [a for a in case.alerts if a.rule_id == "GRA-COM"]
    # One bullet per alert; community alerts are skipped here and summarised below.
    for a in case.alerts:
        if a.rule_id == "GRA-COM":
            continue
        who = world.customers[a.customer_id].name
        lines.append(f"- [{a.alert_id} / {a.rule_id}] {who}: " + _paragraph(world, a))
    # Community summary, taken from the first community alert, since they all describe the same
    # network.
    if community:
        d = community[0].details
        ids = ", ".join(a.alert_id for a in community)
        lines.append(
            f"- [{ids} / GRA-COM] Community detection placed {len(community)} subjects in "
            f"{d['network_id']} ({d['size']} accounts, {d['alerted_members']} independently "
            f"alerted), which exchanged {fmt_cents(d['intra_flow_cents'])} among themselves "
            f"between {fmt_date(min(a.ts_start for a in community))} and "
            f"{fmt_date(max(a.ts_end for a in community))}."
        )
    # Standard assessment wording; it recommends review rather than making a filing decision.
    lines += [
        "",
        "**Assessment.** The activity has no apparent economic, business or lawful purpose "
        "that is consistent with the subjects' documented profiles, and the combination of "
        "indicators above is characteristic of the typologies listed. The analyst should "
        "confirm source of funds and relationships before filing.",
        "",
        # Evidence table: the largest transactions, shown in time order for reading.
        f"**Key transactions** (largest {len(key_rows)} of {len(rows)}, in time order):",
        "",
        "| Date/time | Txn | Channel | From | To | Amount | Jurisdiction | Branch |",
        "|---|---|---|---|---|---:|---|---|",
    ]
    for t in key_rows:
        lines.append(
            f"| {fmt_ts(t.ts)} | {t.txn_id} | {t.channel} | {_party_label(world, t.src)} | "
            f"{_party_label(world, t.dst)} | {fmt_cents(t.amount_cents)} | {t.country} | "
            f"{t.branch or ''} |"
        )
    return "\n".join(lines) + "\n"
