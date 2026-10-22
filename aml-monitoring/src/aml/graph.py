"""Graph analytics with networkx.

* Temporal cycle detection (round-tripping): ``nx.simple_cycles`` on a value-filtered
  transfer graph, then each static cycle is checked for a time-ordered chain of
  transfers inside the window that conserves value hop to hop.
* Fan-in / fan-out degree anomalies: per-segment degree baselines from weekly
  ``nx.DiGraph`` snapshots (median + k*MAD), combined with "new counterparty" share and
  pass-through ratio so landlords, charities and payroll do not look like mules.
* Community detection (Louvain) around alerted customers to group mule rings and
  loops into one network case.
"""

from __future__ import annotations

import math
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Final

import networkx as nx

from .activity import Activity
from .alerts import Alert, Hit, episodes, make_alert
from .config import GraphConfig
from .model import CASH, DAY, HOUR, Txn, World, is_external

# Alert-strength formulas: base + per-unit increments, clamped to 1 by ``make_alert``.
CYC_BASE: Final = 0.6
CYC_PER_LOOP: Final = 0.05
CYC_PER_EXTRA_PARTY: Final = 0.05
CYC_MIN_PATH_NODES: Final = 3  # 2-party loop: a -> b -> a
CYC_RETENTION_WEIGHT: Final = 0.2
FAN_BASE: Final = 0.5
FAN_PER_EXTRA_COUNTERPARTY: Final = 0.05
FAN_PASS_THROUGH_WEIGHT: Final = 0.2
FAN_MAX_PASS_THROUGH: Final = 9.99
COM_BASE: Final = 0.4
COM_PER_ALERTED_MEMBER: Final = 0.1
COM_PER_EXTRA_MEMBER: Final = 0.05
COM_MAX_INCREMENTS: Final = 4
COM_BASE_SIZE: Final = 3
BASELINE_WEEKS: Final = 4  # weekly snapshots taken from this many weeks before monitoring
MAD_TO_SIGMA: Final = 1.4826  # scales MAD to a normal standard deviation
CENTS_PER_DOLLAR: Final = 100.0
MIN_CYCLE_PARTIES: Final = 2

# ---------------------------------------------------------------------------- cycles


@dataclass(frozen=True, slots=True)
class CycleHit:
    """A confirmed temporal loop: parties in flow order and the hop transactions."""

    parties: tuple[str, ...]  # in flow order, first party repeated implicitly at the end
    txns: tuple[Txn, ...]

    @property
    def retention(self) -> float:
        """Last hop amount over first hop amount; 1.0 means the value was fully conserved."""
        return self.txns[-1].amount_cents / self.txns[0].amount_cents

    @property
    def hours(self) -> float:
        """Elapsed hours from the first hop to the last hop."""
        return (self.txns[-1].ts - self.txns[0].ts) / HOUR


def transfer_graph(world: World, lo: int, hi: int, min_amount: int) -> nx.DiGraph:
    """Party-level graph; each edge keeps its transfers (time-ordered) in ``txns``."""
    g = nx.DiGraph()
    # Each transfer is considered once; each party pair becomes one edge that keeps its transfers in
    # time order.
    for t in world.txns:
        # Keep only transfers inside the window, above the minimum amount, and not cash, which has
        # no counterparty.
        if t.ts < lo or t.ts > hi or t.amount_cents < min_amount or CASH in (t.src, t.dst):
            continue
        u, v = world.party(t.src), world.party(t.dst)
        # Self-transfers between a party and itself carry no network signal.
        if u == v:
            continue
        if not g.has_edge(u, v):
            # The first transfer on a pair creates the edge; later ones append to its list.
            g.add_edge(u, v, txns=[])
        g[u][v]["txns"].append(t)
    return g


