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
        return self.txns[-1].amount_cents / self.txns[0].amount_cents

    @property
    def hours(self) -> float:
        return (self.txns[-1].ts - self.txns[0].ts) / HOUR


def transfer_graph(world: World, lo: int, hi: int, min_amount: int) -> nx.DiGraph:
    """Party-level graph; each edge keeps its transfers (time-ordered) in ``txns``."""
    g = nx.DiGraph()
    for t in world.txns:
        if t.ts < lo or t.ts > hi or t.amount_cents < min_amount or CASH in (t.src, t.dst):
            continue
        u, v = world.party(t.src), world.party(t.dst)
        if u == v:
            continue
        if not g.has_edge(u, v):
            g.add_edge(u, v, txns=[])
        g[u][v]["txns"].append(t)
    return g


def _extend(
    path: list[Txn], edges: list[list[Txn]], k: int, cfg: GraphConfig, used: set[str]
) -> list[Txn] | None:
    """Depth-first: extend ``path`` with a later, value-conserving transfer on hop ``k``."""
    if k == len(edges):
        return path
    prev = path[-1]
    for t in edges[k]:
        if t.txn_id in used or t.ts <= prev.ts:
            continue
        if t.ts - path[0].ts > cfg.cycle_window_days * DAY:
            break
        r = t.amount_cents / prev.amount_cents
        if cfg.cycle_min_retention <= r <= cfg.cycle_max_retention:
            found = _extend([*path, t], edges, k + 1, cfg, used)
            if found:
                return found
    return None


def _canonical(cycle: list[str]) -> tuple[str, ...]:
    """Rotate a cycle to start at its smallest node (output order must not depend on
    networkx's internal set ordering, which varies with string hash randomisation)."""
    k = cycle.index(min(cycle))
    return tuple(cycle[k:] + cycle[:k])


def detect_cycles(world: World, cfg: GraphConfig) -> list[CycleHit]:
    """Static cycles confirmed as time-ordered, value-conserving chains inside the window.

    Each transaction is used by at most one hit; cycles are visited shortest first in
    canonical order so the result is independent of hash seeds.
    """
    lo = world.monitor_start_ts - cfg.cycle_window_days * DAY
    g = transfer_graph(world, lo, world.end_ts, cfg.cycle_min_amount_cents)
    hits: list[CycleHit] = []
    used: set[str] = set()
    cycles = sorted(
        (
            _canonical(c)
            for c in nx.simple_cycles(g, length_bound=cfg.cycle_max_len)
            if len(c) >= MIN_CYCLE_PARTIES
        ),
        key=lambda c: (len(c), c),
    )
    for cyc in cycles:
        if all(is_external(p) for p in cyc):
            continue
        n = len(cyc)
        for r in range(n):
            nodes = cyc[r:] + cyc[:r]
            edges = [g[nodes[k]][nodes[(k + 1) % n]]["txns"] for k in range(n)]
            for t0 in edges[0]:
                if t0.txn_id in used:
                    continue
                chain = _extend([t0], edges, 1, cfg, used)
                if chain and chain[-1].ts >= world.monitor_start_ts:
                    hits.append(CycleHit(tuple(nodes), tuple(chain)))
                    used.update(t.txn_id for t in chain)
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
        for p in sorted(set(h.parties)):
            if not is_external(p):
                per_customer[p].append((list(h.txns), info))
    out = []
    for cid in sorted(per_customer):
        for ep, infos in episodes(per_customer[cid]):
            best = max(infos, key=lambda i: i["start_cents"])
            ret = min(1.0, best["retention"])
            score = (
                CYC_BASE
                + CYC_PER_LOOP * len(infos)
                + CYC_PER_EXTRA_PARTY * (len(best["path"]) - CYC_MIN_PATH_NODES)
                + CYC_RETENTION_WEIGHT
                * (ret - cfg.cycle_min_retention)
                / (1 - cfg.cycle_min_retention)
            )
            details = dict(best, loops=len(infos))
            out.append(make_alert("GRA-CYC", "round_tripping", cid, ep, score, details, version))
    return out


# ------------------------------------------------------------------------- fan in / out


def degree_baselines(world: World, cfg: GraphConfig) -> dict[str, int]:
    """Robust per-kind degree thresholds from weekly transfer-graph snapshots."""
    samples: dict[str, list[int]] = defaultdict(list)
    week = cfg.fan_window_days * DAY
    lo = world.monitor_start_ts - BASELINE_WEEKS * week
    while lo < world.end_ts:
        g = transfer_graph(world, lo, lo + week - 1, 1)
        for node in sorted(g.nodes):
            cust = world.customers.get(node)
            if cust is None:
                continue
            if g.in_degree(node):
                samples[f"{cust.kind}:in"].append(g.in_degree(node))
            if g.out_degree(node):
                samples[f"{cust.kind}:out"].append(g.out_degree(node))
        lo += week
    out: dict[str, int] = {}
    for key, vals in sorted(samples.items()):
        med = statistics.median(vals)
        mad = statistics.median(abs(v - med) for v in vals) * MAD_TO_SIGMA
        floor = cfg.fan_in_min if key.endswith(":in") else cfg.fan_out_min
        out[key] = max(floor, math.ceil(med + cfg.degree_mad_k * mad))
    return out


