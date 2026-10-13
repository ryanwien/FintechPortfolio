"""Injectors for the five labelled AML typologies.

Each injector creates its subject(s) through the normal population generators (so bad
actors also have ordinary history), posts the suspicious transactions with
``label=<typology>`` and registers a ``TypologyInstance`` as ground truth. Detectors
never read the label. ``variant`` / ``day0`` can be pinned for tests; otherwise they are
drawn from the builder's RNG.
"""

from __future__ import annotations

from typing import Final

from ..model import CASH, DAY, HOUR, MONITOR_START_DAY, PERIOD_DAYS, is_external, ts_at
from .builder import WorldBuilder, work_ts
from .catalog import BAD_ACTOR_KYC_WEIGHTS, BRANCHES, BY_TIER, ELEVATED, MULE_RECRUIT_KYC_WEIGHTS
from .population import gen_business, gen_individual

LOW_AND_SLOW_P: Final = 0.25
INDIVIDUAL_STRUCTURER_P: Final = 0.7
ROUND_HUNDREDS_P: Final = 0.5
STRUCTURING_ONWARD_WIRE_P: Final = 0.5
MULE_SINGLE_FORWARD_P: Final = 0.6
MULE_COLLECTOR_INDIVIDUAL_P: Final = 0.5
MULE_PAYOUT_SHARE: Final = 0.95
SLOW_MOVER_P: Final = 0.25
FOREIGN_ORIGIN_P: Final = 0.4
SINGLE_PAYOUT_P: Final = 0.5
FOREIGN_DESTINATION_P: Final = 0.5
MEDIUM_ONLY_LAYERING_P: Final = 0.25
ROUND_TRIP_SPACING_DAYS: Final = 14
RAPID_EPISODE_SPACING_DAYS: Final = 12
LAYER_ROUNDING_CENTS: Final = 5_000_00


def inject_structuring(b: WorldBuilder, variant: str | None = None, day0: int | None = None) -> str:
    """Series of sub-CTR cash deposits across branches, sometimes wired on afterwards."""
    rng = b.rng
    variant = variant or ("low_and_slow" if rng.random() < LOW_AND_SLOW_P else "classic")
    kyc = b.draw_kyc(BAD_ACTOR_KYC_WEIGHTS)
    if rng.random() < INDIVIDUAL_STRUCTURER_P:
        occ = rng.choice(("contractor", "restaurant_server", "gig_worker", "truck_driver"))
        cid = gen_individual(b, occupation=occ, kyc=kyc)
    else:
        cid = gen_business(b, "general_contractor", kyc=kyc, light=True)
    a = b.acct(cid)
    if variant == "classic":
        n, span = rng.randint(6, 12), rng.randint(7, 18)
    else:
        n, span = rng.randint(5, 7), rng.randint(28, 35)
    day0 = day0 if day0 is not None else rng.randint(MONITOR_START_DAY + 1, PERIOD_DAYS - span - 12)
    branches = rng.sample(BRANCHES, rng.randint(2, 4))
    total, last = 0, 0
    for i in range(n):
        day = day0 + (i * span) // n + rng.randint(0, 1)
        if variant == "classic":
            if rng.random() < ROUND_HUNDREDS_P:
                amt = rng.randint(85, 99) * 100_00  # round hundreds
            else:
                amt = rng.randint(8_200, 9_950) * 100
        else:
            amt = rng.randint(6_000, 8_400) * 100
        t = b.txn(
            work_ts(rng, day, 9, 17),
            "cash_deposit",
            CASH,
            a,
            amt,
            branch=branches[i % len(branches)],
            label="structuring",
        )
        total += amt
        last = max(last, t.ts)
    if rng.random() < STRUCTURING_ONWARD_WIRE_P:
        b.txn(
            last + rng.randint(3, 10) * DAY,
            "wire",
            a,
            b.ext("BENE"),
            int(total * rng.uniform(0.7, 0.9)),
            country=rng.choice(("US", *BY_TIER["medium"])),
            label="structuring",
        )
    b.instance("structuring", [cid], variant)
    return cid