def _extend(
    path: list[Txn], edges: list[list[Txn]], k: int, cfg: GraphConfig, used: set[str]
) -> list[Txn] | None:
    """Depth-first: extend ``path`` with a later, value-conserving transfer on hop ``k``."""
    if k == len(edges):
        # A path with every hop filled is a complete cycle.
        return path
    prev = path[-1]
    # Try each candidate transfer on this hop in time order.
    for t in edges[k]:
        # Skip transfers already claimed by another hit, or not later than the previous hop.
        if t.txn_id in used or t.ts <= prev.ts:
            continue
        # Edges are time-sorted, so once a transfer falls outside the cycle window no later one can
        # fit.
        if t.ts - path[0].ts > cfg.cycle_window_days * DAY:
            break
        # Value conservation: this hop must carry a similar amount to the previous hop, within the
        # retention band.
        r = t.amount_cents / prev.amount_cents
        if cfg.cycle_min_retention <= r <= cfg.cycle_max_retention:
            # Recurse for the next hop; the first complete chain found is taken.
            found = _extend([*path, t], edges, k + 1, cfg, used)
            if found:
                return found
    return None


def _canonical(cycle: list[str]) -> tuple[str, ...]:
    """Rotate a cycle to start at its smallest node (output order must not depend on
    networkx's internal set ordering, which varies with string hash randomisation)."""
    # Rotate so the smallest name starts the cycle; this gives one canonical form per loop.
    k = cycle.index(min(cycle))
    return tuple(cycle[k:] + cycle[:k])


def detect_cycles(world: World, cfg: GraphConfig) -> list[CycleHit]:
    """Static cycles confirmed as time-ordered, value-conserving chains inside the window.

    Each transaction is used by at most one hit; cycles are visited shortest first in
    canonical order so the result is independent of hash seeds.
    """
    # Look back from monitoring start by the cycle window, so loops that straddle the boundary are
    # still caught.
    lo = world.monitor_start_ts - cfg.cycle_window_days * DAY
    # Build the party graph from transfers at or above the minimum cycle amount.
    g = transfer_graph(world, lo, world.end_ts, cfg.cycle_min_amount_cents)
    hits: list[CycleHit] = []
    # Transactions already used by an earlier hit; each dollar of a loop is counted only once.
    used: set[str] = set()
    # Enumerate simple cycles up to the length bound, canonicalise them, and sort shortest first for
    # a deterministic order.
    cycles = sorted(
        (
            _canonical(c)
            for c in nx.simple_cycles(g, length_bound=cfg.cycle_max_len)
            if len(c) >= MIN_CYCLE_PARTIES
        ),
        key=lambda c: (len(c), c),
    )
    for cyc in cycles:
        # Loops made only of external counterparties do not concern any customer.
        if all(is_external(p) for p in cyc):
            continue
        n = len(cyc)
        # A loop can be entered at any hop, so try every rotation as the starting point.
        for r in range(n):
            nodes = cyc[r:] + cyc[:r]
            # Each hop's transfers, in time order, for this rotation.
            edges = [g[nodes[k]][nodes[(k + 1) % n]]["txns"] for k in range(n)]
            # Try every transfer on the first hop as the start of a chain.
            for t0 in edges[0]:
                if t0.txn_id in used:
                    continue
                chain = _extend([t0], edges, 1, cfg, used)
                # Keep a chain only if its last hop lands in the monitored period; earlier chains
                # are lookback only.
                if chain and chain[-1].ts >= world.monitor_start_ts:
                    hits.append(CycleHit(tuple(nodes), tuple(chain)))
                    # Consume the chain's transfers so they cannot seed another hit.
                    used.update(t.txn_id for t in chain)
    # Final order: time of the first hop, then transaction id.
    return sorted(hits, key=lambda h: (h.txns[0].ts, h.txns[0].txn_id))


