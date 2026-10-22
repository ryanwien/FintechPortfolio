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
    # Credits land on about 8% of days, from a small fixed client list.
    for day in books.days:
        if rng.random() < SHELL_CREDIT_DAY_P:
            books.credit(
                work_ts(rng, day), "ach", rng.choice(clients), lognormal_cents(rng, 4_000_00, 0.5)
            )
    # Monthly rent is what makes the shell look like an operating business.
    books.rent(rng.randint(1_500, 4_000) * 100)


def cash_intensive(books: BusinessBooks, segment: str, scale: float) -> None:
    """Daily cash and card takings, weekly suppliers, payroll: the structuring look-alike."""
    rng, b, a = books.rng, books.builder, books.account
    # Median daily takings scale with the segment and with this business's own scale factor.
    median = CASH_MEDIANS[segment] * 100 * scale
    home = b.customers[books.cid].home_branch
    # Suppliers are a fixed set of three, paid weekly.
    suppliers = [f"X:SUP{rng.randrange(200):03d}" for _ in range(3)]
    for day in books.days:
        # About 70% of days have a cash deposit; the rest only get card settlements.
        if rng.random() < CASH_DEPOSIT_DAY_P:
            # Most deposits go to the home branch; the rest are spread across the other branches.
            branch = home if rng.random() < HOME_BRANCH_P else rng.choice(BRANCHES)
            books.credit(
                ts_at(day, 17, rng.randrange(120)),
                "cash_deposit",
                CASH,
                lognormal_cents(rng, median, 0.5),
                branch=branch,
            )
        # Card processor settlements arrive every day, a little smaller than the cash median.
        books.credit(
            ts_at(day, 4, rng.randrange(60)),
            "ach",
            "X:CARDPROC",
            lognormal_cents(rng, median * 0.9, 0.35),
        )
        # Supplier payments go out on one weekday each week.
        if day % WEEK_DAYS == 2:
            for s in suppliers:
                b.txn(work_ts(rng, day), "ach", a, s, lognormal_cents(rng, median * 1.2, 0.3))
    # Payroll and rent are the fixed monthly costs; crew size is drawn per business.
    books.payroll(rng.randint(4, 14), 900, 1_800)
    books.rent(int(median * 4))
    b.lookalikes["cash_intensive_business"].append(books.cid)


def professional(books: BusinessBooks, scale: float) -> None:
    """Weekly client invoices; occasionally a one-off contract paid on to subcontractors."""
    rng, b, a = books.rng, books.builder, books.account
    # A stable client list; invoices are spread across the year.
    clients = [b.ext("CLI") for _ in range(rng.randint(6, 20))]
    for day in books.days:
        # Invoices arrive on one weekday each week, in batches of one to four.
        if day % WEEK_DAYS == 0:
            for _ in range(rng.randint(1, 4)):
                # Each invoice is a log-normal amount around $6,000, scaled by the business's size.
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
        # Subcontractors are paid 60-95% of the receipt; the firm keeps the margin.
        payout = int(amt * rng.uniform(0.6, 0.95))
        # Split the payout evenly across the subcontractors, spaced within a few days.
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
    # Most importers buy from one foreign country; a minority buy from several.
    n_countries = n_countries or (
        MULTI_COUNTRY_IMPORTER_COUNTRIES if rng.random() < MULTI_COUNTRY_IMPORTER_P else 1
    )
    # Supplier countries come from the medium-risk tier, which is what makes these wires look like
    # geography hits.
    countries = rng.sample(BY_TIER["medium"], n_countries)
    # One supplier account per country, reused for every shipment.
    suppliers = {c: b.ext("SUPINTL") for c in countries}
    for day in books.days:
        # Domestic client receipts: zero to two per day, weighted so most days have none or one.
        for _ in range(rng.choice((0, 0, 1, 1, 2))):
            books.credit(
                work_ts(rng, day),
                "ach",
                rng.choice(clients),
                lognormal_cents(rng, 15_000_00 * scale, 0.5),
            )
    # The first supplier wire lands within ten days of the start, then at gaps of one to three
    # weeks.
    day = books.start_day + rng.randint(0, 10)
    while day < PERIOD_DAYS:
        # Pick a supplier country each time, so wires cycle through the chosen countries.
        c = rng.choice(countries)
        b.txn(
            work_ts(rng, day),
            "wire",
            a,
            suppliers[c],
            lognormal_cents(rng, 35_000_00 * scale, 0.4),
            country=c,
        )
        # Gaps of seven to twenty-one days between supplier wires.
        day += rng.randint(7, 21)
    books.payroll(rng.randint(4, 12), 2_000, 5_000)
    books.rent(rng.randint(4_000, 10_000) * 100)
    b.lookalikes["importer"].append(books.cid)


