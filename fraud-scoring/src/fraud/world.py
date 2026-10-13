"""The simulated card network: customers, merchants and their devices, plus the shared
building blocks (row accumulator, device-id allocator, amount sampler) that the legit
generator and the fraud injectors both write through.

Invariant: every function here consumes the caller's ``np.random.Generator`` in a fixed
order, so the whole stream is reproducible from :attr:`SimConfig.seed`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

import numpy as np

from fraud.catalog import (
    ABROAD,
    DOMESTIC,
    LOCATIONS,
    MCC_BASE_WEIGHT,
    MCC_CODES,
    MCCS,
    N_LOC,
)
from fraud.config import SimConfig

KM_PER_DEGREE: Final = 111.0
MIN_COS_LAT: Final = 0.2  # caps longitude stretch near the poles
MERCHANT_JITTER_KM: Final = 12.0
CUSTOMER_JITTER_KM: Final = 15.0
ABROAD_MERCHANT_WEIGHT: Final = 0.01  # a few merchants abroad
MCC_SPREAD_EXPONENT: Final = 0.6  # flattens the MCC mix of merchants relative to spend
ONLINE_MERCHANTS_PER_MCC: Final = 8  # guaranteed minimum per online-only MCC
WALLET_CONCENTRATION: Final = 40.0  # Dirichlet concentration of each customer's MCC mix
PHONE_UPGRADE_SHARE: Final = 0.4
MIN_AMOUNT_CENTS: Final = 50

# Device-id ranges. 0 means "card present, no device".
CUSTOMER_DEVICE_BASE: Final = 10_000  # each customer owns (base + 2c, base + 2c + 1)
ONE_OFF_DEVICE_BASE: Final = 500_000  # legit guest checkouts / new browsers
FRAUD_DEVICE_BASE: Final = 1_000_000  # fraudster devices, never collide with legit ones

COLUMNS: Final = (
    "ts",
    "customer_id",
    "merchant_id",
    "mcc",
    "amount_cents",
    "lat",
    "lon",
    "device_id",
    "card_present",
    "is_fraud",
    "fraud_type",
    "label_ts",
)


@dataclass(slots=True)
class World:
    """Customer and merchant attributes drawn once per simulation."""

    cust_home: np.ndarray  # (n, 2) lat/lon
    cust_loc: np.ndarray  # home location index
    cust_start: np.ndarray  # account open time (s)
    cust_rate: np.ndarray  # expected transactions per day
    cust_scale: np.ndarray  # ticket-size multiplier
    cust_mcc_p: np.ndarray  # (n, n_mcc)
    cust_devices: np.ndarray  # (n, 2) primary, upgrade device id
    cust_upgrade_ts: np.ndarray  # inf if the customer never upgrades
    merch_mcc: np.ndarray
    merch_loc_idx: np.ndarray  # -1 for online merchants
    merch_latlon: np.ndarray
    merch_by_mcc_loc: dict[tuple[int, int], np.ndarray]
    merch_online_by_mcc: dict[int, np.ndarray]

    def device_at(self, customer: int, ts: int) -> int:
        """The customer's own device at time ``ts`` (switches after a phone upgrade)."""
        upgraded = ts >= self.cust_upgrade_ts[customer]
        return int(self.cust_devices[customer, 1] if upgraded else self.cust_devices[customer, 0])


class TxnRows:
    """Column-oriented accumulator for transactions (cheap appends, one DataFrame at the end)."""

    def __init__(self) -> None:
        self.cols: dict[str, list] = {k: [] for k in COLUMNS}

    def add(
        self,
        *,
        ts: int,
        customer_id: int,
        merchant_id: int,
        mcc: int,
        amount_cents: int,
        lat: float,
        lon: float,
        device_id: int,
        card_present: bool,
        is_fraud: bool,
        fraud_type: str,
        label_ts: int,
    ) -> None:
        values = (
            ts,
            customer_id,
            merchant_id,
            mcc,
            amount_cents,
            lat,
            lon,
            device_id,
            card_present,
            is_fraud,
            fraud_type,
            label_ts,
        )
        for name, value in zip(COLUMNS, values, strict=True):
            self.cols[name].append(value)


class DeviceIds:
    """Monotonic allocator for never-seen device ids."""

    def __init__(self, start: int) -> None:
        self.next = start

    def new(self) -> int:
        self.next += 1
        return self.next


def jitter(rng: np.random.Generator, lat: float, lon: float, km: float) -> np.ndarray:
    """A point roughly ``km`` (one sigma) from (lat, lon)."""
    d = rng.normal(0.0, km / KM_PER_DEGREE, size=2)
    d[1] /= max(np.cos(np.radians(lat)), MIN_COS_LAT)
    return np.array([lat + d[0], lon + d[1]])


