"""Fraud-pattern injectors.

* ``card_testing``   - burst of tiny charges at digital-goods merchants, then maybe a cash-out
* ``ato``            - account takeover: new device, often a geo jump, high-value online buys
* ``high_value``     - stolen/counterfeit card used for out-of-pattern big-ticket purchases
* ``merchant_compromise`` - cards skimmed at a breached merchant are cloned and used weeks later
* ``friendly_fraud`` - a genuine purchase later disputed as unauthorised (label noise by design)

Every fraud transaction gets a ``label_ts``: when its chargeback / customer report arrives.
In the shifted period account takeovers change tactics (residential proxies near the victim,
never-seen devices, smaller baskets), which silences the geo-jump and shared-device signals.

Invariant: the order of random draws is part of the contract (it fixes the dataset for a
seed), so the daily patterns are injected day by day in a fixed order.
"""

from __future__ import annotations

from typing import Final

import numpy as np
import pandas as pd

from fraud.catalog import (
    LOCATIONS,
    MCC_DEPARTMENT_STORE,
    MCC_DIGITAL_GOODS,
    MCC_ELECTRONICS,
    MCC_FUEL,
    MCC_GIFT_CARD,
    MCC_GROCERY,
    MCC_INDEX,
    MCC_JEWELRY,
    MCC_ONLINE_RETAIL,
    MCCS,
)
from fraud.config import DAY, HOUR, SimConfig
from fraud.world import DeviceIds, TxnRows, World, amount_cents, away_location

# Chargeback / report delay: lognormal around a per-type median, clipped to [0.5, 90] days.
LABEL_DELAY_MEDIAN_DAYS: Final = {"ato": 3.0, "friendly_fraud": 25.0}
LABEL_DELAY_DEFAULT_DAYS: Final = 14.0
LABEL_DELAY_SIGMA: Final = 0.7
LABEL_DELAY_MIN: Final = 0.5 * DAY
LABEL_DELAY_MAX: Final = 90 * DAY

RING_DEVICES: Final = np.arange(900_000, 900_400)  # devices reused by a fraud ring
N_WEAK_DIGITAL_MERCHANTS: Final = 4  # weak controls, favoured by card testers
VICTIM_MIN_TENURE: Final = 7 * DAY  # fraudsters hit accounts that have some history
NEAR_HOME_NOISE_DEG: Final = 0.08
POPULAR_MERCHANTS: Final = 100  # breaches hit busy merchants
FRIENDLY_FRAUD_MIN_CENTS: Final = 4_000

ATO_SHIFT_RATE_MULT: Final = 1.5
ATO_MCCS: Final = (MCC_ELECTRONICS, MCC_GIFT_CARD, MCC_JEWELRY, MCC_ONLINE_RETAIL)
ATO_SHIFTED_MCCS: Final = (MCC_ONLINE_RETAIL, MCC_ONLINE_RETAIL, MCC_ELECTRONICS)
HIGH_VALUE_MCCS: Final = [MCC_JEWELRY, MCC_ELECTRONICS, MCC_GIFT_CARD, MCC_DEPARTMENT_STORE]
CLONED_CARD_MCCS: Final = [
    MCC_FUEL,
    MCC_DEPARTMENT_STORE,
    MCC_ELECTRONICS,
    MCC_GIFT_CARD,
    MCC_GROCERY,
]


def label_delay(rng: np.random.Generator, fraud_type: str) -> int:
    """Seconds between a fraud transaction and its chargeback / report."""
    median_days = LABEL_DELAY_MEDIAN_DAYS.get(fraud_type, LABEL_DELAY_DEFAULT_DAYS)
    # Lognormal delay: most reports land near the median, with a long tail of late ones.
    d = rng.lognormal(np.log(median_days), LABEL_DELAY_SIGMA) * DAY
    return int(min(max(d, LABEL_DELAY_MIN), LABEL_DELAY_MAX))


