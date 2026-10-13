"""Mutable, seeded builder that accumulates customers, accounts and transactions.

Invariants:

* One ``random.Random`` (``WorldBuilder.rng``) drives every draw, so a world is a pure
  function of its seed and of the order in which generators are called.
* Ids are sequential per kind (``C00001``, ``A000001``, ``T0000001``, ``X:<prefix>00001``).
* Transactions dated at or after the end of the period, or with a non-positive amount,
  are dropped (``txn`` returns ``None``).
"""

from __future__ import annotations

import math
import random
from collections import defaultdict
from dataclasses import replace
from typing import Final

from ..model import (
    KYC_LEVELS,
    PERIOD_DAYS,
    Account,
    Customer,
    Txn,
    TypologyInstance,
    World,
    ts_at,
)
from .catalog import BIZ_SUFFIX, BIZ_WORDS, BRANCHES, DEFAULT_BIZ_SUFFIX, FIRST_NAMES, LAST_NAMES

MIN_AMOUNT_CENTS: Final = 100
WORK_DAY_START_HOUR: Final = 8
WORK_DAY_END_HOUR: Final = 18
MINUTES_PER_HOUR: Final = 60
_PERIOD_END_TS: Final = ts_at(PERIOD_DAYS, 0)


def lognormal_cents(rng: random.Random, median_cents: float, sigma: float) -> int:
    """Lognormal amount in cents with the given median (floored at one dollar)."""
    return max(MIN_AMOUNT_CENTS, int(median_cents * math.exp(rng.gauss(0.0, sigma))))


def work_ts(
    rng: random.Random, day: int, lo: int = WORK_DAY_START_HOUR, hi: int = WORK_DAY_END_HOUR
) -> int:
    """Random timestamp on ``day`` between hours ``lo`` (inclusive) and ``hi`` (exclusive)."""
    return ts_at(day, rng.randint(lo, hi - 1), rng.randrange(MINUTES_PER_HOUR))


class WorldBuilder:
    """Accumulates world entities; ``build`` freezes them into a time-ordered ``World``."""

    def __init__(self, seed: int) -> None:
        self.seed = seed
        self.rng = random.Random(seed)
        self.customers: dict[str, Customer] = {}
        self.accounts: dict[str, Account] = {}
        self.primary: dict[str, str] = {}
        self.txns: list[Txn] = []
        self.instances: list[TypologyInstance] = []
        self.lookalikes: dict[str, list[str]] = defaultdict(list)
        self._counters: dict[str, int] = defaultdict(int)

    def _next(self, key: str) -> int:
        self._counters[key] += 1
        return self._counters[key]

    def ext(self, prefix: str) -> str:
        """A fresh external counterparty id."""
        return f"X:{prefix}{self._next('ext'):05d}"

    def draw_kyc(self, weights: tuple[float, ...]) -> str:
        """Draw a KYC rating with the given (low, medium, high) weights."""
        return self.rng.choices(KYC_LEVELS, weights)[0]

    def customer(
        self, kind: str, segment: str, kyc: str, expected: int = 0, onboarded_day: int = 0
    ) -> str:
        """Create a customer with one primary account; returns the customer id."""
        rng = self.rng
        cid = f"C{self._next('cust'):05d}"
        if kind == "individual":
            name = f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}"
        else:
            name = f"{rng.choice(BIZ_WORDS)} {BIZ_SUFFIX.get(segment, DEFAULT_BIZ_SUFFIX)} LLC"
        self.customers[cid] = Customer(
            cid, name, kind, segment, kyc, expected, onboarded_day, rng.choice(BRANCHES)
        )
        aid = f"A{self._next('acct'):06d}"
        product = "checking" if kind == "individual" else "business_checking"
        self.accounts[aid] = Account(aid, cid, product)
        self.primary[cid] = aid
        return cid

    def set_expected(self, cid: str, cents: float) -> None:
        """Record the declared expected monthly inflow (truncated to whole cents)."""
        self.customers[cid] = replace(self.customers[cid], expected_monthly_cents=int(cents))

    def acct(self, cid: str) -> str:
        """Primary account id of a customer."""
        return self.primary[cid]

    def txn(
        self,
        ts: int,
        channel: str,
        src: str,
        dst: str,
        amount: int,
        country: str = "US",
        branch: str | None = None,
        label: str | None = None,
    ) -> Txn | None:
        """Post a transaction; returns ``None`` if it falls outside the period or is empty."""
        if ts >= _PERIOD_END_TS or amount <= 0:
            return None
        t = Txn(
            f"T{self._next('txn'):07d}", ts, channel, src, dst, int(amount), country, branch, label
        )
        self.txns.append(t)
        return t

    def instance(self, typology: str, members: list[str], variant: str) -> str:
        """Register a ground-truth typology instance; returns its id (e.g. ``STR-03``)."""
        iid = f"{typology[:3].upper()}-{self._next('inst_' + typology):02d}"
        self.instances.append(TypologyInstance(iid, typology, tuple(members), variant))
        return iid

    def build(self) -> World:
        txns = sorted(self.txns, key=lambda t: (t.ts, t.txn_id))
        return World(
            customers=dict(self.customers),
            accounts=dict(self.accounts),
            txns=txns,
            instances=list(self.instances),
            lookalikes={k: sorted(v) for k, v in sorted(self.lookalikes.items())},
            seed=self.seed,
        )


class BusinessBooks:
    """Posts one business customer's activity and tallies credited inflow.

    ``credited_cents`` feeds the declared expected monthly inflow, so only credits that
    actually land inside the period count.
    """

    PAYROLL_EVERY_DAYS: Final = 14
    PAYROLL_OFFSET_DAY: Final = 5
    RENT_DAY_OF_MONTH: Final = 1
    MONTH_DAYS: Final = 30

    def __init__(self, b: WorldBuilder, cid: str, start_day: int) -> None:
        self.builder = b
        self.rng = b.rng
        self.cid = cid
        self.account = b.acct(cid)
        self.start_day = start_day
        self.days = range(start_day, PERIOD_DAYS)
        self.credited_cents = 0

    def credit(
        self, ts: int, channel: str, src: str, amount: int, branch: str | None = None
    ) -> None:
        """Inbound payment to the business; counted toward its declared profile."""
        if self.builder.txn(ts, channel, src, self.account, amount, branch=branch):
            self.credited_cents += amount

    def payroll(
        self,
        n_employees: int,
        wage_lo: int,
        wage_hi: int,
        every: int = PAYROLL_EVERY_DAYS,
        offset: int = PAYROLL_OFFSET_DAY,
    ) -> None:
        """Recurring ACH wages (dollars per run in ``[wage_lo, wage_hi]``) to fixed payees."""
        b, rng = self.builder, self.rng
        payees = [b.ext("PAYEE") for _ in range(n_employees)]
        wages = [rng.randint(wage_lo, wage_hi) * 100 for _ in payees]
        for day in self.days:
            if day % every == offset:
                for payee, wage in zip(payees, wages, strict=True):
                    b.txn(
                        ts_at(day, 6, rng.randrange(30)),
                        "ach",
                        self.account,
                        payee,
                        int(wage * rng.uniform(0.95, 1.05)),
                    )

    def rent(self, amount: int) -> None:
        """Monthly ACH rent to a commercial landlord."""
        landlord = self.builder.ext("CRE")
        for day in self.days:
            if day % self.MONTH_DAYS == self.RENT_DAY_OF_MONTH:
                self.builder.txn(ts_at(day, 9), "ach", self.account, landlord, amount)
