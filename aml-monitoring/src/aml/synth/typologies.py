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
    # Classic bursts are 6-12 deposits in one to two and a half weeks, just under $10,000; low-and-
    # slow spreads 5-7 deposits over about a month, further below.
    variant = variant or ("low_and_slow" if rng.random() < LOW_AND_SLOW_P else "classic")
    # Subjects draw their KYC rating from the bad-actor weights, so most are rated higher risk.
    kyc = b.draw_kyc(BAD_ACTOR_KYC_WEIGHTS)
    # Most subjects are individuals in cash-heavy occupations; the rest are cash-heavy businesses.
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
    # Start after the lookback, leaving room for the whole series plus the possible wire-out.
    day0 = day0 if day0 is not None else rng.randint(MONITOR_START_DAY + 1, PERIOD_DAYS - span - 12)
    # Spread the deposits over two to four branches, so no single branch sees the whole pattern.
    branches = rng.sample(BRANCHES, rng.randint(2, 4))
    total, last = 0, 0
    for i in range(n):
        # Space deposits evenly across the span, with a small random day offset.
        day = day0 + (i * span) // n + rng.randint(0, 1)
        if variant == "classic":
            if rng.random() < ROUND_HUNDREDS_P:
                # Round-hundred amounts ($8,500 to $9,900) are a common structuring pattern.
                amt = rng.randint(85, 99) * 100_00  # round hundreds
            # Other deposits are random dollar amounts that sit just under the reporting threshold.
            else:
                amt = rng.randint(8_200, 9_950) * 100
        else:
            amt = rng.randint(6_000, 8_400) * 100
        # Each deposit is cash into the subject's account, labelled for ground truth; detectors
        # never read the label.
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
    # Some subjects wire most of the cash onward a few days after the last deposit.
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
    # Register the subject as ground truth; the instance is what evaluation scores detection
    # against.
    b.instance("structuring", [cid], variant)
    return cid


def inject_round_trip(
    b: WorldBuilder, variant: str | None = None, day0: int | None = None
) -> list[str]:
    """Shell companies pass value around a loop (optionally via offshore) back to the start."""
    rng = b.rng
    # Domestic loops use three shells; offshore loops use two shells plus one external hop.
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
    # Money moves around the ring in list order, each account paying the next.
    path = [b.acct(s) for s in shells]
    off_country = rng.choice(ELEVATED)
    # The offshore variant closes the ring through an external counterparty in an elevated country.
    if variant == "offshore_loop":
        path.append(b.ext("OFFSHORE"))
    rounds = rng.randint(1, 3)
    day0 = (
        day0
        if day0 is not None
        else rng.randint(MONITOR_START_DAY + 1, PERIOD_DAYS - ROUND_TRIP_SPACING_DAYS * rounds - 6)
    )
    # Each round starts a fixed spacing after the previous one; day0 keeps every round inside the
    # period.
    for r in range(rounds):
        amt = rng.randint(40_000, 250_000) * 100
        t = work_ts(rng, day0 + r * ROUND_TRIP_SPACING_DAYS)
        for k in range(len(path)):
            src, dst = path[k], path[(k + 1) % len(path)]
            # A leg touching an external account is priced at the elevated country; internal legs
            # are domestic.
            country = off_country if (is_external(src) or is_external(dst)) else "US"
            b.txn(t, "wire", src, dst, amt, country=country, label="round_tripping")
            # Each hop skims a few percent, as fees or leakage would, so the loop does not conserve
            # value exactly.
            amt = int(amt * rng.uniform(0.94, 0.995))
            t += rng.randint(12, 72) * HOUR
    # Register the shells as the instance members; the external hop is not a member.
    b.instance("round_tripping", shells, variant)
    return shells