def flip_friendly_fraud(
    cfg: SimConfig, rng: np.random.Generator, rows: TxnRows, legit: pd.DataFrame
) -> np.ndarray:
    """Mark a few genuine CNP purchases as disputed fraud, in place in ``rows``.

    ``legit`` must be the DataFrame view of ``rows`` (same row order). Returns the boolean
    mask of flipped rows.
    """
    # Only card-not-present purchases above a minimum amount can be disputed as friendly fraud.
    eligible = (~legit["card_present"]) & (legit["amount_cents"] >= FRIENDLY_FRAUD_MIN_CENTS)
    flip = (eligible & (rng.random(len(legit)) < cfg.friendly_fraud_p)).to_numpy()
    # Rows are stored column-wise, so flip the label columns by index, not by rebuilding rows.
    for i in np.flatnonzero(flip):
        rows.cols["is_fraud"][i] = True
        rows.cols["fraud_type"][i] = "friendly_fraud"
        rows.cols["label_ts"][i] = int(rows.cols["ts"][i]) + label_delay(rng, "friendly_fraud")
    return flip


class _Injector:
    """Shared context for the fraud patterns: victim choice, merchant lookup, row emission."""

    def __init__(
        self,
        cfg: SimConfig,
        w: World,
        rng: np.random.Generator,
        rows: TxnRows,
        devices: DeviceIds,
    ) -> None:
        """Bind the world, RNG, output rows and device-id allocator for one simulation run."""
        self.w = w
        self.rng = rng
        self.rows = rows
        self.devices = devices
        self.total_s = cfg.total_seconds
        self.digital = w.merch_online_by_mcc[MCC_INDEX[MCC_DIGITAL_GOODS]]
        self.weak_digital = self.digital[:N_WEAK_DIGITAL_MERCHANTS]
        self.online_retail = w.merch_online_by_mcc[MCC_INDEX[MCC_ONLINE_RETAIL]]

    def victim(self, t: int) -> int:
        """A customer active for at least a week, weighted by spend rate."""
        # Victims need an account old enough to have history, which is what fraudsters target.
        active = self.w.cust_start <= max(t - VICTIM_MIN_TENURE, 0)
        # Weight by spend rate: busier accounts offer more cover and are hit more often.
        p = np.where(active, self.w.cust_rate, 0.0)
        return int(self.rng.choice(len(p), p=p / p.sum()))

    def physical(self, mcc: int, loc: int) -> int:
        """Pick a random merchant with this MCC in location ``loc``."""
        pool = self.w.merch_by_mcc_loc[(MCC_INDEX[mcc], loc)]
        return int(pool[int(self.rng.integers(0, len(pool)))])

    def near_home(self, c: int) -> tuple[float, float]:
        """Customer home location with GPS-scale noise, so the fraud looks local."""
        return (
            float(self.w.cust_home[c, 0] + self.rng.normal(0, NEAR_HOME_NOISE_DEG)),
            float(self.w.cust_home[c, 1] + self.rng.normal(0, NEAR_HOME_NOISE_DEG)),
        )

    def near_home_or_away(self, c: int, p_home: float) -> tuple[float, float]:
        """Near the victim's home with probability ``p_home``, else at an away metro."""
        if self.rng.random() < p_home:
            return self.near_home(c)
        k = away_location(self.rng, int(self.w.cust_loc[c]))
        return LOCATIONS[k][1], LOCATIONS[k][2]

    def ring_or_new_device(self, p_ring: float) -> int:
        """Shared ring device with probability ``p_ring``, otherwise a freshly allocated device."""
        if self.rng.random() < p_ring:
            return int(self.rng.choice(RING_DEVICES))
        return self.devices.new()

    def emit(
        self,
        t: int,
        c: int,
        m: int,
        amount: int,
        lat: float,
        lon: float,
        device: int,
        card_present: bool,
        fraud_type: str,
    ) -> None:
        """Record one fraud transaction; anything past the end of the stream is dropped."""
        # The stream has a fixed end; episodes that would run past it are dropped, not cut short.
        if t >= self.total_s:
            return
        # label_ts is the report time: chargebacks land days later, so they cannot inform scoring.
        self.rows.add(
            ts=int(t),
            customer_id=int(c),
            merchant_id=int(m),
            mcc=int(self.w.merch_mcc[m]),
            amount_cents=int(amount),
            lat=float(lat),
            lon=float(lon),
            device_id=int(device),
            card_present=bool(card_present),
            is_fraud=True,
            fraud_type=fraud_type,
            label_ts=int(t) + label_delay(self.rng, fraud_type),
        )