def cycle_alerts(hits: list[CycleHit], cfg: GraphConfig, version: str) -> list[Alert]:
    """GRA-CYC: one alert per internal party per episode of confirmed loops."""
    per_customer: dict[str, list[Hit]] = defaultdict(list)
    for h in hits:
        info = {
            "path": [*h.parties, h.parties[0]],
            "start_cents": h.txns[0].amount_cents,
            "end_cents": h.txns[-1].amount_cents,
            "retention": round(h.retention, 3),
            "hours": round(h.hours, 1),
        }
        # Each loop counts for every internal party on it; external parties get no alert.
        for p in sorted(set(h.parties)):
            if not is_external(p):
                per_customer[p].append((list(h.txns), info))
    out = []
    # Per customer, merge overlapping loops into episodes.
    for cid in sorted(per_customer):
        for ep, infos in episodes(per_customer[cid]):
            # The strongest loop is the evidence: the one that moved the largest starting amount.
            best = max(infos, key=lambda i: i["start_cents"])
            # Clamp retention to 1.0 so a loop that grew cannot push the score past the retention
            # term.
            ret = min(1.0, best["retention"])
            # Score: a base, plus a bump for each loop, plus a bump for each party beyond a two-
            # party path, plus a bonus for value kept through the loop.
            score = (
                CYC_BASE
                + CYC_PER_LOOP * len(infos)
                + CYC_PER_EXTRA_PARTY * (len(best["path"]) - CYC_MIN_PATH_NODES)
                + CYC_RETENTION_WEIGHT
                * (ret - cfg.cycle_min_retention)
                / (1 - cfg.cycle_min_retention)
            )
            # Keep the loop count alongside the evidence.
            details = dict(best, loops=len(infos))
            out.append(make_alert("GRA-CYC", "round_tripping", cid, ep, score, details, version))
    return out


# ------------------------------------------------------------------------- fan in / out


def degree_baselines(world: World, cfg: GraphConfig) -> dict[str, int]:
    """Robust per-kind degree thresholds from weekly transfer-graph snapshots."""
    samples: dict[str, list[int]] = defaultdict(list)
    # Baselines use weekly snapshots, starting the baseline weeks before monitoring begins.
    week = cfg.fan_window_days * DAY
    lo = world.monitor_start_ts - BASELINE_WEEKS * week
    # One snapshot per week, from the baseline start until the period ends.
    while lo < world.end_ts:
        # Each snapshot is the transfer graph for that week alone, with no amount floor.
        g = transfer_graph(world, lo, lo + week - 1, 1)
        # Sorted nodes keep the sample order, and so the medians, fixed.
        for node in sorted(g.nodes):
            # Only customers get degree baselines; external parties are not thresholded.
            cust = world.customers.get(node)
            if cust is None:
                continue
            # In-degree and out-degree are sampled separately, as they are separate kinds.
            if g.in_degree(node):
                samples[f"{cust.kind}:in"].append(g.in_degree(node))
            if g.out_degree(node):
                samples[f"{cust.kind}:out"].append(g.out_degree(node))
        lo += week
    out: dict[str, int] = {}
    for key, vals in sorted(samples.items()):
        # Robust centre and spread: the median is the centre and the MAD (median absolute deviation)
        # is the spread, which outliers barely move.
        med = statistics.median(vals)
        # Scale the MAD to a standard deviation so the k multiplier has its usual meaning.
        mad = statistics.median(abs(v - med) for v in vals) * MAD_TO_SIGMA
        # Floors stop small-sample noise from pushing a threshold below the configured minimum.
        floor = cfg.fan_in_min if key.endswith(":in") else cfg.fan_out_min
        # Threshold: median plus k robust sigmas, rounded up to a whole count.
        out[key] = max(floor, math.ceil(med + cfg.degree_mad_k * mad))
    return out


def _first_seen(world: World, acts: dict[str, Activity]) -> dict[tuple[str, str], int]:
    """First timestamp each (customer, counterparty) pair transacted."""
    # Record the first time each (customer, counterparty) pair appears; a counterparty is judged new
    # against this.
    seen: dict[tuple[str, str], int] = {}
    for cid, act in acts.items():
        for t in act.inflows:
            seen.setdefault((cid, world.party(t.src)), t.ts)
        for t in act.outflows:
            seen.setdefault((cid, world.party(t.dst)), t.ts)
    return seen


