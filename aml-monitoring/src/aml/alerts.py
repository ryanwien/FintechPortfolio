"""Alert value type and the two merge steps every detector shares.

* ``episodes``: within one detector, raw hits whose time spans overlap become one episode.
* ``dedupe_alerts``: across a run, alerts of the same rule and customer whose windows
  overlap or sit within ``gap_days`` merge into one, then ids ``ALR-00001..`` are stamped
  in (start, customer, rule) order.

Alert scores are clamped to ``[0, MAX_ALERT_SCORE]`` and rounded to 4 places.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Final

from .model import DAY, Txn

MAX_ALERT_SCORE: Final = 1.0
SCORE_DECIMALS: Final = 4

type Details = dict[str, Any]
type Hit = tuple[list[Txn], Details]
"""One raw detector hit: the evidencing transactions and what the detector measured."""


@dataclass(frozen=True, slots=True)
class Alert:
    """A detector alert: one customer, one rule, one time span, and the txns that evidence it.

    ``score`` is the signal strength in [0, 1]. ``alert_id`` stays empty until
    ``dedupe_alerts`` stamps it. ``details`` is excluded from equality comparisons.
    """

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
        # Clamp before rounding so the stored score always sits in [0, MAX_ALERT_SCORE].
        round(min(MAX_ALERT_SCORE, score), SCORE_DECIMALS),
        tuple(t.txn_id for t in txns),
        details,
        version,
    )


def episodes(hits: list[Hit]) -> list[tuple[list[Txn], list[Details]]]:
    """Merge hit windows that overlap in time into episodes.

    Returns (deduped txns in time order, infos of the merged hits) per episode.
    """
    # Sort by start, then end, so overlapping windows become adjacent in one pass.
    spans = sorted(
        ((min(t.ts for t in h), max(t.ts for t in h), h, info) for h, info in hits),
        key=lambda s: (s[0], s[1]),
    )
    # Each episode keeps txns keyed by id, so a txn shared by overlapping hits is counted once.
    merged: list[tuple[int, int, dict[str, Txn], list[Details]]] = []
    for lo, hi, h, info in spans:
        # Overlap test: a hit that starts on or before the open episode's end extends it.
        if merged and lo <= merged[-1][1]:
            m_lo, m_hi, m_txns, m_infos = merged[-1]
            m_txns.update({t.txn_id: t for t in h})
            m_infos.append(info)
            # Keep the later of the two ends so a long earlier hit is never shortened.
            merged[-1] = (m_lo, max(m_hi, hi), m_txns, m_infos)
        else:
            # No overlap with the open episode, so this hit starts a new one.
            merged.append((lo, hi, {t.txn_id: t for t in h}, [info]))
    # Time-order each episode's txns so the output is stable and readable.
    return [
        (sorted(txns.values(), key=lambda t: (t.ts, t.txn_id)), infos)
        for _, _, txns, infos in merged
    ]


def dedupe_alerts(alerts: list[Alert], gap_days: int) -> list[Alert]:
    """Merge same rule + customer alerts that overlap or are within ``gap_days``; stamp ids.

    The merged alert keeps the stronger alert's score and details (plus ``merged_hits``).
    """
    # Group by (rule, customer): merging across rules or customers would be wrong.
    groups: dict[tuple[str, str], list[Alert]] = {}
    for a in alerts:
        groups.setdefault((a.rule_id, a.customer_id), []).append(a)
    merged: list[Alert] = []
    gap = gap_days * DAY
    # Sorted group keys make the output order deterministic.
    for key in sorted(groups):
        # Walk each group in time order so every alert is compared with the running merged alert.
        items = sorted(groups[key], key=lambda a: (a.ts_start, a.ts_end))
        cur = items[0]
        n = 1
        for a in items[1:]:
            # Merge when the alert starts no later than the current end plus the gap window.
            if a.ts_start <= cur.ts_end + gap:
                # Keep the stronger alert's score and details; the merged span always widens to
                # cover both.
                top = a if a.score > cur.score else cur
                cur = replace(
                    top,
                    ts_start=min(cur.ts_start, a.ts_start),
                    ts_end=max(cur.ts_end, a.ts_end),
                    txn_ids=tuple(sorted(set(cur.txn_ids) | set(a.txn_ids))),
                    # Count how many raw alerts this merged alert now covers, reported as
                    # merged_hits.
                    details=dict(top.details, merged_hits=n + 1),
                )
                n += 1
            else:
                # Gap too large: finish the current alert and start a new one from this item.
                merged.append(cur)
                cur, n = a, 1
        merged.append(cur)
    # Sort by (start, customer, rule) so alert_id numbering is stable for a given input.
    merged.sort(key=lambda a: (a.ts_start, a.customer_id, a.rule_id))
    # IDs are stamped after sorting, which is what makes them reproducible across runs.
    return [replace(a, alert_id=f"ALR-{i:05d}") for i, a in enumerate(merged, start=1)]