def _first_seen(world: World, acts: dict[str, Activity]) -> dict[tuple[str, str], int]:
    """First timestamp each (customer, counterparty) pair transacted."""
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
    thresholds = thresholds or degree_baselines(world, cfg)
    seen = _first_seen(world, acts)
    win = cfg.fan_window_days * DAY
    pt = cfg.pass_through_hours * HOUR
    out: list[Alert] = []
    for cid, act in acts.items():
        kind = world.customers[cid].kind
        for direction in ("in", "out"):
            flows = act.inflows if direction == "in" else act.outflows
            flows = [t for t in flows if CASH not in (t.src, t.dst)]
            thr = thresholds.get(
                f"{kind}:{direction}", cfg.fan_in_min if direction == "in" else cfg.fan_out_min
            )
            cps: Counter[str] = Counter()
            hits: list[Hit] = []
            j = 0
            for i, t in enumerate(flows):
                cps[world.party(t.src if direction == "in" else t.dst)] += 1
                while t.ts - flows[j].ts > win:
                    old = world.party(flows[j].src if direction == "in" else flows[j].dst)
                    cps[old] -= 1
                    if cps[old] == 0:
                        del cps[old]
                    j += 1
                if len(cps) < thr or t.ts < world.monitor_start_ts:
                    continue
                w_lo = flows[j].ts
                new = sum(1 for cp in cps if seen[(cid, cp)] >= w_lo) / len(cps)
                if new < cfg.new_counterparty_share:
                    continue
                window = flows[j : i + 1]
                vol = sum(x.amount_cents for x in window)
                if vol < cfg.fan_min_volume_cents:
                    continue
                if direction == "in":
                    other = act.out_sum(w_lo, t.ts + pt)
                else:
                    other = act.in_sum(w_lo - pt, t.ts)
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
            rule = "GRA-FIN" if direction == "in" else "GRA-FOUT"
            for ep, infos in episodes(hits):
                best = max(infos, key=lambda x: (x["distinct_counterparties"], x["volume_cents"]))
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
    pair: dict[tuple[str, str], int] = defaultdict(int)
    pair_txns: dict[tuple[str, str], list[Txn]] = defaultdict(list)
    for t in world.txns:
        if t.ts < world.monitor_start_ts:
            continue
        u, v = world.owner(t.src), world.owner(t.dst)
        if u is None or v is None or u == v or (u not in alerted and v not in alerted):
            continue
        key = (min(u, v), max(u, v))
        pair[key] += t.amount_cents
        pair_txns[key].append(t)
    # Louvain runs on integer node labels: its internal sets then iterate in an order
    # that does not depend on PYTHONHASHSEED, so communities are reproducible.
    kept = sorted((k, w) for k, w in pair.items() if w >= cfg.community_min_edge_cents)
    names = sorted({n for (u, v), _ in kept for n in (u, v)})
    idx = {n: i for i, n in enumerate(names)}
    g = nx.Graph()
    for (u, v), w in kept:
        g.add_edge(idx[u], idx[v], weight=w / CENTS_PER_DOLLAR)
    if g.number_of_edges() == 0:
        return [], []
    comms = nx.community.louvain_communities(g, weight="weight", seed=cfg.community_seed)
    flagged = []
    for c in comms:
        members = tuple(sorted(names[i] for i in c))
        hit = tuple(m for m in members if m in alerted)
        if len(members) >= cfg.community_min_size and len(hit) >= cfg.community_min_alerted:
            flagged.append((members, hit))
    flagged.sort()
    networks: list[Network] = []
    alerts: list[Alert] = []
    for k, (members, hit) in enumerate(flagged, start=1):
        mset = set(members)
        intra = [
            t
            for key, ts in sorted(pair_txns.items())
            if key[0] in mset and key[1] in mset
            for t in ts
        ]
        flow = sum(t.amount_cents for t in intra)
        net = Network(f"NET-{k:03d}", members, hit, flow)
        networks.append(net)
        for m in members:
            mine = sorted(
                (t for t in intra if m in (world.owner(t.src), world.owner(t.dst))),
                key=lambda t: (t.ts, t.txn_id),
            )
            if not mine:
                continue
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
