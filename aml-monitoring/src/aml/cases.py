"""Alert -> case management.

* ``build_cases``: one case per customer, or one per network when Louvain grouped the
  customer into a flagged community; scored and split at "the line".
* ``CaseQueue``: priority queue (highest score first, then oldest).
* ``work_cases``: simulated L1/L2/QA analyst workflow with noisy, ground-truth-driven
  decisions; every step is written to the hash-chained audit log.
"""

from __future__ import annotations

import hashlib
import heapq
import random
from dataclasses import dataclass, field
from typing import Any, Final

from .alerts import Alert
from .audit import AuditLog
from .config import MonitoringConfig, ScoringConfig
from .model import DAY, HOUR, World, ts_at
from .narrative import draft_narrative
from .scoring import case_score, priority

CASE_OPEN_HOUR: Final = 9  # cases open the morning after their last alert
NETWORK_KEY_PREFIX: Final = "NET-"
# Event offsets (minutes) that keep audit entries for one case in a stable order.
ALERT_LINK_OFFSET: Final = 1
SUPPRESS_OFFSET: Final = 2
REVIEW_START_OFFSET: Final = 10
ASSIGNMENT_DELAY: Final = 2 * HOUR
# Simulated work durations, whole hours drawn uniformly from these ranges.
L1_REVIEW_HOURS: Final = (3, 9)
L2_DECISION_HOURS: Final = (12, 36)
QA_REVIEW_HOURS: Final = (2, 8)


@dataclass
class Case:
    """A scored case over one customer or one network; mutated as analysts work it."""

    case_id: str
    key: str
    subjects: tuple[str, ...]
    network_id: str | None
    alerts: list[Alert]
    score: float
    breakdown: dict[str, Any]
    priority: str
    opened_ts: int
    above_line: bool
    status: str = "OPEN"
    disposition: str | None = None
    analyst: str | None = None
    narrative: str | None = None
    closed_ts: int | None = None
    history: list[str] = field(default_factory=list)

    @property
    def families(self) -> list[str]:
        """Sorted, de-duplicated rule families across the case's alerts."""
        return sorted({a.family for a in self.alerts})


