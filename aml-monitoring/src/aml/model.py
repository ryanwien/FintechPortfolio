"""Core data types for the synthetic banking world.

Time is an integer number of minutes since ``EPOCH``; money is integer cents. Party ids
are account ids (``A...``), external counterparties (``X:<id>``) or ``CASH``.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Final

HOUR: Final = 60
DAY: Final = 24 * HOUR
EPOCH: Final = datetime(2026, 6, 1)
PERIOD_DAYS: Final = 92
# The first month is lookback history only (counterparty baselines, rolling windows);
# alerts are only raised on activity inside the monitoring window.
MONITOR_START_DAY: Final = 31

CASH: Final = "CASH"
EXTERNAL_PREFIX: Final = "X:"
CHANNELS: Final = ("cash_deposit", "cash_withdrawal", "wire", "ach", "p2p")
KYC_LEVELS: Final = ("low", "medium", "high")

# Jurisdiction risk table. Codes are ISO 3166 user-assigned (XA-XZ) on purpose: the
# project is about the mechanics, not about labelling real countries.
COUNTRY_TIERS: Final[dict[str, str]] = {
    "US": "domestic",
    "XA": "low",
    "XB": "low",
    "XC": "low",
    "XD": "medium",
    "XE": "medium",
    "XF": "medium",
    "XG": "medium",
    "XH": "high",
    "XJ": "high",
    "XK": "high",
}


def ts_at(day: int, hour: int = 12, minute: int = 0) -> int:
    """Timestamp (minutes since ``EPOCH``) of ``day`` at ``hour:minute``."""
    return day * DAY + hour * HOUR + minute


def to_datetime(ts: int) -> datetime:
    """Convert minutes since ``EPOCH`` to a naive datetime."""
    return EPOCH + timedelta(minutes=ts)


def fmt_ts(ts: int) -> str:
    """Format a timestamp as ``YYYY-MM-DD HH:MM``."""
    return to_datetime(ts).strftime("%Y-%m-%d %H:%M")


def fmt_date(ts: int) -> str:
    """Format a timestamp's calendar date as ``YYYY-MM-DD``."""
    return to_datetime(ts).strftime("%Y-%m-%d")


def fmt_cents(cents: int) -> str:
    """``$1,234.56`` (with a leading ``-`` for negatives)."""
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}${cents // 100:,}.{cents % 100:02d}"


def is_external(party: str) -> bool:
    """True for counterparties outside the bank, identified by the ``X:`` prefix."""
    return party.startswith(EXTERNAL_PREFIX)


@dataclass(frozen=True, slots=True)
class Customer:
    """A bank customer as declared at onboarding, with its KYC risk rating."""

    customer_id: str
    name: str
    kind: str  # "individual" | "business"
    segment: str  # occupation (individuals) or industry (businesses)
    kyc_risk: str  # "low" | "medium" | "high"
    expected_monthly_cents: int  # declared expected monthly inflow at onboarding
    onboarded_day: int
    home_branch: str


@dataclass(frozen=True, slots=True)
class Account:
    """An internal account owned by one customer."""

    account_id: str
    customer_id: str
    product: str


@dataclass(frozen=True, slots=True)
class Txn:
    """One transfer in integer cents; ``label`` is ground truth that detectors never read."""

    txn_id: str
    ts: int
    channel: str
    src: str  # account id, "X:<external>" or CASH
    dst: str
    amount_cents: int
    country: str = "US"  # counterparty jurisdiction (wires)
    branch: str | None = None  # cash channel location
    label: str | None = None  # ground truth only; detectors never read it


@dataclass(frozen=True, slots=True)
class TypologyInstance:
    """One injected ground-truth typology instance and its member customers."""

    instance_id: str
    typology: str
    members: tuple[str, ...]
    variant: str


@dataclass
class World:
    """The generated bank: entities, time-ordered transactions and ground truth."""

    customers: dict[str, Customer]
    accounts: dict[str, Account]
    txns: list[Txn]
    instances: list[TypologyInstance]
    lookalikes: dict[str, list[str]]
    seed: int
    monitor_start_ts: int = ts_at(MONITOR_START_DAY, 0)
    end_ts: int = ts_at(PERIOD_DAYS, 0)
    txn_by_id: dict[str, Txn] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Index transactions by id; derived state, so it is not a constructor argument."""
        self.txn_by_id = {t.txn_id: t for t in self.txns}

    def owner(self, party: str) -> str | None:
        """Customer id owning an internal account, else ``None``."""
        acct = self.accounts.get(party)
        return acct.customer_id if acct else None

    def party(self, party: str) -> str:
        """Customer id for internal accounts, the raw id for external parties / cash."""
        return self.owner(party) or party

    def truth(self) -> dict[str, str]:
        """Customer id -> typology of the first instance it belongs to."""
        out: dict[str, str] = {}
        for inst in self.instances:
            for m in inst.members:
                out.setdefault(m, inst.typology)
        return out

    def without_labels(self) -> World:
        """Copy with transaction labels, instances and look-alike buckets removed."""
        return World(
            customers=self.customers,
            accounts=self.accounts,
            txns=[replace(t, label=None) for t in self.txns],
            instances=[],
            lookalikes={},
            seed=self.seed,
            monitor_start_ts=self.monitor_start_ts,
            end_ts=self.end_ts,
        )
