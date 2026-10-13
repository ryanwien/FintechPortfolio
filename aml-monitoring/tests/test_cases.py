"""Alert dedupe, case aggregation, priority queue, analyst workflow."""

from aml.alerts import Alert, dedupe_alerts
from aml.audit import AuditLog
from aml.cases import CaseQueue, build_cases, work_cases
from aml.detect import run_detection
from aml.model import DAY
from aml.synth import WorldBuilder, gen_individual, inject_structuring


def _alert(rule, cid, start_day, end_day, score=0.6, family="structuring", txns=("T1",)):
    return Alert(rule, family, cid, start_day * DAY, end_day * DAY, score, txns, {}, "v")


def _world():
    b = WorldBuilder(21)
    bad = inject_structuring(b, variant="classic", day0=40)
    others = [gen_individual(b, light=True) for _ in range(3)]
    return b.build(), bad, others


def test_dedupe_merges_overlapping_alerts_of_same_rule_and_customer():
    alerts = [
        _alert("STR-01", "C1", 40, 45, 0.6, txns=("T1", "T2")),
        _alert("STR-01", "C1", 46, 50, 0.8, txns=("T2", "T3")),  # within 3-day gap
        _alert("STR-01", "C1", 80, 82),  # separate episode
        _alert("VEL-01", "C1", 41, 44, family="velocity"),  # different rule
    ]
    out = dedupe_alerts(alerts, gap_days=3)
    assert len(out) == 3
    merged = next(a for a in out if a.rule_id == "STR-01" and a.ts_start == 40 * DAY)
    assert merged.ts_end == 50 * DAY
    assert merged.score == 0.8
    assert merged.txn_ids == ("T1", "T2", "T3")
    assert [a.alert_id for a in out] == ["ALR-00001", "ALR-00002", "ALR-00003"]


def test_cases_aggregate_per_customer_and_per_network(cfg):
    w, bad, (x, y, z) = _world()
    alerts = dedupe_alerts(
        [
            _alert("STR-01", bad, 40, 45),
            _alert("VEL-01", bad, 41, 50, family="velocity"),
            _alert("GRA-FIN", x, 50, 52, family="fan"),
            _alert("GRA-COM", y, 50, 52, family="network"),
            _alert("RMF-01", z, 60, 61, family="rapid_movement", score=0.1),
        ],
        3,
    )
    cases = build_cases(w, alerts, {x: "NET-001", y: "NET-001"}, cfg.scoring)
    by_key = {c.key: c for c in cases}
    assert set(by_key) == {bad, "NET-001", z}
    assert len(by_key[bad].alerts) == 2
    assert by_key[bad].subjects == (bad,)
    assert by_key["NET-001"].subjects == tuple(sorted((x, y)))
    assert by_key[bad].score > by_key[z].score
    assert not by_key[z].above_line
    q = CaseQueue(cases)
    popped = [q.pop() for _ in range(len(q))]
    assert [c.score for c in popped] == sorted((c.score for c in cases), reverse=True)


def test_work_cases_dispositions_and_audit(cfg):
    w, bad, _ = _world()
    det = run_detection(w, cfg)
    cases = build_cases(w, det.alerts, det.network_of(), cfg.scoring)
    assert any(bad in c.subjects and c.above_line for c in cases)
    log = AuditLog()
    work_cases(w, cases, cfg, log)
    assert log.verify().ok
    for c in cases:
        actions = [e.action for e in log.for_case(c.case_id)]
        assert actions[0] == "CASE_OPENED"
        assert actions[-1] in ("CASE_CLOSED", "SUPPRESSED_BELOW_LINE")
        assert actions.count("ALERT_LINKED") == len(c.alerts)
        if c.above_line:
            assert c.disposition in ("SAR_FILED", "NO_SAR", "NO_SAR_L2")
            if "ESCALATED_L2" in actions:
                assert "NARRATIVE_DRAFTED" in actions
                assert c.narrative
        else:
            assert c.disposition == "BELOW_LINE"