def build_cases(
    world: World, alerts: list[Alert], network_of: dict[str, str], cfg: ScoringConfig
) -> list[Case]:
    """One case per network (if the customer is in one) else per customer; ids by score."""
    groups: dict[str, list[Alert]] = {}
    for a in alerts:
        groups.setdefault(network_of.get(a.customer_id, a.customer_id), []).append(a)
    drafts = []
    for key, items in groups.items():
        subjects = tuple(sorted({a.customer_id for a in items}))
        kyc = [world.customers[s].kyc_risk for s in subjects]
        score, breakdown = case_score(items, kyc, cfg)
        opened = ts_at(max(a.ts_end for a in items) // DAY + 1, CASE_OPEN_HOUR)
        drafts.append((key, subjects, items, score, breakdown, opened))
    drafts.sort(key=lambda d: (-d[3], d[5], d[0]))
    cases = []
    for i, (key, subjects, items, score, breakdown, opened) in enumerate(drafts, start=1):
        cases.append(
            Case(
                case_id=f"CASE-{i:04d}",
                key=key,
                subjects=subjects,
                network_id=key if key.startswith(NETWORK_KEY_PREFIX) else None,
                alerts=sorted(items, key=lambda a: (a.ts_start, a.alert_id)),
                score=score,
                breakdown=breakdown,
                priority=priority(score, cfg),
                opened_ts=opened,
                above_line=score >= cfg.case_line,
            )
        )
    return cases


class CaseQueue:
    """Max-priority queue: highest score first, ties broken by oldest, then id."""

    def __init__(self, cases: list[Case] | None = None) -> None:
        self._heap: list[tuple[float, int, str, Case]] = []
        for c in cases or []:
            self.push(c)

    def push(self, case: Case) -> None:
        """Add a case; ties on score are broken by ``opened_ts`` and then ``case_id``."""
        heapq.heappush(self._heap, (-case.score, case.opened_ts, case.case_id, case))

    def pop(self) -> Case:
        """Remove and return the highest-priority case; raises ``IndexError`` when empty."""
        return heapq.heappop(self._heap)[-1]

    def __len__(self) -> int:
        return len(self._heap)


L1_ANALYSTS: Final = ("analyst.kim", "analyst.osei", "analyst.patel", "analyst.ruiz")
L2_ANALYST: Final = "sr.analyst.chen"
QA_REVIEWER: Final = "qa.mendes"
CLOSE_REASONS: Final = (
    "activity consistent with documented business profile",
    "source of funds verified with customer documentation",
    "counterparties known and verified; no adverse media",
    "one-off event explained (documented sale / settlement)",
)


def work_cases(world: World, cases: list[Case], cfg: MonitoringConfig, audit: AuditLog) -> None:
    """Run the queue and record every step in the audit log (time-ordered)."""
    rng = random.Random(cfg.analyst.seed)
    truth = world.truth()
    events: list[tuple[int, str, int, str, str, dict[str, object]]] = []

    def ev(ts: int, case: Case, actor: str, action: str, **details: object) -> None:
        """Queue an audit event and log its action on ``case.history``.
        Events reach the audit log only at the end of ``work_cases``, sorted by time.
        """
        events.append((ts, case.case_id, len(events), actor, action, details))
        case.history.append(action)

    for c in sorted(cases, key=lambda c: c.case_id):
        ev(
            c.opened_ts,
            c,
            "monitoring-engine",
            "CASE_OPENED",
            score=c.score,
            priority=c.priority,
            subjects=",".join(c.subjects),
            alerts=len(c.alerts),
            config=cfg.tag(),
        )
        for a in c.alerts:
            ev(
                c.opened_ts + ALERT_LINK_OFFSET,
                c,
                "monitoring-engine",
                "ALERT_LINKED",
                alert_id=a.alert_id,
                rule=a.rule_id,
                score=a.score,
            )
        if not c.above_line:
            c.status, c.disposition, c.closed_ts = (
                "CLOSED",
                "BELOW_LINE",
                c.opened_ts + SUPPRESS_OFFSET,
            )
            ev(
                c.opened_ts + SUPPRESS_OFFSET,
                c,
                "monitoring-engine",
                "SUPPRESSED_BELOW_LINE",
                score=c.score,
                line=cfg.scoring.case_line,
            )

    clocks = dict.fromkeys(L1_ANALYSTS, 0)
    queue = CaseQueue([c for c in cases if c.above_line])
    position = 0
    a_cfg = cfg.analyst
    while queue:
        c = queue.pop()
        position += 1
        analyst = min(L1_ANALYSTS, key=lambda a: (clocks[a], a))
        start = max(clocks[analyst], c.opened_ts + ASSIGNMENT_DELAY)
        c.analyst = analyst
        ev(
            start,
            c,
            "case-router",
            "CASE_ASSIGNED",
            analyst=analyst,
            priority=c.priority,
            queue_position=position,
        )
        ev(start + REVIEW_START_OFFSET, c, analyst, "REVIEW_STARTED")
        done = start + rng.randint(*L1_REVIEW_HOURS) * HOUR
        clocks[analyst] = done
        suspicious = any(s in truth for s in c.subjects)
        p_esc = a_cfg.p_escalate_if_suspicious if suspicious else a_cfg.p_escalate_if_benign
        if rng.random() >= p_esc:
            c.status, c.disposition, c.closed_ts = "CLOSED", "NO_SAR", done
            ev(
                done,
                c,
                analyst,
                "CASE_CLOSED",
                disposition="NO_SAR",
                reason=rng.choice(CLOSE_REASONS),
            )
            continue
        ev(
            done,
            c,
            analyst,
            "ESCALATED_L2",
            rationale="unexplained activity; red flags: " + ", ".join(c.families),
        )
        c.narrative = draft_narrative(world, c)
        digest = hashlib.sha256(c.narrative.encode()).hexdigest()
        ev(done + HOUR, c, "narrative-generator", "NARRATIVE_DRAFTED", sha256=digest)
        l2_ts = done + rng.randint(*L2_DECISION_HOURS) * HOUR
        p_file = a_cfg.p_file_if_suspicious if suspicious else a_cfg.p_file_if_benign
        if rng.random() < p_file:
            ev(l2_ts, c, L2_ANALYST, "SAR_DECISION", decision="FILE", narrative_sha256=digest)
            qa_ts = l2_ts + rng.randint(*QA_REVIEW_HOURS) * HOUR
            ev(qa_ts, c, QA_REVIEWER, "QA_APPROVED", checklist="5W+H complete")
            c.status, c.disposition, c.closed_ts = "CLOSED", "SAR_FILED", qa_ts + HOUR
            ev(qa_ts + HOUR, c, L2_ANALYST, "CASE_CLOSED", disposition="SAR_FILED")
        else:
            ev(l2_ts, c, L2_ANALYST, "SAR_DECISION", decision="DO_NOT_FILE")
            c.status, c.disposition, c.closed_ts = "CLOSED", "NO_SAR_L2", l2_ts + HOUR
            ev(
                l2_ts + HOUR,
                c,
                L2_ANALYST,
                "CASE_CLOSED",
                disposition="NO_SAR_L2",
                reason=rng.choice(CLOSE_REASONS),
            )

    for ts, case_id, _, actor, action, details in sorted(events, key=lambda e: e[:3]):
        audit.append(ts, actor, action, case_id, **details)
