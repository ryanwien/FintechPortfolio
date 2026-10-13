"""Per-segment activity profiles for business customers.

Each profile posts one business's normal activity through ``BusinessBooks``. Most
segments are deliberate benign look-alikes for a rule (cash-intensive businesses for
structuring, escrow for rapid movement, importers for geography, staffing payroll for
velocity, landlords and charities for fan-in) and register themselves in
``WorldBuilder.lookalikes`` under the bucket the evaluation reports on.

The order of RNG draws inside each profile is fixed; changing it changes the world.
"""

from __future__ import annotations

from typing import Final

from ..model import CASH, HOUR, MONITOR_START_DAY, PERIOD_DAYS, ts_at
from .builder import BusinessBooks, lognormal_cents, work_ts
from .catalog import BRANCHES, BY_TIER, CASH_MEDIANS

CASH_DEPOSIT_DAY_P: Final = 0.7
HOME_BRANCH_P: Final = 0.85
CONTRACT_SPIKE_P: Final = 0.12
MULTI_COUNTRY_IMPORTER_P: Final = 0.12
MULTI_COUNTRY_IMPORTER_COUNTRIES: Final = 3
ESCROW_CLOSING_DAY_P: Final = 0.25
SHELL_CREDIT_DAY_P: Final = 0.08
WEEK_DAYS: Final = 7
MONTH_DAYS: Final = 30


def shell_activity(books: BusinessBooks) -> None:
    """Thin activity for a 'light' business: a few client credits and rent."""
    rng, b = books.rng, books.builder
    clients = [b.ext("CLI") for _ in range(rng.randint(2, 4))]
    for day in books.days:
        if rng.random() < SHELL_CREDIT_DAY_P:
            books.credit(
                work_ts(rng, day), "ach", rng.choice(clients), lognormal_cents(rng, 4_000_00, 0.5)
            )
    books.rent(rng.randint(1_500, 4_000) * 100)


def cash_intensive(books: BusinessBooks, segment: str, scale: float) -> None:
    """Daily cash and card takings, weekly suppliers, payroll: the structuring look-alike."""
    rng, b, a = books.rng, books.builder, books.account
    median = CASH_MEDIANS[segment] * 100 * scale
    home = b.customers[books.cid].home_branch
    suppliers = [f"X:SUP{rng.randrange(200):03d}" for _ in range(3)]
    for day in books.days:
        if rng.random() < CASH_DEPOSIT_DAY_P:
            branch = home if rng.random() < HOME_BRANCH_P else rng.choice(BRANCHES)
            books.credit(
                ts_at(day, 17, rng.randrange(120)),
                "cash_deposit",
                CASH,
                lognormal_cents(rng, median, 0.5),
                branch=branch,
            )
        books.credit(
            ts_at(day, 4, rng.randrange(60)),
            "ach",
            "X:CARDPROC",
            lognormal_cents(rng, median * 0.9, 0.35),
        )
        if day % WEEK_DAYS == 2:
            for s in suppliers:
                b.txn(work_ts(rng, day), "ach", a, s, lognormal_cents(rng, median * 1.2, 0.3))
    books.payroll(rng.randint(4, 14), 900, 1_800)
    books.rent(int(median * 4))
    b.lookalikes["cash_intensive_business"].append(books.cid)


def professional(books: BusinessBooks, scale: float) -> None:
    """Weekly client invoices; occasionally a one-off contract paid on to subcontractors."""
    rng, b, a = books.rng, books.builder, books.account
    clients = [b.ext("CLI") for _ in range(rng.randint(6, 20))]
    for day in books.days:
        if day % WEEK_DAYS == 0:
            for _ in range(rng.randint(1, 4)):
                books.credit(
                    work_ts(rng, day + rng.randrange(5)),
                    "ach",
                    rng.choice(clients),
                    lognormal_cents(rng, 6_000_00 * scale, 0.6),
                )
    books.payroll(rng.randint(3, 12), 2_500, 6_000)
    books.rent(rng.randint(3_000, 12_000) * 100)
    if rng.random() < CONTRACT_SPIKE_P:
        # legitimate one-off contract / progress payment, mostly paid on to
        # subcontractors within days; not part of the declared profile
        t0 = work_ts(rng, rng.randint(MONITOR_START_DAY, PERIOD_DAYS - 8))
        amt = rng.randint(80_000, 400_000) * 100
        b.txn(t0, "wire", b.ext("PROJ"), a, amt)
        subs = rng.randint(2, 5)
        payout = int(amt * rng.uniform(0.6, 0.95))
        for _ in range(subs):
            b.txn(
                t0 + rng.randint(6, 120) * HOUR,
                rng.choice(("wire", "ach")),
                a,
                b.ext("SUBCON"),
                payout // subs,
            )
        b.lookalikes["contract_payment_spike"].append(books.cid)


