"""Background population: individuals (salary, rent, bills, cash, P2P) and businesses.

``gen_individual`` and ``gen_business`` are the building blocks every other generator
uses: look-alikes and typology injectors create their subjects through them so that bad
actors carry a normal-looking history as well as the suspicious activity.
"""

from __future__ import annotations

from typing import Final

from ..model import CASH, PERIOD_DAYS, ts_at
from . import segments
from .builder import BusinessBooks, WorldBuilder, lognormal_cents, work_ts
from .catalog import (
    BUSINESS_SEGMENTS,
    CASH_BUSINESS_KYC_WEIGHTS,
    CASH_SEGMENTS,
    HIGH_SCRUTINY_KYC_WEIGHTS,
    HIGH_SCRUTINY_SEGMENTS,
    INDIVIDUAL_KYC_WEIGHTS,
    OCC_WEIGHTS,
    OCCUPATIONS,
    PROFESSIONAL_SEGMENTS,
    STANDARD_BUSINESS_KYC_WEIGHTS,
)

MONTH_DAYS: Final = 30
BIWEEKLY_PAY_DAYS: Final = 14
PAY_PERIODS_PER_YEAR: Final = 26
MONTHS_PER_YEAR: Final = 12
CASH_DEPOSIT_MONTH_P: Final = 0.15
P2P_FRIENDS: Final = 3


class UnknownSegmentError(ValueError):
    """Raised when a business segment has no activity profile."""


def gen_individual(
    b: WorldBuilder,
    occupation: str | None = None,
    kyc: str | None = None,
    onboarded_day: int = 0,
    light: bool = False,
) -> str:
    """Create an individual with salary or pension; ``light`` skips spending activity."""
    rng = b.rng
    # Occupation sets the salary band; when none is given, one is drawn from the population weights.
    occ = occupation or rng.choices(list(OCC_WEIGHTS), list(OCC_WEIGHTS.values()))[0]
    lo, hi = OCCUPATIONS[occ]
    # Annual salary is drawn from the occupation's band; the monthly figure is one twelfth of it.
    annual = rng.randint(lo, hi) * 100
    monthly = annual // MONTHS_PER_YEAR
    # KYC comes from the individual weights unless the caller pins one.
    kyc = kyc or b.draw_kyc(INDIVIDUAL_KYC_WEIGHTS)
    # The declared profile is the monthly pay scaled by a random factor, so it is only roughly
    # right.
    cid = b.customer("individual", occ, kyc, int(monthly * rng.uniform(0.85, 1.35)), onboarded_day)
    a = b.acct(cid)
    # Pay and rent start from the onboarding day, so nobody has history from before they joined.
    start = onboarded_day
    # Retirees get a fixed monthly pension instead of biweekly pay.
    if occ == "retired":
        for day in range(start + rng.randint(0, 4), PERIOD_DAYS, MONTH_DAYS):
            b.txn(ts_at(day, 6), "ach", "X:PENSION", a, monthly)
    else:
        # One of 300 synthetic employer ids; pay then arrives on a fixed biweekly cadence.
        employer = f"X:EMP{rng.randrange(300):03d}"
        for day in range(start + rng.randint(0, 13), PERIOD_DAYS, BIWEEKLY_PAY_DAYS):
            # Each paycheck is the per-period share of the annual salary, with a small random
            # variation.
            pay = int(annual / PAY_PERIODS_PER_YEAR * rng.uniform(0.97, 1.03))
            b.txn(ts_at(day, 5, rng.randrange(60)), "ach", employer, a, pay)
    # Light individuals stop here: just income, a thin profile used for typology subjects.
    if light:
        return cid
    landlord = f"X:LL{rng.randrange(900):03d}"
    # Rent is 22-36% of monthly pay, a typical household share, paid to a landlord once a month.
    rent = int(monthly * rng.uniform(0.22, 0.36))
    for day in range(start + rng.randint(0, 4), PERIOD_DAYS, MONTH_DAYS):
        b.txn(ts_at(day, 9), "ach", a, landlord, rent)
    for month in range(start, PERIOD_DAYS, MONTH_DAYS):
        # Routine bills: three to six small utility debits a month, each on a random day.
        for _ in range(rng.randint(3, 6)):
            day = month + rng.randrange(MONTH_DAYS)
            b.txn(
                work_ts(rng, day),
                "ach",
                a,
                f"X:UTIL{rng.randrange(30):02d}",
                rng.randint(40, 350) * 100,
            )
        # One to three ATM cash withdrawals a month, in round amounts, at random ATMs.
        for _ in range(rng.randint(1, 3)):
            day = month + rng.randrange(MONTH_DAYS)
            amt = rng.choice((40, 60, 80, 100, 200, 300)) * 100
            b.txn(
                work_ts(rng, day, 7, 22),
                "cash_withdrawal",
                a,
                CASH,
                amt,
                branch=f"ATM{rng.randrange(40):02d}",
            )
        # In about 15% of months there is a cash deposit, log-normal around $400, at the home
        # branch.
        if rng.random() < CASH_DEPOSIT_MONTH_P:
            day = month + rng.randrange(MONTH_DAYS)
            b.txn(
                work_ts(rng, day),
                "cash_deposit",
                CASH,
                a,
                lognormal_cents(rng, 400_00, 0.6),
                branch=b.customers[cid].home_branch,
            )
    return cid


