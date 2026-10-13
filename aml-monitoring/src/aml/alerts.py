"""Alert value type and the two merge steps every detector shares.

* ``episodes``: within one detector, raw hits whose time spans overlap become one episode.
* ``dedupe_alerts``: across a run, alerts of the same rule and customer whose windows
  overlap or sit within ``gap_days`` merge into one, then ids ``ALR-00001..`` are stamped
  in (start, customer, rule) order.

Alert scores are clamped to ``[0, MAX_ALERT_SCORE]`` and rounded to 4 places.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Final, TypeAlias

from .model import DAY, Txn

MAX_ALERT_SCORE: Final = 1.0
SCORE_DECIMALS: Final = 4

Details: TypeAlias = dict[str, Any]
Hit: TypeAlias = tuple[list[Txn], Details]
"""One raw detector hit: the evidencing transactions and what the detector measured."""


@dataclass(frozen=True, slots=True)
class Alert:
    rule_id: str
    family: str
    customer_id: str
    ts_start: int
    ts_end: int
    score: float  # 0..1 strength of the signal
    txn_ids: tuple[str, ...]
    details: Details = field(default_factory=dict, compare=False)
    rule_version: str = ""
    alert_id: str = ""


def make_alert(
    rule_id: str,
    family: str,
    cid: str,
    txns: list[Txn],
    score: float,
    details: Details,
    version: str,
) -> Alert:
    """Alert spanning ``txns`` (time-ordered) with a clamped, rounded score."""
    return Alert(
        rule_id,
        family,
        cid,
        txns[0].ts,
        txns[-1].ts,
        round(min(MAX_ALERT_SCORE, score), SCORE_DECIMALS),
        tuple(t.txn_id for t in txns),
        details,
        version,
    )


def episodes(hits: list[Hit]) -> list[tuple[list[Txn], list[Details]]]:
    """Merge hit windows that overlap in time into episodes.

    Returns (deduped txns in time order, infos of the merged hits) per episode.
    """
    spans = sorted(
        ((min(t.ts for t in h), max(t.ts for t in h), h, info) for h, info in hits),
        key=lambda s: (s[0], s[1]),
    )
    merged: list[tuple[int, int, dict[str, Txn], list[Details]]] = []
    for lo, hi, h, info in spans:
        if merged and lo <= merged[-1][1]:
            m_lo, m_hi, m_txns, m_infos = merged[-1]
            m_txns.update({t.txn_id: t for t in h})
            m_infos.append(info)
            merged[-1] = (m_lo, max(m_hi, hi), m_txns, m_infos)
        else:
            merged.append((lo, hi, {t.txn_id: t for t in h}, [info]))
    return [
        (sorted(txns.values(), key=lambda t: (t.ts, t.txn_id)), infos)
        for _, _, txns, infos in merged
    ]


def dedupe_alerts(alerts: list[Alert], gap_days: int) -> list[Alert]:
    """Merge same rule + customer alerts that overlap or are within ``gap_days``; stamp ids.

    The merged alert keeps the stronger alert's score and details (plus ``merged_hits``).
    """
    groups: dict[tuple[str, str], list[Alert]] = {}
    for a in alerts:
        groups.setdefault((a.rule_id, a.customer_id), []).append(a)
    merged: list[Alert] = []
    gap = gap_days * DAY
    for key in sorted(groups):
        items = sorted(groups[key], key=lambda a: (a.ts_start, a.ts_end))
        cur = items[0]
        n = 1
        for a in items[1:]:
            if a.ts_start <= cur.ts_end + gap:
                top = a if a.score > cur.score else cur
                cur = replace(
                    top,
                    ts_start=min(cur.ts_start, a.ts_start),
                    ts_end=max(cur.ts_end, a.ts_end),
                    txn_ids=tuple(sorted(set(cur.txn_ids) | set(a.txn_ids))),
                    details=dict(top.details, merged_hits=n + 1),
                )
                n += 1
            else:
                merged.append(cur)
                cur, n = a, 1
        merged.append(cur)
    merged.sort(key=lambda a: (a.ts_start, a.customer_id, a.rule_id))
    return [replace(a, alert_id=f"ALR-{i:05d}") for i, a in enumerate(merged, start=1)]