def inject_round_trip(
    b: WorldBuilder, variant: str | None = None, day0: int | None = None
) -> list[str]:
    """Shell companies pass value around a loop (optionally via offshore) back to the start."""
    rng = b.rng
    variant = variant or rng.choice(("domestic_loop", "offshore_loop"))
    n_internal = 3 if variant == "domestic_loop" else 2
    shells = [
        gen_business(
            b,
            rng.choice(("consulting", "general_trading")),
            light=True,
            kyc=b.draw_kyc(BAD_ACTOR_KYC_WEIGHTS),
        )
        for _ in range(n_internal)
    ]
    path = [b.acct(s) for s in shells]
    off_country = rng.choice(ELEVATED)
    if variant == "offshore_loop":
        path.append(b.ext("OFFSHORE"))
    rounds = rng.randint(1, 3)
    day0 = (
        day0
        if day0 is not None
        else rng.randint(MONITOR_START_DAY + 1, PERIOD_DAYS - ROUND_TRIP_SPACING_DAYS * rounds - 6)
    )
    for r in range(rounds):
        amt = rng.randint(40_000, 250_000) * 100
        t = work_ts(rng, day0 + r * ROUND_TRIP_SPACING_DAYS)
        for k in range(len(path)):
            src, dst = path[k], path[(k + 1) % len(path)]
            country = off_country if (is_external(src) or is_external(dst)) else "US"
            b.txn(t, "wire", src, dst, amt, country=country, label="round_tripping")
            amt = int(amt * rng.uniform(0.94, 0.995))
            t += rng.randint(12, 72) * HOUR
    b.instance("round_tripping", shells, variant)
    return shells


