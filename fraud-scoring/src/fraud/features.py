"""Streaming, point-in-time feature engine.

One class, :class:`FeatureEngine`, is the only place features are computed. Training replays
the historical event stream through it (:func:`build_features`); the online scorer feeds it one
request at a time. Because both paths call the same ``process`` method on the same state, there
is no train/serve skew by construction - and ``tests/test_online_batch.py`` checks it anyway.

Point-in-time rule: the features for transaction *t* are computed from state that contains
only events strictly before *t* in stream order (earlier transactions, and chargeback labels
whose ``label_ts <= ts``). The transaction is folded into state *after* its features are read.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Iterator
from typing import Any, Final, Literal, NamedTuple

import numpy as np
import pandas as pd

from fraud.catalog import MCC_INDEX
from fraud.config import DAY, HOUR, WEEK

MERCHANT_WINDOW: Final = 30 * DAY
SMALL_TICKET_CENTS: Final = 500
NO_HISTORY_SECS: Final = 30 * DAY
MERCHANT_LOOKBACK: Final = 60 * DAY  # "new merchant" = not used in the last 60 days
EMA_ALPHA: Final = 0.1
EARTH_RADIUS_KM: Final = 6371.0
MIN_HISTORY_FOR_Z: Final = 3  # amount z-score is 0 until the customer has this many txns
MIN_AMOUNT_STD: Final = 0.25  # floor on the log-amount std so z-scores stay bounded
AMOUNT_VS_EMA_CAP: Final = 50.0
MIN_GAP_HOURS: Final = 1.0 / 60.0  # one minute: avoids infinite speed on same-second txns
SPEED_CAP_KMH: Final = 5_000.0
MERCHANT_CB_PRIOR_TXNS: Final = 20.0  # shrinks chargeback rate of low-volume merchants

FEATURES: Final[list[str]] = [
    "amount_log",
    "amount_z",
    "amount_vs_ema",
    "cnt_7d",
    "cnt_1h",
    "cnt_24h",
    "small_cnt_1h",
    "amt_sum_24h_log",
    "secs_since_last_log",
    "km_from_prev",
    "speed_kmh",
    "km_from_centroid",
    "new_merchant",
    "new_device",
    "device_n_customers",
    "card_present",
    "hour",
    "merchant_cb_rate_30d",
    "mcc",
]
F_INDEX: Final = {f: i for i, f in enumerate(FEATURES)}
CATEGORICAL: Final = ["mcc"]

EventKind = Literal["txn", "label"]


class Txn(NamedTuple):
    """One authorization request, exactly the fields the online scorer receives."""

    txn_id: int
    ts: int
    customer_id: int
    merchant_id: int
    mcc: int
    amount_cents: int
    lat: float
    lon: float
    device_id: int
    card_present: bool


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in km."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    # The min() guards against float error pushing sqrt(a) just above 1 and breaking asin.
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


class _Customer:
    """Per-customer streaming state (7-day window, running amount stats, last position)."""

    __slots__ = (
        "clat",
        "clon",
        "devices",
        "ema_amt",
        "last_lat",
        "last_lon",
        "last_ts",
        "m2",
        "mean",
        "merchants",
        "n",
        "win",
    )

    def __init__(self) -> None:
        """Start with no history: empty 7-day window, zero amount statistics and no position."""
        self.win: deque[tuple[int, int]] = deque()  # (ts, amount_cents) in last 7 days
        self.n = 0
        self.mean = 0.0  # Welford over log1p(dollars)
        self.m2 = 0.0
        self.ema_amt = 0.0  # recency-weighted typical ticket (cents)
        self.last_ts = -1
        self.last_lat = 0.0
        self.last_lon = 0.0
        self.clat = 0.0
        self.clon = 0.0
        self.merchants: dict[int, int] = {}  # merchant -> last seen ts
        self.devices: set[int] = set()


class _Merchant:
    """Per-merchant 30-day transaction and chargeback-arrival timestamps."""

    __slots__ = ("labels", "txns")

    def __init__(self) -> None:
        """Start with empty 30-day transaction and chargeback-report windows."""
        self.txns: deque[int] = deque()
        self.labels: deque[int] = deque()


class FeatureEngine:
    """Holds per-customer, per-merchant and per-device state; emits features online."""

    def __init__(self) -> None:
        """Start with no customer, merchant or device state."""
        self.customers: dict[int, _Customer] = {}
        self.merchants: dict[int, _Merchant] = {}
        self.device_customers: dict[int, set[int]] = {}
        self.last_event_ts = -1

    # -- labels ------------------------------------------------------------------------------
    def ingest_label(self, merchant_id: int, label_ts: int) -> None:
        """A chargeback/fraud report for a past transaction at ``merchant_id`` arrived."""
        m = self.merchants.get(merchant_id)
        if m is None:
            m = self.merchants[merchant_id] = _Merchant()
        m.labels.append(label_ts)
        self.last_event_ts = max(self.last_event_ts, label_ts)

    # -- transactions ------------------------------------------------------------------------
    def process(self, t: Txn) -> list[float]:
        """Return features for ``t`` using only prior state, then fold ``t`` into state."""
        ts = t.ts
        # Create state for a first-seen customer or merchant so the reads below never miss.
        c = self.customers.get(t.customer_id)
        if c is None:
            c = self.customers[t.customer_id] = _Customer()
        m = self.merchants.get(t.merchant_id)
        if m is None:
            m = self.merchants[t.merchant_id] = _Merchant()

        # ---- read (strictly prior state) ----
        # Everything read below reflects earlier events only; t is folded in at the bottom.
        win = c.win
        # Drop transactions older than 7 days; the deque is time-ordered, so pop from the left.
        while win and win[0][0] <= ts - WEEK:
            win.popleft()
        # What is left is this customer's activity in the last 7 days.
        cnt_7d = len(win)
        cnt_24h = cnt_1h = small_1h = sum_24h = 0
        # Scan newest first and stop at 24h, so cost tracks recent activity, not full history.
        for wts, wamt in reversed(win):
            if wts <= ts - DAY:
                break
            cnt_24h += 1
            sum_24h += wamt
            if wts > ts - HOUR:
                cnt_1h += 1
                if wamt < SMALL_TICKET_CENTS:
                    small_1h += 1

        # Amount features: log of dollars, and a z-score against the customer's running stats.
        dollars = t.amount_cents / 100.0
        x = math.log1p(dollars)
        # Too few transactions make the std meaningless, so the z-score stays 0 until enough.
        if c.n >= MIN_HISTORY_FOR_Z:
            std = math.sqrt(c.m2 / (c.n - 1))
            amount_z = (x - c.mean) / max(std, MIN_AMOUNT_STD)
        else:
            amount_z = 0.0
        amount_vs_ema = t.amount_cents / c.ema_amt if c.ema_amt > 0 else 1.0

        # Movement is measured from the previous transaction; a first one has no position yet.
        if c.last_ts >= 0:
            dt = ts - c.last_ts
            km_prev = haversine_km(c.last_lat, c.last_lon, t.lat, t.lon)
            speed = km_prev / max(dt / HOUR, MIN_GAP_HOURS)
            km_centroid = haversine_km(c.clat, c.clon, t.lat, t.lon)
        else:
            # No earlier position: zero distance and speed, and 30 days as the 'long gap' value.
            dt = NO_HISTORY_SECS
            km_prev = 0.0
            speed = 0.0
            km_centroid = 0.0

        # Known merchant = used by this customer within MERCHANT_LOOKBACK; older use counts as new.
        last_seen = c.merchants.get(t.merchant_id)
        recent = last_seen is not None and ts - last_seen <= MERCHANT_LOOKBACK
        new_merchant = 0.0 if recent else 1.0
        # Device signals only apply to card-not-present traffic that carries a real device id.
        if t.card_present or t.device_id == 0:
            new_device = 0.0
            dev_n = 0.0
        else:
            new_device = 0.0 if t.device_id in c.devices else 1.0
            seen = self.device_customers.get(t.device_id)
            dev_n = float(len(seen - {t.customer_id})) if seen else 0.0

        # Merchant chargeback rate over a 30-day window; expire old entries from both queues first.
        mt, ml = m.txns, m.labels
        while mt and mt[0] <= ts - MERCHANT_WINDOW:
            mt.popleft()
        while ml and ml[0] <= ts - MERCHANT_WINDOW:
            ml.popleft()
        # Only chargebacks that had arrived by ts count, so a later report cannot leak into this.
        n_labels = 0
        for lts in ml:  # labels may be appended out of order relative to ts; count <= ts
            if lts <= ts:
                n_labels += 1
        # The prior in the denominator keeps a merchant with few transactions from an extreme rate.
        cb_rate = n_labels / (len(mt) + MERCHANT_CB_PRIOR_TXNS)

        # Feature vector in FEATURES order; this list and FEATURES must stay in step.
        feats = [
            x,
            amount_z,
            min(amount_vs_ema, AMOUNT_VS_EMA_CAP),
            float(cnt_7d),
            float(cnt_1h),
            float(cnt_24h),
            float(small_1h),
            math.log1p(sum_24h / 100.0),
            math.log1p(min(dt, NO_HISTORY_SECS)),
            km_prev,
            min(speed, SPEED_CAP_KMH),
            km_centroid,
            new_merchant,
            new_device,
            dev_n,
            1.0 if t.card_present else 0.0,
            float((ts // HOUR) % 24),
            cb_rate,
            float(MCC_INDEX.get(t.mcc, -1)),
        ]

        # ---- write (fold the current transaction into state) ----
        # Fold t into state only now, after its features are read, so it never sees itself.
        win.append((ts, t.amount_cents))
        c.n += 1
        # Welford update: running mean and sum of squared deviations, one pass, no stored history.
        delta = x - c.mean
        c.mean += delta / c.n
        c.m2 += delta * (x - c.mean)
        # Recency-weighted (EMA) state so features stay stationary as history grows:
        # running mean for the first 10 txns, then alpha = 0.1.
        # Early on alpha = 1/n is a true running mean; from 10 txns it fixes at EMA_ALPHA.
        alpha = max(1.0 / c.n, EMA_ALPHA)
        c.ema_amt += alpha * (t.amount_cents - c.ema_amt)
        c.clat += alpha * (t.lat - c.clat)
        c.clon += alpha * (t.lon - c.clon)
        # Position state feeds the distance and speed features of this customer's next txn.
        c.last_ts, c.last_lat, c.last_lon = ts, t.lat, t.lon
        c.merchants[t.merchant_id] = ts
        # Record device use for the device features; card-present and no-device rows are skipped.
        if not t.card_present and t.device_id != 0:
            c.devices.add(t.device_id)
            self.device_customers.setdefault(t.device_id, set()).add(t.customer_id)
        mt.append(ts)
        self.last_event_ts = max(self.last_event_ts, ts)
        return feats


_TXN_COLS: Final = list(Txn._fields)


def iter_events(txns: pd.DataFrame, labels: pd.DataFrame | None) -> Iterator[tuple[EventKind, Any]]:
    """Merge transactions and label arrivals into one time-ordered stream.

    Yields ``("txn", Txn)`` or ``("label", (merchant_id, label_ts))``. Ordering: by time; at
    equal time label events come first (a chargeback that arrived at ``ts`` is known when a
    transaction at ``ts`` is scored); transactions keep input order.
    """
    t_ts = txns["ts"].to_numpy()
    t_rows = list(zip(*(txns[c].to_numpy().tolist() for c in _TXN_COLS), strict=True))
    if labels is None or len(labels) == 0:
        for r in t_rows:
            yield "txn", Txn(*r)
        return
    l_ts = labels["label_ts"].to_numpy()
    l_m = labels["merchant_id"].to_numpy()
    # Two-pointer merge of two streams that are each already sorted by time.
    i = j = 0
    n_t, n_l = len(t_rows), len(l_ts)
    while i < n_t:
        # Ties favour the label, so a chargeback arriving at ts is known when ts is scored.
        if j < n_l and l_ts[j] <= t_ts[i]:
            yield "label", (int(l_m[j]), int(l_ts[j]))
            j += 1
        else:
            yield "txn", Txn(*t_rows[i])
            i += 1


def build_features(
    txns: pd.DataFrame, labels: pd.DataFrame | None = None, engine: FeatureEngine | None = None
) -> np.ndarray:
    """Batch feature generation = replaying the stream through the online engine."""
    engine = engine or FeatureEngine()
    out = np.empty((len(txns), len(FEATURES)), dtype=np.float64)
    # Rows are filled in transaction order; label events only update state, no row.
    k = 0
    for kind, ev in iter_events(txns, labels):
        if kind == "txn":
            out[k] = engine.process(ev)
            k += 1
        else:
            engine.ingest_label(*ev)
    return out