def title_escrow(books: BusinessBooks) -> None:
    """Large closings received and paid out within hours: the rapid-movement look-alike."""
    rng, b, a = books.rng, books.builder, books.account
    for day in books.days:
        # On about a quarter of days a closing completes: funds come in from the buyer, then go out
        # within hours.
        if rng.random() < ESCROW_CLOSING_DAY_P:
            t0 = work_ts(rng, day)
            amt = rng.randint(150_000, 700_000) * 100
            books.credit(t0, "wire", b.ext("BUYER"), amt)
            t1 = t0 + rng.randint(2, 30) * HOUR
            # The seller takes 62-75% of the closing amount and a 1-2.5% fee is kept; the rest goes
            # to the lender.
            seller = int(amt * rng.uniform(0.62, 0.75))
            fee = int(amt * rng.uniform(0.01, 0.025))
            # Pay the seller within hours of receipt; the lender is paid an hour later.
            b.txn(t1, "wire", a, b.ext("SELLER"), seller)
            b.txn(t1 + HOUR, "wire", a, b.ext("LENDER"), amt - seller - fee)
    books.payroll(rng.randint(4, 10), 2_500, 5_000)
    books.rent(rng.randint(4_000, 9_000) * 100)
    b.lookalikes["title_escrow"].append(books.cid)


def property_management(books: BusinessBooks) -> None:
    """Many tenants pay in, a few owners are paid out: the fan-in look-alike."""
    rng, b, a = books.rng, books.builder, books.account
    # Each tenant pays a fixed monthly rent between $900 and $2,500.
    tenants = [(b.ext("TEN"), rng.randint(900, 2_500) * 100) for _ in range(rng.randint(15, 40))]
    # A few owners receive the payouts.
    owners = [b.ext("OWNER") for _ in range(rng.randint(2, 5))]
    for month in range(books.start_day, PERIOD_DAYS, MONTH_DAYS):
        collected = 0
        for tenant, amt in tenants:
            # Tenants pay within the first five days of the month.
            day = month + rng.randint(0, 4)
            books.credit(work_ts(rng, day), rng.choice(("p2p", "ach")), tenant, amt)
            collected += amt
        # Owners receive 70-85% of what was collected, split evenly; the rest is the manager's fee.
        share = int(collected * rng.uniform(0.7, 0.85)) // len(owners)
        # Owner payouts go out mid-month, after collections are in.
        for o in owners:
            b.txn(work_ts(rng, month + rng.randint(15, 18)), "ach", a, o, share)
    b.lookalikes["property_management"].append(books.cid)


def nonprofit(books: BusinessBooks) -> None:
    """Many small first-time donors plus regulars, monthly grants: the fan-in look-alike."""
    rng, b, a = books.rng, books.builder, books.account
    # A few regular donors give every month; the rest are small one-off donors.
    regulars = [b.ext("DONOR") for _ in range(rng.randint(5, 15))]
    for day in books.days:
        # Weekly one-off donations: three to twelve small gifts a week from the donor pool.
        if day % WEEK_DAYS == 0:
            for _ in range(rng.randint(3, 12)):
                books.credit(
                    work_ts(rng, day + rng.randrange(7), 8, 22),
                    "p2p",
                    b.ext("DONOR"),
                    lognormal_cents(rng, 80_00, 0.8),
                )
        # Regular donors give once a month, on the third.
        if day % MONTH_DAYS == 3:
            for r in regulars:
                books.credit(work_ts(rng, day), "ach", r, rng.randint(50, 500) * 100)
        # Grants go out around the 20th of each month, to one to three grantees.
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
    # A few fixed corporate clients.
    clients = [b.ext("CLI") for _ in range(rng.randint(2, 4))]
    # The crew size is drawn once per business, so payroll is steady week to week.
    n_workers = rng.randint(25, 60)
    for day in books.days:
        # Clients are invoiced on one weekday each week; receipts are large and steady.
        if day % WEEK_DAYS == 1:
            for c in clients:
                books.credit(
                    work_ts(rng, day), "ach", c, lognormal_cents(rng, 25_000_00 * scale, 0.25)
                )
    # Weekly payroll for all workers, paid on the fourth day of each week.
    books.payroll(n_workers, 600, 1_400, every=WEEK_DAYS, offset=3)
    books.rent(rng.randint(3_000, 8_000) * 100)
    b.lookalikes["staffing_payroll"].append(books.cid)