def detect_fans(
    world: World,
    acts: dict[str, Activity],
    cfg: GraphConfig,
    version: str,
    thresholds: dict[str, int] | None = None,
) -> list[Alert]:
    """GRA-FIN / GRA-FOUT: many mostly-new counterparties in a window, with pass-through.

    ``thresholds`` defaults to ``degree_baselines(world, cfg)``.
    """
    # Use the supplied thresholds, or derive them from the graph when the caller has none.
    thresholds = thresholds or degree_baselines(world, cfg)
    # The first-seen index is built once and shared by every customer.
    seen = _first_seen(world, acts)
    win = cfg.fan_window_days * DAY
    pt = cfg.pass_through_hours * HOUR
    out: list[Alert] = []
    for cid, act in acts.items():
        kind = world.customers[cid].kind
        for direction in ("in", "out"):
            # Inflows and outflows are scanned separately, because fan-in and fan-out are different
            # rules.
            flows = act.inflows if direction == "in" else act.outflows
            # Cash has no counterparty, so cash legs are left out of the counts.
            flows = [t for t in flows if CASH not in (t.src, t.dst)]
            # The threshold for this customer kind and direction.
            thr = thresholds.get(
                f"{kind}:{direction}", cfg.fan_in_min if direction == "in" else cfg.fan_out_min
            )
            # Distinct counterparties currently inside the window, with a count for each.
            cps: Counter[str] = Counter()
            hits: list[Hit] = []
            j = 0
            for i, t in enumerate(flows):
                # Add the newest flow's counterparty to the window.
                cps[world.party(t.src if direction == "in" else t.dst)] += 1
                # Drop flows that have left the window from the left; a count reaching zero is
                # removed, so len(cps) stays the distinct count.
                while t.ts - flows[j].ts > win:
                    old = world.party(flows[j].src if direction == "in" else flows[j].dst)
                    cps[old] -= 1
                    if cps[old] == 0:
                        del cps[old]
                    j += 1
                # Test only once the window holds enough distinct counterparties, and only for
                # activity inside the monitoring period.
                if len(cps) < thr or t.ts < world.monitor_start_ts:
                    continue
                # The window starts at its oldest flow; a counterparty is new if it was first seen
                # inside this window.
                w_lo = flows[j].ts
                # Share of the window's counterparties that were first seen inside it.
                new = sum(1 for cp in cps if seen[(cid, cp)] >= w_lo) / len(cps)
                if new < cfg.new_counterparty_share:
                    continue
                # The flows in the window are the evidence for the alert.
                window = flows[j : i + 1]
                vol = sum(x.amount_cents for x in window)
                if vol < cfg.fan_min_volume_cents:
                    continue
                # Pass-through: how much of the window's money left again, for fan-in, or arrived,
                # for fan-out, within the pass-through allowance.
                if direction == "in":
                    # For fan-in, money sent out from the window start through the allowance after
                    # the last inflow.
                    other = act.out_sum(w_lo, t.ts + pt)
                else:
                    # For fan-out, money received before the window ends, within the allowance
                    # before the first outflow.
                    other = act.in_sum(w_lo - pt, t.ts)
                # Pass-through ratio: the other side's volume over this side's volume.
                ratio = other / vol if vol else 0.0
                if ratio < cfg.pass_through_ratio:
                    continue
                hits.append(
                    (
                        window,
                        {
                            "distinct_counterparties": len(cps),
                            "new_share": round(new, 2),
                            "pass_through": round(min(ratio, FAN_MAX_PASS_THROUGH), 2),
                            "volume_cents": vol,
                            "threshold": thr,
                        },
                    )
                )
            # The rule id follows the direction.
            rule = "GRA-FIN" if direction == "in" else "GRA-FOUT"
            for ep, infos in episodes(hits):
                # Strongest hit: most distinct counterparties, then the largest volume.
                best = max(infos, key=lambda x: (x["distinct_counterparties"], x["volume_cents"]))
                # Score: a base, plus a bump per distinct counterparty above the threshold, plus a
                # bump for pass-through above the ratio.
                score = (
                    FAN_BASE
                    + FAN_PER_EXTRA_COUNTERPARTY * (best["distinct_counterparties"] - thr)
                    + FAN_PASS_THROUGH_WEIGHT
                    * min(1.0, best["pass_through"] - cfg.pass_through_ratio)
                )
                out.append(make_alert(rule, "fan", cid, ep, score, dict(best), version))
    return out


# ------------------------------------------------------------------------- communities