def _card_testing(inj: _Injector, day0: int) -> None:
    """Emit a burst of small digital-goods charges, sometimes followed by a larger cash-out."""
    rng = inj.rng
    t = day0 + int(rng.integers(0, DAY))
    c = inj.victim(t)
    # One attacker session: the same place and device for the whole burst.
    lat, lon = inj.near_home_or_away(c, p_home=0.5)
    dev = inj.ring_or_new_device(p_ring=0.4)
    # Probes: tiny charges 30 seconds to 20 minutes apart, testing whether the card is live.
    for _k in range(int(rng.integers(2, 8))):
        m = int(rng.choice(inj.weak_digital) if rng.random() < 0.5 else rng.choice(inj.digital))
        inj.emit(t, c, m, int(rng.integers(50, 500)), lat, lon, dev, False, "card_testing")
        t += int(rng.integers(30, 20 * 60))
    if rng.random() < 0.5:  # cash-out once the card is confirmed live
        t += int(rng.integers(600, 6 * HOUR))
        m = int(rng.choice(inj.online_retail))
        inj.emit(t, c, m, amount_cents(rng, 220.0, 0.5), lat, lon, dev, False, "card_testing")


def _account_takeover(inj: _Injector, day0: int, shifted: bool) -> None:
    """Emit one account-takeover episode for a random victim: a burst of online purchases.

    Before the shift the attacker sits at an away location or uses a ring device. In the shifted
    period it stays near home on a new device and buys smaller baskets at general online retail.
    """
    rng = inj.rng
    t = day0 + int(rng.integers(0, DAY))
    c = inj.victim(t)
    if shifted:
        # Tactic change: residential proxies next to the victim, never-seen devices,
        # smaller baskets at general online retail. Geo-jump and shared-device
        # signals stop firing.
        lat, lon = inj.near_home(c)
        dev = inj.devices.new()
        mults, mccs = (1.0, 2.5), ATO_SHIFTED_MCCS
    else:
        # Before the shift the attacker is often away, on a shared ring device or a new one.
        lat, lon = inj.near_home_or_away(c, p_home=0.4)
        dev = inj.ring_or_new_device(p_ring=0.35)
        mults, mccs = (2.5, 10.0), ATO_MCCS
    # One to four purchases in a session, spaced 5 minutes to 3 hours apart.
    for _k in range(int(rng.integers(1, 5))):
        mcc = int(rng.choice(mccs))
        pool = inj.w.merch_online_by_mcc.get(MCC_INDEX[mcc], inj.online_retail)
        m = int(rng.choice(pool))
        # Basket size follows the victim's spend level; the multiplier range shifts with the tactic.
        mean = MCCS[mcc][1] * inj.w.cust_scale[c] * rng.uniform(*mults)
        inj.emit(t, c, m, amount_cents(rng, mean, 0.4), lat, lon, dev, False, "ato")
        t += int(rng.integers(5 * 60, 3 * HOUR))


def _high_value(inj: _Injector, day0: int) -> None:
    """Emit one or two out-of-pattern big-ticket card-present purchases for a random victim."""
    rng = inj.rng
    t = day0 + int(rng.integers(8 * HOUR, 22 * HOUR))
    c = inj.victim(t)
    home = int(inj.w.cust_loc[c])
    # High-value fraud happens near home half the time and away the other half.
    loc = home if rng.random() < 0.5 else away_location(rng, home)
    for _k in range(int(rng.integers(1, 3))):
        mcc = int(rng.choice(HIGH_VALUE_MCCS))
        m = inj.physical(mcc, loc)
        mean = MCCS[mcc][1] * inj.w.cust_scale[c] * rng.uniform(2.0, 9.0)
        lat, lon = inj.w.merch_latlon[m]
        inj.emit(t, c, m, amount_cents(rng, mean, 0.4), lat, lon, 0, True, "high_value")
        t += int(rng.integers(10 * 60, 3 * HOUR))


