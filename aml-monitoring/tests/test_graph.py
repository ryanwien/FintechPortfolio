"""Graph analytics: temporal cycles, fan-in anomalies, community grouping."""

from aml.activity import index_activity
from aml.detect import run_detection
from aml.graph import detect_cycles, detect_fans
from aml.model import HOUR, ts_at
from aml.synth import (
    WorldBuilder,
    gen_business,
    gen_individual,
    gen_intercompany_group,
    inject_mule_ring,
    inject_round_trip,
)


def _cycle_parties(hits):
    return {p for h in hits for p in h.parties}


def test_cycle_detection_finds_round_trip_not_intercompany(cfg):
    b = WorldBuilder(11)
    loop = inject_round_trip(b, variant="domestic_loop", day0=40)
    group = gen_intercompany_group(b)
    w = b.build()
    hits = detect_cycles(w, cfg.graph)
    parties = _cycle_parties(hits)
    assert set(loop) <= parties
    assert not parties & set(group)
    for h in hits:
        assert [t.ts for t in h.txns] == sorted(t.ts for t in h.txns)
        assert h.retention >= cfg.graph.cycle_min_retention ** len(h.txns)


def test_cycle_must_be_time_ordered_and_inside_window(cfg):
    b = WorldBuilder(12)
    a, c, d = (gen_business(b, "consulting", light=True) for _ in range(3))
    acct = b.acct
    # static cycle a->c->d->a, but the c->d hop happens *before* a->c
    b.txn(ts_at(50), "wire", acct(a), acct(c), 100_000_00)
    b.txn(ts_at(49), "wire", acct(c), acct(d), 98_000_00)
    b.txn(ts_at(52), "wire", acct(d), acct(a), 97_000_00)
    # a value-conserving 2-loop that takes longer than the window
    b.txn(ts_at(35), "wire", acct(c), acct(a), 60_000_00)
    b.txn(ts_at(35) + 30 * 24 * HOUR, "wire", acct(a), acct(c), 59_000_00)
    w = b.build()
    assert detect_cycles(w, cfg.graph) == []


def test_fan_in_flags_mule_collector_not_charity_or_landlord(cfg):
    b = WorldBuilder(13)
    ring = inject_mule_ring(b, day0=50)
    charity = gen_business(b, "nonprofit")
    landlord = gen_business(b, "property_management")
    for _ in range(30):  # population for the degree baselines
        gen_individual(b)
    w = b.build()
    alerts = detect_fans(w, index_activity(w), cfg.graph, "test")
    fan_in = {a.customer_id for a in alerts if a.rule_id == "GRA-FIN"}
    collector = ring[0]
    assert collector in fan_in
    assert charity not in fan_in
    assert landlord not in fan_in


def test_community_detection_groups_mule_ring_into_one_network(cfg):
    b = WorldBuilder(14)
    ring = inject_mule_ring(b, day0=50)
    bystanders = [gen_individual(b) for _ in range(40)]
    w = b.build()
    det = run_detection(w, cfg)
    nets = [n for n in det.networks if set(n.members) & set(ring)]
    assert len(nets) == 1
    assert set(ring) <= set(nets[0].members)
    assert not set(nets[0].members) & set(bystanders)