def inject_mule_ring(b: WorldBuilder, day0: int | None = None) -> list[str]:
    """Victim funds land on recruited mules, are forwarded to a collector, then wired out."""
    rng = b.rng
    day0 = day0 if day0 is not None else rng.randint(MONITOR_START_DAY + 4, PERIOD_DAYS - 8)
    if rng.random() < MULE_COLLECTOR_INDIVIDUAL_P:
        collector = gen_individual(
            b, occupation="gig_worker", light=True, kyc=b.draw_kyc(BAD_ACTOR_KYC_WEIGHTS)
        )
    else:
        collector = gen_business(
            b, "general_trading", light=True, kyc=b.draw_kyc(BAD_ACTOR_KYC_WEIGHTS)
        )
    mules = [
        gen_individual(
            b,
            occupation=rng.choice(("student", "gig_worker", "retail_associate")),
            kyc=b.draw_kyc(MULE_RECRUIT_KYC_WEIGHTS),
            onboarded_day=rng.randint(5, day0 - 3),
            light=True,
        )
        for _ in range(rng.randint(5, 8))
    ]
    c_acct = b.acct(collector)
    collected, last_fwd = 0, 0
    for m in mules:
        m_acct = b.acct(m)
        received, last_in = 0, 0
        for _ in range(rng.randint(3, 5)):
            ts = ts_at(day0 + rng.randint(0, 2), rng.randint(7, 21), rng.randrange(60))
            amt = rng.randint(1_200, 4_800) * 100
            b.txn(ts, rng.choice(("p2p", "ach")), b.ext("VIC"), m_acct, amt, label="mule_network")
            received += amt
            last_in = max(last_in, ts)
        fwd = int(received * rng.uniform(0.85, 0.95))
        ts = last_in + rng.randint(4, 30) * HOUR
        parts = [fwd] if rng.random() < MULE_SINGLE_FORWARD_P else [fwd // 2, fwd - fwd // 2]
        for p in parts:
            b.txn(ts, rng.choice(("p2p", "wire")), m_acct, c_acct, p, label="mule_network")
            ts += rng.randint(1, 5) * HOUR
        collected += fwd
        last_fwd = max(last_fwd, ts)
    n_ben = rng.randint(3, 6)
    weights = [rng.uniform(0.5, 1.5) for _ in range(n_ben)]
    out_total = int(collected * MULE_PAYOUT_SHARE)
    ts = last_fwd + rng.randint(6, 30) * HOUR
    for w in weights:
        b.txn(
            ts,
            "wire",
            c_acct,
            b.ext("MULEBEN"),
            int(out_total * w / sum(weights)),
            country=rng.choice(ELEVATED),
            label="mule_network",
        )
        ts += rng.randint(1, 6) * HOUR
    members = [collector, *mules]
    b.instance("mule_network", members, f"{len(mules)}_mules")
    return members


def inject_rapid_movement(
    b: WorldBuilder, variant: str | None = None, day0: int | None = None
) -> str:
    """Large inbound wires sent onward within hours ("fast") or a few days ("slow")."""
    rng = b.rng
    variant = variant or ("slow" if rng.random() < SLOW_MOVER_P else "fast")
    occ = rng.choice(("contractor", "accountant", "retail_associate", "gig_worker"))
    cid = gen_individual(b, occupation=occ, kyc=b.draw_kyc(BAD_ACTOR_KYC_WEIGHTS))
    a = b.acct(cid)
    n_episodes = rng.randint(1, 3)
    day0 = (
        day0
        if day0 is not None
        else rng.randint(
            MONITOR_START_DAY + 1, PERIOD_DAYS - RAPID_EPISODE_SPACING_DAYS * n_episodes - 5
        )
    )
    for e in range(n_episodes):
        t0 = work_ts(rng, day0 + e * RAPID_EPISODE_SPACING_DAYS)
        amt = rng.randint(25_000, 150_000) * 100
        foreign = rng.random() < FOREIGN_ORIGIN_P
        b.txn(
            t0,
            "wire",
            b.ext("ORIG"),
            a,
            amt,
            country=rng.choice(BY_TIER["medium"]) if foreign else "US",
            label="rapid_movement",
        )
        out = int(amt * rng.uniform(0.85, 0.98))
        delay = rng.randint(2, 36) if variant == "fast" else rng.randint(56, 90)
        parts = [out] if rng.random() < SINGLE_PAYOUT_P else [out // 2, out - out // 2]
        t = t0 + delay * HOUR
        for p in parts:
            dest_foreign = rng.random() < FOREIGN_DESTINATION_P
            b.txn(
                t,
                "wire",
                a,
                b.ext("BENE"),
                p,
                country=rng.choice(ELEVATED) if dest_foreign else "US",
                label="rapid_movement",
            )
            t += rng.randint(1, 4) * HOUR
    b.instance("rapid_movement", [cid], variant)
    return cid


def inject_hr_layering(b: WorldBuilder, variant: str | None = None, day0: int | None = None) -> str:
    """Funds pooled from a few funders, then layered out in round wires to risky jurisdictions."""
    rng = b.rng
    variant = variant or ("medium_only" if rng.random() < MEDIUM_ONLY_LAYERING_P else "high_tier")
    cid = gen_business(
        b,
        rng.choice(("general_trading", "consulting")),
        light=True,
        kyc=b.draw_kyc(BAD_ACTOR_KYC_WEIGHTS),
    )
    a = b.acct(cid)
    day0 = day0 if day0 is not None else rng.randint(MONITOR_START_DAY + 1, PERIOD_DAYS - 24)
    total_in = rng.randint(150_000, 600_000) * 100
    n_in = rng.randint(3, 6)
    for k in range(n_in):
        b.txn(
            work_ts(rng, day0 + k * 2),
            rng.choice(("ach", "wire")),
            b.ext("FUNDER"),
            a,
            total_in // n_in,
            label="high_risk_layering",
        )
    if variant == "high_tier":
        countries = rng.sample(BY_TIER["high"], 1) + rng.sample(ELEVATED, rng.randint(2, 3))
    else:
        countries = rng.sample(BY_TIER["medium"], 2)
    n_out = rng.randint(4, 7)
    budget = int(total_in * rng.uniform(0.9, 0.97))
    for k in range(n_out):
        amt = max(
            LAYER_ROUNDING_CENTS, (budget // n_out) // LAYER_ROUNDING_CENTS * LAYER_ROUNDING_CENTS
        )
        b.txn(
            work_ts(rng, day0 + 3 + k * 2 + rng.randint(0, 1)),
            "wire",
            a,
            b.ext("LAYER"),
            amt,
            country=countries[k % len(countries)],
            label="high_risk_layering",
        )
    b.instance("high_risk_layering", [cid], variant)
    return cid