def importer(books: BusinessBooks, scale: float, n_countries: int | None) -> None:
    """Domestic client receipts, recurring supplier wires abroad: the geography look-alike."""
    rng, b, a = books.rng, books.builder, books.account
    clients = [b.ext("CLI") for _ in range(rng.randint(5, 15))]
    n_countries = n_countries or (
        MULTI_COUNTRY_IMPORTER_COUNTRIES if rng.random() < MULTI_COUNTRY_IMPORTER_P else 1
    )
    countries = rng.sample(BY_TIER["medium"], n_countries)
    suppliers = {c: b.ext("SUPINTL") for c in countries}
    for day in books.days:
        for _ in range(rng.choice((0, 0, 1, 1, 2))):
            books.credit(
                work_ts(rng, day),
                "ach",
                rng.choice(clients),
                lognormal_cents(rng, 15_000_00 * scale, 0.5),
            )
    day = books.start_day + rng.randint(0, 10)
    while day < PERIOD_DAYS:
        c = rng.choice(countries)
        b.txn(
            work_ts(rng, day),
            "wire",
            a,
            suppliers[c],
            lognormal_cents(rng, 35_000_00 * scale, 0.4),
            country=c,
        )
        day += rng.randint(7, 21)
    books.payroll(rng.randint(4, 12), 2_000, 5_000)
    books.rent(rng.randint(4_000, 10_000) * 100)
    b.lookalikes["importer"].append(books.cid)


def title_escrow(books: BusinessBooks) -> None:
    """Large closings received and paid out within hours: the rapid-movement look-alike."""
    rng, b, a = books.rng, books.builder, books.account
    for day in books.days:
        if rng.random() < ESCROW_CLOSING_DAY_P:
            t0 = work_ts(rng, day)
            amt = rng.randint(150_000, 700_000) * 100
            books.credit(t0, "wire", b.ext("BUYER"), amt)
            t1 = t0 + rng.randint(2, 30) * HOUR
            seller = int(amt * rng.uniform(0.62, 0.75))
            fee = int(amt * rng.uniform(0.01, 0.025))
            b.txn(t1, "wire", a, b.ext("SELLER"), seller)
            b.txn(t1 + HOUR, "wire", a, b.ext("LENDER"), amt - seller - fee)
    books.payroll(rng.randint(4, 10), 2_500, 5_000)
    books.rent(rng.randint(4_000, 9_000) * 100)
    b.lookalikes["title_escrow"].append(books.cid)


def property_management(books: BusinessBooks) -> None:
    """Many tenants pay in, a few owners are paid out: the fan-in look-alike."""
    rng, b, a = books.rng, books.builder, books.account
    tenants = [(b.ext("TEN"), rng.randint(900, 2_500) * 100) for _ in range(rng.randint(15, 40))]
    owners = [b.ext("OWNER") for _ in range(rng.randint(2, 5))]
    for month in range(books.start_day, PERIOD_DAYS, MONTH_DAYS):
        collected = 0
        for tenant, amt in tenants:
            day = month + rng.randint(0, 4)
            books.credit(work_ts(rng, day), rng.choice(("p2p", "ach")), tenant, amt)
            collected += amt
        share = int(collected * rng.uniform(0.7, 0.85)) // len(owners)
        for o in owners:
            b.txn(work_ts(rng, month + rng.randint(15, 18)), "ach", a, o, share)
    b.lookalikes["property_management"].append(books.cid)


def nonprofit(books: BusinessBooks) -> None:
    """Many small first-time donors plus regulars, monthly grants: the fan-in look-alike."""
    rng, b, a = books.rng, books.builder, books.account
    regulars = [b.ext("DONOR") for _ in range(rng.randint(5, 15))]
    for day in books.days:
        if day % WEEK_DAYS == 0:
            for _ in range(rng.randint(3, 12)):
                books.credit(
                    work_ts(rng, day + rng.randrange(7), 8, 22),
                    "p2p",
                    b.ext("DONOR"),
                    lognormal_cents(rng, 80_00, 0.8),
                )
        if day % MONTH_DAYS == 3:
            for r in regulars:
                books.credit(work_ts(rng, day), "ach", r, rng.randint(50, 500) * 100)
        if day % MONTH_DAYS == 20:
            for _ in range(rng.randint(1, 3)):
                b.txn(
                    work_ts(rng, day), "ach", a, b.ext("GRANTEE"), rng.randint(1_000, 6_000) * 100
                )
    books.payroll(rng.randint(2, 5), 1_500, 3_000)
    b.lookalikes["nonprofit_donations"].append(books.cid)


def staffing_agency(books: BusinessBooks, scale: float) -> None:
    """Large weekly client receipts, wide weekly payroll: the velocity look-alike."""
    rng, b = books.rng, books.builder
    clients = [b.ext("CLI") for _ in range(rng.randint(2, 4))]
    n_workers = rng.randint(25, 60)
    for day in books.days:
        if day % WEEK_DAYS == 1:
            for c in clients:
                books.credit(
                    work_ts(rng, day), "ach", c, lognormal_cents(rng, 25_000_00 * scale, 0.25)
                )
    books.payroll(n_workers, 600, 1_400, every=WEEK_DAYS, offset=3)
    books.rent(rng.randint(3_000, 8_000) * 100)
    b.lookalikes["staffing_payroll"].append(books.cid)
