"""Legitimate customer behaviour.

Each customer has a home metro, a spending profile (rate, ticket size, MCC mix), favourite
merchants, devices, trips (domestic and abroad), phone upgrades, one-off browsers/devices,
occasional legit big-ticket purchases and in-app micro-purchase bursts. The look-alikes are
deliberate: they are what makes fraud detection hard.

The last (shifted) period tilts spend toward online retail and electronics with bigger
baskets: a seasonal shift that population drift monitoring should notice.
"""

from __future__ import annotations

from typing import Final

import numpy as np

from fraud.catalog import (
    ABROAD,
    BIG_TICKET_MCCS,
    DOMESTIC,
    HOUR_P,
    LOCATIONS,
    MCC_CODES,
    MCC_DIGITAL_GOODS,
    MCC_ELECTRONICS,
    MCC_INDEX,
    MCC_ONLINE_RETAIL,
    MCCS,
)
from fraud.config import DAY, HOUR, SimConfig
from fraud.world import DeviceIds, TxnRows, World, amount_cents

SEASONAL_TILT_P: Final = 0.18  # share of shifted-period txns moved to online/electronics
SEASONAL_BASKET_MULT: Final = 1.3
FAVOURITES_PER_POOL: Final = 3
FAVOURITE_P: Final = 0.75  # share of purchases at one of the customer's favourites
TRIP_ABROAD_P: Final = 0.2
CNP_GEO_NOISE_DEG: Final = 0.03  # IP geolocation noise around the customer's position
MICRO_PRICES_CENTS: Final = (99, 199, 299, 499)


def generate_legit(
    cfg: SimConfig, w: World, rng: np.random.Generator, rows: TxnRows, devices: DeviceIds
) -> None:
    """Append every customer's legitimate transactions to ``rows`` (customer by customer)."""
    total_s = cfg.total_seconds
    shift_start = cfg.shift_start_ts
    online_idx = {MCC_INDEX[int(m)] for m in MCC_CODES if MCCS[int(m)][2]}
    digital = w.merch_online_by_mcc[MCC_INDEX[MCC_DIGITAL_GOODS]]

    for c in range(cfg.n_customers):
        start = int(w.cust_start[c])
        active_days = (total_s - start) / DAY
        n = rng.poisson(w.cust_rate[c] * active_days)
        ts = start + rng.integers(0, max(1, total_s - start), size=n)
        ts = (
            (ts // DAY) * DAY
            + rng.choice(24, size=n, p=HOUR_P) * HOUR
            + rng.integers(0, HOUR, size=n)
        )
        ts = np.sort(ts[ts < total_s])
        n = len(ts)
        mcc_idx = rng.choice(len(MCC_CODES), size=n, p=w.cust_mcc_p[c])
        mult = np.ones(n)
        # Seasonal shift in the last period: more online / electronics, bigger baskets.
        shifted = ts >= shift_start
        tilt = shifted & (rng.random(n) < SEASONAL_TILT_P)
        mcc_idx[tilt] = rng.choice(
            [MCC_INDEX[MCC_ONLINE_RETAIL], MCC_INDEX[MCC_ELECTRONICS]], size=int(tilt.sum())
        )
        mult[shifted] = SEASONAL_BASKET_MULT
        # Legit big-ticket purchases (look like "high_value" fraud).
        p_big = cfg.big_ticket_per_month * (active_days / cfg.period_days) / max(1, n)
        big = rng.random(n) < p_big
        mcc_idx[big] = [MCC_INDEX[int(rng.choice(BIG_TICKET_MCCS))] for _ in range(big.sum())]
        mult[big] *= rng.uniform(2.5, 10.0, size=int(big.sum()))
        trips = _draw_trips(cfg, w, rng, c, start, total_s, active_days)
        favs: dict[tuple[int, int], np.ndarray] = {}
        u = rng.random((n, 4))
        home_lat, home_lon = w.cust_home[c]
        for i in range(n):
            t = int(ts[i])
            mi = int(mcc_idx[i])
            loc = int(w.cust_loc[c])
            here_lat, here_lon = home_lat, home_lon
            for s, e, dest in trips:
                if s <= t < e:
                    loc = dest
                    here_lat, here_lon = LOCATIONS[dest][1], LOCATIONS[dest][2]
                    break
            if mi in online_idx:
                pool, key, card_present = w.merch_online_by_mcc[mi], (mi, -1), False
            else:
                pool, key, card_present = w.merch_by_mcc_loc[(mi, loc)], (mi, loc), True
            if key not in favs:
                favs[key] = pool[
                    rng.integers(0, len(pool), size=min(FAVOURITES_PER_POOL, len(pool)))
                ]
            fav = favs[key]
            if u[i, 1] < FAVOURITE_P:
                m = int(fav[int(u[i, 0] * len(fav))])
            else:
                m = int(pool[int(u[i, 2] * len(pool))])
            mean = MCCS[int(MCC_CODES[mi])][1] * w.cust_scale[c] * mult[i]
            amount = amount_cents(rng, mean)
            if card_present:
                lat, lon = w.merch_latlon[m]
                device = 0
            else:
                lat = here_lat + rng.normal(0, CNP_GEO_NOISE_DEG)
                lon = here_lon + rng.normal(0, CNP_GEO_NOISE_DEG)
                # A one-off device is a guest checkout / new browser / work laptop.
                one_off = u[i, 3] < cfg.one_off_device_p
                device = devices.new() if one_off else w.device_at(c, t)
            rows.add(
                ts=t,
                customer_id=c,
                merchant_id=m,
                mcc=int(MCC_CODES[mi]),
                amount_cents=amount,
                lat=float(lat),
                lon=float(lon),
                device_id=device,
                card_present=card_present,
                is_fraud=False,
                fraud_type="",
                label_ts=-1,
            )
        # In-app micro-purchase bursts (look like card testing).
        for _ in range(rng.poisson(cfg.micro_burst_per_month * active_days / cfg.period_days)):
            t = start + int(rng.integers(0, max(1, total_s - start)))
            m = int(rng.choice(digital))
            dev = w.device_at(c, t)
            for _k in range(int(rng.integers(2, 6))):
                if t >= total_s:
                    break
                rows.add(
                    ts=t,
                    customer_id=c,
                    merchant_id=m,
                    mcc=MCC_DIGITAL_GOODS,
                    amount_cents=int(rng.choice(list(MICRO_PRICES_CENTS))),
                    lat=float(home_lat + rng.normal(0, CNP_GEO_NOISE_DEG)),
                    lon=float(home_lon + rng.normal(0, CNP_GEO_NOISE_DEG)),
                    device_id=dev,
                    card_present=False,
                    is_fraud=False,
                    fraud_type="",
                    label_ts=-1,
                )
                t += int(rng.integers(60, 15 * 60))


def _draw_trips(
    cfg: SimConfig,
    w: World,
    rng: np.random.Generator,
    c: int,
    start: int,
    total_s: int,
    active_days: float,
) -> list[tuple[int, int, int]]:
    """Trips (start, end, destination): card-present spend happens at the destination."""
    trips = []
    for _ in range(rng.poisson(cfg.trips_per_month * active_days / cfg.period_days)):
        t0 = start + int(rng.integers(0, max(1, total_s - start)))
        length = int(rng.integers(2, 9)) * DAY
        if rng.random() < TRIP_ABROAD_P:
            dest = int(rng.choice(ABROAD))
        else:
            dest = int(rng.choice(DOMESTIC))
            if dest == w.cust_loc[c]:
                dest = (dest + 1) % len(DOMESTIC)
        trips.append((t0, t0 + length, dest))
    return trips