def amount_cents(rng: np.random.Generator, mean_dollars: float, sigma: float = 0.6) -> int:
    """Lognormal ticket with the given mean, in integer cents (at least 50c)."""
    v = mean_dollars * rng.lognormal(-0.5 * sigma**2, sigma)
    return int(max(MIN_AMOUNT_CENTS, round(v * 100)))


def away_location(rng: np.random.Generator, home: int) -> int:
    """A location other than ``home``: abroad a quarter of the time, else another metro."""
    if rng.random() < 0.25:
        return int(rng.choice(ABROAD))
    k = int(rng.choice(DOMESTIC))
    return k if k != home else (k + 3) % len(DOMESTIC)


def build_world(cfg: SimConfig, rng: np.random.Generator) -> World:
    """Draw merchants (with guaranteed coverage of every MCC/location) and customers."""
    pop_w = np.array([loc[3] for loc in LOCATIONS])
    pop_w = pop_w / pop_w.sum()
    merch_w = pop_w + ABROAD_MERCHANT_WEIGHT
    merch_w = merch_w / merch_w.sum()
    n_mcc = len(MCC_CODES)
    is_online = np.array([MCCS[int(m)][2] for m in MCC_CODES])

    mcc_w = (
        np.array([MCC_BASE_WEIGHT[int(m)] for m in MCC_CODES], dtype=float) ** MCC_SPREAD_EXPONENT
    )
    mcc_idx = list(rng.choice(n_mcc, size=cfg.n_merchants, p=mcc_w / mcc_w.sum()))
    loc_idx = [-1 if is_online[i] else int(rng.choice(N_LOC, p=merch_w)) for i in mcc_idx]
    # Guarantee coverage: every physical (mcc, location) and several merchants per online mcc.
    for i in range(n_mcc):
        if is_online[i]:
            mcc_idx += [i] * ONLINE_MERCHANTS_PER_MCC
            loc_idx += [-1] * ONLINE_MERCHANTS_PER_MCC
        else:
            mcc_idx += [i] * N_LOC
            loc_idx += list(range(N_LOC))
    merch_mcc_idx = np.array(mcc_idx)
    merch_loc_idx = np.array(loc_idx)
    latlon = np.zeros((len(mcc_idx), 2))
    for j, k in enumerate(loc_idx):
        if k >= 0:
            latlon[j] = jitter(rng, LOCATIONS[k][1], LOCATIONS[k][2], MERCHANT_JITTER_KM)
    by_loc: dict[tuple[int, int], list[int]] = {}
    online: dict[int, list[int]] = {}
    for j, (mi, k) in enumerate(zip(mcc_idx, loc_idx, strict=True)):
        if k < 0:
            online.setdefault(int(mi), []).append(j)
        else:
            by_loc.setdefault((int(mi), int(k)), []).append(j)

    n = cfg.n_customers
    cust_loc = rng.choice(N_LOC, size=n, p=pop_w)
    cust_home = np.vstack(
        [jitter(rng, LOCATIONS[k][1], LOCATIONS[k][2], CUSTOMER_JITTER_KM) for k in cust_loc]
    )
    total = cfg.total_seconds
    late = rng.random(n) < cfg.late_joiner_share
    cust_start = np.where(late, rng.uniform(0, total, size=n), 0.0).astype(np.int64)
    cust_rate = cfg.mean_txn_per_day * rng.lognormal(-0.18, 0.6, size=n)
    cust_scale = rng.lognormal(0.0, 0.45, size=n)
    base = np.array([MCC_BASE_WEIGHT[int(m)] for m in MCC_CODES], dtype=float)
    cust_mcc_p = rng.dirichlet(base / base.sum() * WALLET_CONCENTRATION, size=n)
    cust_devices = np.column_stack(
        [CUSTOMER_DEVICE_BASE + 2 * np.arange(n), CUSTOMER_DEVICE_BASE + 1 + 2 * np.arange(n)]
    )
    upgrade = rng.uniform(0, total, size=n)
    cust_upgrade_ts = np.where(rng.random(n) < PHONE_UPGRADE_SHARE, upgrade, np.inf)

    return World(
        cust_home=cust_home,
        cust_loc=cust_loc,
        cust_start=cust_start,
        cust_rate=cust_rate,
        cust_scale=cust_scale,
        cust_mcc_p=cust_mcc_p,
        cust_devices=cust_devices,
        cust_upgrade_ts=cust_upgrade_ts,
        merch_mcc=MCC_CODES[merch_mcc_idx],
        merch_loc_idx=merch_loc_idx,
        merch_latlon=latlon,
        merch_by_mcc_loc={k: np.array(v) for k, v in by_loc.items()},
        merch_online_by_mcc={k: np.array(v) for k, v in online.items()},
    )
