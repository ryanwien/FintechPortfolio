"""Each scenario fires on its typology and stays quiet on its benign look-alike."""

from dataclasses import replace

from aml.activity import index_activity
from aml.model import CASH, ts_at
from aml.rules import geography, rapid_movement, structuring, velocity
from aml.synth import (
    WorldBuilder,
    add_remittance,
    gen_business,
    gen_individual,
    inject_hr_layering,
    inject_rapid_movement,
    inject_structuring,
)


def _customers(alerts):
    return {a.customer_id for a in alerts}


def test_structuring_fires_on_structurer_not_cash_business(cfg):
    b = WorldBuilder(1)
    structurer = inject_structuring(b, variant="classic", day0=40)
    laundromat = gen_business(b, "laundromat", scale=1.0)
    store = gen_business(b, "convenience_store", scale=1.0)
    w = b.build()
    hit = _customers(structuring(w, index_activity(w), cfg.structuring))
    assert structurer in hit
    assert laundromat not in hit
    assert store not in hit


def test_structuring_same_day_split_across_branches(cfg):
    b = WorldBuilder(2)
    cid = gen_individual(b, occupation="teacher", light=True)
    a = b.acct(cid)
    b.txn(ts_at(50, 10), "cash_deposit", CASH, a, 6_000_00, branch="BR01")
    b.txn(ts_at(50, 15), "cash_deposit", CASH, a, 5_500_00, branch="BR07")
    w = b.build()
    alerts = structuring(w, index_activity(w), cfg.structuring)
    assert [a.customer_id for a in alerts] == [cid]
    assert alerts[0].details["patterns"] == ["same_day_split"]


def test_velocity_fires_on_profile_breach_not_on_busy_business(cfg):
    b = WorldBuilder(3)
    mover = inject_rapid_movement(b, variant="fast", day0=45)
    staffing = gen_business(b, "staffing_agency")
    w = b.build()
    hit = _customers(velocity(w, index_activity(w), cfg.velocity))
    assert mover in hit
    assert staffing not in hit
    assert w.customers[staffing].expected_monthly_cents > 25_000_00  # busy, but as declared


def test_rapid_movement_fires_on_pass_through_not_escrow(cfg):
    b = WorldBuilder(4)
    mover = inject_rapid_movement(b, variant="fast", day0=45)
    escrow = gen_business(b, "title_escrow")
    w = b.build()
    hit = _customers(rapid_movement(w, index_activity(w), cfg.rapid_movement))
    assert mover in hit
    assert escrow not in hit


def test_rapid_movement_slow_variant_is_missed_until_window_widens(cfg):
    b = WorldBuilder(5)
    slow = inject_rapid_movement(b, variant="slow", day0=45)
    w = b.build()
    acts = index_activity(w)
    assert slow not in _customers(rapid_movement(w, acts, cfg.rapid_movement))
    wide = replace(cfg.rapid_movement, out_window_hours=96)
    assert slow in _customers(rapid_movement(w, acts, wide))


def test_geography_fires_on_layering_not_importer_or_remitter(cfg):
    b = WorldBuilder(6)
    layering = inject_hr_layering(b, variant="high_tier", day0=40)
    importer = gen_business(b, "import_export", importer_countries=1)
    remitter = gen_individual(b, occupation="nurse")
    add_remittance(b, remitter)
    w = b.build()
    hit = _customers(geography(w, index_activity(w), cfg.geography))
    assert layering in hit
    assert importer not in hit
    assert remitter not in hit


def test_alerts_carry_versioned_config(cfg):
    b = WorldBuilder(7)
    inject_structuring(b, variant="classic", day0=40)
    w = b.build()
    alerts = structuring(w, index_activity(w), cfg.structuring, cfg.tag())
    assert alerts
    assert all(a.rule_version == cfg.tag() for a in alerts)
    tweaked = replace(cfg, structuring=replace(cfg.structuring, min_count=4))
    assert tweaked.fingerprint() != cfg.fingerprint()