def _merchant_compromise(cfg: SimConfig, inj: _Injector, legit: pd.DataFrame) -> None:
    """Skimmed at a breached merchant, cloned card used later at an away location."""
    rng = inj.rng
    # Skimming happens at physical merchants, so candidate victims are card-present shoppers.
    phys = legit[legit["card_present"]]
    counts = phys["merchant_id"].value_counts()
    # Breaches hit busy merchants, where skimming has many victims.
    popular = counts.index.to_numpy()[:POPULAR_MERCHANTS]
    n_breach = min(cfg.compromised_merchants_per_period, len(popular))
    if n_breach == 0:
        return
    # Each period has its own breaches; cloned cards are used weeks later, so labels lag.
    for period in range(cfg.n_periods):
        p0 = period * cfg.period_seconds
        breached = rng.choice(popular, size=n_breach, replace=False)
        for m_b in breached:
            # The breach window is 10 days, starting at a random point in the period.
            w0 = p0 + int(rng.integers(0, 15 * DAY))
            hits = phys[
                (phys["merchant_id"] == m_b) & (phys["ts"] >= w0) & (phys["ts"] < w0 + 10 * DAY)
            ]
            for t_skim, c in zip(
                hits["ts"].to_numpy(), hits["customer_id"].to_numpy(), strict=True
            ):
                # Only 45% of shoppers at a breached merchant are actually skimmed.
                if rng.random() > 0.45:
                    continue
                # Cloned cards are used 5 to 30 days after the skim, not straight away.
                t = int(t_skim) + int(rng.integers(5 * DAY, 30 * DAY))
                # Clones are used away from home, which is the signal the geographic features catch.
                loc = away_location(rng, int(inj.w.cust_loc[c]))
                # A few purchases on the clone, spaced 10 minutes to 3 hours apart.
                for _k in range(int(rng.integers(2, 6))):
                    mcc = int(rng.choice(CLONED_CARD_MCCS))
                    m = inj.physical(mcc, loc)
                    lat, lon = inj.w.merch_latlon[m]
                    mean = MCCS[mcc][1] * rng.uniform(1.5, 4.0)
                    amount = amount_cents(rng, mean, 0.4)
                    inj.emit(t, int(c), m, amount, lat, lon, 0, True, "merchant_compromise")
                    t += int(rng.integers(10 * 60, 3 * HOUR))


def inject_fraud(
    cfg: SimConfig,
    w: World,
    rng: np.random.Generator,
    rows: TxnRows,
    legit: pd.DataFrame,
    devices: DeviceIds,
) -> None:
    """Append card-testing, ATO, high-value and merchant-compromise fraud to ``rows``.

    ``legit`` (genuine transactions only) is where skimming victims are found.
    """
    # One shared injector, so every pattern draws from the same RNG in a fixed order.
    inj = _Injector(cfg, w, rng, rows, devices)
    # Day by day, with patterns in a fixed order, per the module's draw-order invariant.
    for day in range(cfg.total_days):
        day0 = day * DAY
        shifted = day0 >= cfg.shift_start_ts
        for _ in range(rng.poisson(cfg.card_testing_per_day)):
            _card_testing(inj, day0)
        # The shifted period also raises the takeover rate.
        ato_rate = cfg.ato_per_day * (ATO_SHIFT_RATE_MULT if shifted else 1.0)
        for _ in range(rng.poisson(ato_rate)):
            _account_takeover(inj, day0, shifted)
        for _ in range(rng.poisson(cfg.high_value_per_day)):
            _high_value(inj, day0)
    # Runs last: it needs the finished legit table and works period by period.
    _merchant_compromise(cfg, inj, legit)
