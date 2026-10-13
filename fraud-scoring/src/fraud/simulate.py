"""Deterministic synthetic card-transaction stream: world -> legit spend -> fraud -> table.

The pieces live in :mod:`fraud.world` (customers, merchants, devices),
:mod:`fraud.legit` (genuine behaviour, including deliberate fraud look-alikes) and
:mod:`fraud.fraud_patterns` (the five injected fraud types). This module wires them together
in a fixed order, which is what makes the stream reproducible from ``SimConfig.seed``.

Invariants of the returned table:

* sorted by ``ts`` with a stable ``txn_id`` equal to the row number;
* amounts are integer cents, time is integer seconds since the simulation epoch;
* every fraud row has a ``label_ts`` (chargeback arrival) after its ``ts``; legit rows have -1.
  Anything trained or scored at time T may only use labels with ``label_ts <= T``.

The first ``warmup_periods`` months exist only to give the streaming features history; the
last period has a seasonal shift in legit spend plus a change in account-takeover tactics.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from fraud.config import SimConfig
from fraud.fraud_patterns import flip_friendly_fraud, inject_fraud
from fraud.legit import generate_legit
from fraud.world import (
    FRAUD_DEVICE_BASE,
    ONE_OFF_DEVICE_BASE,
    DeviceIds,
    TxnRows,
    build_world,
)

__all__ = ["SimConfig", "label_events", "simulate"]

_INT_COLUMNS = ("ts", "amount_cents", "label_ts", "device_id")
_SORT_KEYS = ["ts", "customer_id", "merchant_id"]


def simulate(cfg: SimConfig | None = None) -> pd.DataFrame:
    """Return the full transaction stream sorted by time with a stable ``txn_id``."""
    cfg = cfg or SimConfig()
    rng = np.random.default_rng(cfg.seed)
    world = build_world(cfg, rng)
    rows = TxnRows()
    devices = DeviceIds(ONE_OFF_DEVICE_BASE)
    generate_legit(cfg, world, rng, rows, devices)
    legit = pd.DataFrame(rows.cols)
    devices.next = max(devices.next, FRAUD_DEVICE_BASE)
    flipped = flip_friendly_fraud(cfg, rng, rows, legit)
    inject_fraud(cfg, world, rng, rows, legit[~flipped], devices)

    df = pd.DataFrame(rows.cols)
    df = df.sort_values(_SORT_KEYS, kind="mergesort")
    df = df.reset_index(drop=True)
    df.insert(0, "txn_id", np.arange(len(df), dtype=np.int64))
    for col in _INT_COLUMNS:
        df[col] = df[col].astype(np.int64)
    df["card_present"] = df["card_present"].astype(bool)
    df["is_fraud"] = df["is_fraud"].astype(bool)
    df["period"] = (df["ts"] // cfg.period_seconds).astype(np.int64)
    return df


def label_events(df: pd.DataFrame) -> pd.DataFrame:
    """Chargeback / fraud-report events as they arrive, ordered by ``label_ts``.

    Columns: ``label_ts``, ``txn_id``, ``merchant_id``, ``customer_id``.
    """
    lab = df.loc[df["is_fraud"], ["label_ts", "txn_id", "merchant_id", "customer_id"]]
    return lab.sort_values(["label_ts", "txn_id"], kind="mergesort").reset_index(drop=True)