@dataclass(frozen=True, slots=True)
class Network:
    """A flagged community: all members, the independently alerted ones, intra-group flow."""

    network_id: str
    members: tuple[str, ...]
    alerted: tuple[str, ...]
    intra_flow_cents: int


def detect_communities(
    world: World, alerted: set[str], cfg: GraphConfig, version: str
) -> tuple[list[Alert], list[Network]]:
    """GRA-COM: Louvain communities on the flow graph around alerted customers.

    A community with enough members and enough independently alerted members becomes a
    ``Network``; every member with intra-network flow gets a GRA-COM alert.
    """
    # Aggregate value since monitoring started, keeping only pairs that touch an alerted customer,
    # so the graph stays near the alerts.
    pair: dict[tuple[str, str], int] = defaultdict(int)
    pair_txns: dict[tuple[str, str], list[Txn]] = defaultdict(list)
    for t in world.txns:
        if t.ts < world.monitor_start_ts:
            continue
        # Map accounts to their owners; external or cash legs have no customer to link.
        u, v = world.owner(t.src), world.owner(t.dst)
        if u is None or v is None or u == v or (u not in alerted and v not in alerted):
            continue
        # Undirected pair key: direction does not matter for community membership.
        key = (min(u, v), max(u, v))
        pair[key] += t.amount_cents
        pair_txns[key].append(t)
    # Louvain runs on integer node labels: its internal sets then iterate in an order
    # that does not depend on PYTHONHASHSEED, so communities are reproducible.
    # Drop edges below the minimum weight, so communities reflect material flows.
    kept = sorted((k, w) for k, w in pair.items() if w >= cfg.community_min_edge_cents)
    names = sorted({n for (u, v), _ in kept for n in (u, v)})
    idx = {n: i for i, n in enumerate(names)}
    g = nx.Graph()
    for (u, v), w in kept:
        g.add_edge(idx[u], idx[v], weight=w / CENTS_PER_DOLLAR)
    # With no edges there are no communities; this skips the Louvain call on an empty graph.
    if g.number_of_edges() == 0:
        return [], []
    # Louvain with a fixed seed, weighted by dollars.
    comms = nx.community.louvain_communities(g, weight="weight", seed=cfg.community_seed)
    flagged = []
    for c in comms:
        members = tuple(sorted(names[i] for i in c))
        hit = tuple(m for m in members if m in alerted)
        # A community is flagged only if it is big enough and has enough independently alerted
        # members.
        if len(members) >= cfg.community_min_size and len(hit) >= cfg.community_min_alerted:
            flagged.append((members, hit))
    # Sort flagged communities, so network numbers are stable from run to run.
    flagged.sort()
    networks: list[Network] = []
    alerts: list[Alert] = []
    for k, (members, hit) in enumerate(flagged, start=1):
        mset = set(members)
        # A network's flow is the money moved among its own members only.
        intra = [
            t
            for key, ts in sorted(pair_txns.items())
            if key[0] in mset and key[1] in mset
            for t in ts
        ]
        flow = sum(t.amount_cents for t in intra)
        net = Network(f"NET-{k:03d}", members, hit, flow)
        networks.append(net)
        # Every member with some intra-network flow gets its own GRA-COM alert.
        for m in members:
            # Each member's own intra-network transfers, in time order, as its evidence.
            mine = sorted(
                (t for t in intra if m in (world.owner(t.src), world.owner(t.dst))),
                key=lambda t: (t.ts, t.txn_id),
            )
            if not mine:
                continue
            # Score: a base, plus a bump per extra alerted member and per member above the base
            # size, both capped.
            score = (
                COM_BASE
                + COM_PER_ALERTED_MEMBER * min(COM_MAX_INCREMENTS, len(hit) - 1)
                + COM_PER_EXTRA_MEMBER * min(COM_MAX_INCREMENTS, len(members) - COM_BASE_SIZE)
            )
            details = {
                "network_id": net.network_id,
                "size": len(members),
                "alerted_members": len(hit),
                "intra_flow_cents": flow,
            }
            alerts.append(make_alert("GRA-COM", "network", m, mine, score, details, version))
    return alerts, networks
