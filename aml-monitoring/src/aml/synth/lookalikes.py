"""Benign look-alikes layered on the background population.

Legitimate behaviour that resembles a typology: remittances and family wires to
higher-risk jurisdictions (geography), large one-off life events (velocity, rapid
movement) and intercompany groups whose payments form a static cycle without conserving
value (round-tripping). Each registers its customers in ``WorldBuilder.lookalikes`` so
the evaluation can report where false positives come from. Business-segment look-alikes
live in ``segments``.
"""

from __future__ import annotations

from typing import Final

from ..model import HOUR, MONITOR_START_DAY, PERIOD_DAYS
from .builder import WorldBuilder, work_ts
from .catalog import BY_TIER
from .population import gen_business

MONTH_DAYS: Final = 30
HIGH_TIER_REMITTANCE_P: Final = 0.3
LIFE_EVENTS: Final = ("home_sale", "inheritance", "vehicle_purchase", "bonus_investment")


def add_remittance(b: WorldBuilder, cid: str) -> None:
    """Monthly wires of similar size to one beneficiary abroad."""
    rng = b.rng
    tier = "high" if rng.random() < HIGH_TIER_REMITTANCE_P else "medium"
    country = rng.choice(BY_TIER[tier])
    ben = b.ext("BEN")
    base = rng.randint(150, 1_800) * 100
    for day in range(rng.randint(0, 29), PERIOD_DAYS, MONTH_DAYS):
        b.txn(
            work_ts(rng, day),
            "wire",
            b.acct(cid),
            ben,
            int(base * rng.uniform(0.9, 1.1)),
            country=country,
        )
    b.lookalikes["remitter"].append(cid)


def add_life_event(b: WorldBuilder, cid: str) -> None:
    """A legitimate large one-off movement: the classic source of false positives."""
    rng = b.rng
    a = b.acct(cid)
    day = rng.randint(MONITOR_START_DAY, PERIOD_DAYS - 12)
    t0 = work_ts(rng, day)
    kind = rng.choice(LIFE_EVENTS)
    if kind == "home_sale":
        amt = rng.randint(80_000, 350_000) * 100
        b.txn(t0, "wire", b.ext("TITLE"), a, amt)
        b.txn(
            t0 + rng.randint(24, 144) * HOUR,
            "wire",
            a,
            b.ext("TITLE"),
            int(amt * rng.uniform(0.7, 1.0)),
        )
    elif kind == "inheritance":
        amt = rng.randint(40_000, 150_000) * 100
        b.txn(t0, "wire", b.ext("ESTATE"), a, amt)
        b.txn(
            t0 + rng.randint(72, 600) * HOUR,
            "ach",
            a,
            b.ext("BROKER"),
            int(amt * rng.uniform(0.3, 0.9)),
        )
    elif kind == "vehicle_purchase":
        amt = rng.randint(15_000, 60_000) * 100
        b.txn(t0, "ach", b.ext("BROKER"), a, amt)
        b.txn(
            t0 + rng.randint(4, 40) * HOUR,
            "wire",
            a,
            b.ext("DEALER"),
            int(amt * rng.uniform(0.9, 0.99)),
        )
    else:
        amt = rng.randint(20_000, 80_000) * 100
        b.txn(t0, "ach", b.ext("EMPBONUS"), a, amt)
        b.txn(
            t0 + rng.randint(24, 72) * HOUR,
            "ach",
            a,
            b.ext("BROKER"),
            int(amt * rng.uniform(0.8, 0.95)),
        )
    b.lookalikes["life_event"].append(cid)


def add_family_wire(b: WorldBuilder, cid: str) -> None:
    """One sizeable wire to family in a high-risk jurisdiction."""
    rng = b.rng
    day = rng.randint(MONITOR_START_DAY, PERIOD_DAYS - 2)
    b.txn(
        work_ts(rng, day),
        "wire",
        b.acct(cid),
        b.ext("FAM"),
        rng.randint(18_000, 60_000) * 100,
        country=rng.choice(BY_TIER["high"]),
    )
    b.lookalikes["family_support_wire"].append(cid)


def gen_intercompany_group(b: WorldBuilder) -> list[str]:
    """Three related companies paying each other rent / fees / services monthly.

    The static graph has a cycle, but amounts are unrelated (no value is conserved
    around the loop), so it is a look-alike for round-tripping, not an instance.
    """
    rng = b.rng
    holdco = gen_business(b, "holding_company", kyc="low")
    opco = gen_business(b, "professional_services", kyc="low")
    realty = gen_business(b, "property_management")
    for month in range(0, PERIOD_DAYS, MONTH_DAYS):
        b.txn(
            work_ts(rng, month + rng.randint(1, 3)),
            "ach",
            b.acct(holdco),
            b.acct(opco),
            rng.randint(12_000, 18_000) * 100,
        )
        b.txn(
            work_ts(rng, month + rng.randint(8, 12)),
            "wire",
            b.acct(opco),
            b.acct(realty),
            rng.randint(30_000, 50_000) * 100,
        )
        b.txn(
            work_ts(rng, month + rng.randint(18, 22)),
            "ach",
            b.acct(realty),
            b.acct(holdco),
            rng.randint(5_000, 9_000) * 100,
        )
    members = [holdco, opco, realty]
    b.lookalikes["intercompany_group"].extend(members)
    return members