def inject_mule_ring(b: WorldBuilder, day0: int | None = None) -> list[str]:
    """Victim funds land on recruited mules, are forwarded to a collector, then wired out."""
    rng = b.rng
    # Victim payments land on the mules; day0 leaves room for the forward and payout legs.
    day0 = day0 if day0 is not None else rng.randint(MONITOR_START_DAY + 4, PERIOD_DAYS - 8)
    # The collector is either an individual or a shell business, both drawn from bad-actor KYC.
    if rng.random() < MULE_COLLECTOR_INDIVIDUAL_P:
        collector = gen_individual(
            b, occupation="gig_worker", light=True, kyc=b.draw_kyc(BAD_ACTOR_KYC_WEIGHTS)
        )
    else:
        collector = gen_business(
            b, "general_trading", light=True, kyc=b.draw_kyc(BAD_ACTOR_KYC_WEIGHTS)
        )
    # Recruited mules are ordinary-looking individuals, onboarded shortly before the ring starts.
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
    # Track the running totals: collected is what actually reached the collector.
    collected, last_fwd = 0, 0
    for m in mules:
        m_acct = b.acct(m)
        received, last_in = 0, 0
        # Each mule takes several victim payments over three days, during working hours.
        for _ in range(rng.randint(3, 5)):
            ts = ts_at(day0 + rng.randint(0, 2), rng.randint(7, 21), rng.randrange(60))
            amt = rng.randint(1_200, 4_800) * 100
            b.txn(ts, rng.choice(("p2p", "ach")), b.ext("VIC"), m_acct, amt, label="mule_network")
            received += amt
            last_in = max(last_in, ts)
        # Mules forward most of what they received after a short delay, keeping a small cut.
        fwd = int(received * rng.uniform(0.85, 0.95))
        ts = last_in + rng.randint(4, 30) * HOUR
        # Forward either in one transfer or in two halves, so the outgoing pattern varies.
        parts = [fwd] if rng.random() < MULE_SINGLE_FORWARD_P else [fwd // 2, fwd - fwd // 2]
        for p in parts:
            b.txn(ts, rng.choice(("p2p", "wire")), m_acct, c_acct, p, label="mule_network")
            ts += rng.randint(1, 5) * HOUR
        collected += fwd
        # Remember when the last forward landed; the payout starts after it.
        last_fwd = max(last_fwd, ts)
    n_ben = rng.randint(3, 6)
    weights = [rng.uniform(0.5, 1.5) for _ in range(n_ben)]
    # The collector pays out 95% of what it collected and keeps the rest as margin.
    out_total = int(collected * MULE_PAYOUT_SHARE)
    ts = last_fwd + rng.randint(6, 30) * HOUR
    # Split the payout over several beneficiaries using random weights, all in elevated countries.
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
    # Ground truth covers the collector and every mule.
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
        # Inbound amount: $25,000 to $150,000, large enough to matter to a monitor.
        amt = rng.randint(25_000, 150_000) * 100
        # Some inbound wires come from medium-risk countries.
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
        # Outbound is 85-98% of the inbound amount; the rest is kept back.
        out = int(amt * rng.uniform(0.85, 0.98))
        # Fast moves funds out within 36 hours; slow holds them for 56 to 90 hours.
        delay = rng.randint(2, 36) if variant == "fast" else rng.randint(56, 90)
        # Some episodes pay out in one wire, others in two halves.
        parts = [out] if rng.random() < SINGLE_PAYOUT_P else [out // 2, out - out // 2]
        t = t0 + delay * HOUR
        for p in parts:
            # Each outbound leg may independently go to an elevated jurisdiction.
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
    # The high-tier variant layers through one high-risk country plus elevated ones; medium-only
    # stays in medium-risk countries.
    variant = variant or ("medium_only" if rng.random() < MEDIUM_ONLY_LAYERING_P else "high_tier")
    cid = gen_business(
        b,
        rng.choice(("general_trading", "consulting")),
        light=True,
        kyc=b.draw_kyc(BAD_ACTOR_KYC_WEIGHTS),
    )
    a = b.acct(cid)
    day0 = day0 if day0 is not None else rng.randint(MONITOR_START_DAY + 1, PERIOD_DAYS - 24)
    # Pool the inflows over the window; the total is between $150,000 and $600,000.
    total_in = rng.randint(150_000, 600_000) * 100
    n_in = rng.randint(3, 6)
    # Fund from several distinct funders, two days apart, so the pooling looks gradual.
    for k in range(n_in):
        b.txn(
            work_ts(rng, day0 + k * 2),
            rng.choice(("ach", "wire")),
            b.ext("FUNDER"),
            a,
            total_in // n_in,
            label="high_risk_layering",
        )
    # High-tier layering sends to one high-risk country plus two or three elevated ones.
    if variant == "high_tier":
        countries = rng.sample(BY_TIER["high"], 1) + rng.sample(ELEVATED, rng.randint(2, 3))
    else:
        countries = rng.sample(BY_TIER["medium"], 2)
    n_out = rng.randint(4, 7)
    # The outflow budget is 90-97% of pooled inflows; the remainder is retained.
    budget = int(total_in * rng.uniform(0.9, 0.97))
    # Layer-out wires are staggered every two days after funding begins.
    for k in range(n_out):
        # Round each outbound wire down to a multiple of $5,000, with a floor of one such unit.
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