def gen_p2p(b: WorldBuilder, individuals: list[str]) -> None:
    """Small peer-to-peer payments among a few friends per individual."""
    rng = b.rng
    for cid in individuals:
        # Each person has a fixed circle of friends; P2P payments only go within that circle.
        friends = rng.sample(individuals, P2P_FRIENDS)
        for month in range(0, PERIOD_DAYS, MONTH_DAYS):
            # Zero to three P2P payments a month within the circle.
            for _ in range(rng.randint(0, 3)):
                f = rng.choice(friends)
                # Skip self-payments, since the friend circle is drawn from the whole population and
                # may include cid.
                if f == cid:
                    continue
                day = month + rng.randrange(MONTH_DAYS)
                b.txn(
                    work_ts(rng, day, 8, 23),
                    "p2p",
                    b.acct(cid),
                    b.acct(f),
                    rng.randint(15, 350) * 100,
                )


def _default_business_kyc(b: WorldBuilder, segment: str) -> str:
    """KYC draw for a business segment: cash-heavy, high-scrutiny, or standard weights."""
    if segment in CASH_SEGMENTS:
        return b.draw_kyc(CASH_BUSINESS_KYC_WEIGHTS)
    if segment in HIGH_SCRUTINY_SEGMENTS:
        return b.draw_kyc(HIGH_SCRUTINY_KYC_WEIGHTS)
    return b.draw_kyc(STANDARD_BUSINESS_KYC_WEIGHTS)


def gen_business(
    b: WorldBuilder,
    segment: str,
    kyc: str | None = None,
    onboarded_day: int = 0,
    scale: float | None = None,
    light: bool = False,
    importer_countries: int | None = None,
) -> str:
    """Create a business running its segment's activity profile.

    The declared expected monthly inflow is set afterwards from what was actually
    credited, with noise, so the KYC profile is roughly right for benign businesses.
    ``light`` gives a thin shell profile regardless of segment.
    """
    if segment not in BUSINESS_SEGMENTS:
        raise UnknownSegmentError(f"unknown segment {segment}")
    rng = b.rng
    # Default KYC depends on the segment's risk class; a caller can override it.
    if kyc is None:
        kyc = _default_business_kyc(b, segment)
    # Scale multiplies the segment's activity sizes; it is drawn once per business, so a firm is
    # consistently bigger or smaller.
    scale = scale if scale is not None else rng.uniform(0.6, 1.6)
    # Create the customer first; its expected inflow is set at the end, once the credits are known.
    cid = b.customer("business", segment, kyc, 0, onboarded_day)
    books = BusinessBooks(b, cid, onboarded_day)

    # Light shells use one thin profile regardless of segment; otherwise dispatch on segment.
    # Each profile draws its randomness in a fixed order, so the world stays reproducible.
    if light:
        segments.shell_activity(books)
    elif segment in CASH_SEGMENTS:
        segments.cash_intensive(books, segment, scale)
    elif segment in PROFESSIONAL_SEGMENTS:
        segments.professional(books, scale)
    elif segment == "import_export":
        segments.importer(books, scale, importer_countries)
    elif segment == "title_escrow":
        segments.title_escrow(books)
    elif segment == "property_management":
        segments.property_management(books)
    elif segment == "nonprofit":
        segments.nonprofit(books)
    elif segment == "staffing_agency":
        segments.staffing_agency(books, scale)
    else:  # pragma: no cover - BUSINESS_SEGMENTS and this dispatch must stay in sync
        raise UnknownSegmentError(f"no activity profile for segment {segment}")

    # Expected inflow is the average monthly credit actually posted, with noise, so the profile is
    # honest but imperfect.
    active_days = max(1, PERIOD_DAYS - onboarded_day)
    b.set_expected(cid, books.credited_cents * MONTH_DAYS / active_days * rng.uniform(0.85, 1.25))
    return cid
