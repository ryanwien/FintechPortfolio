"""Hash-chained audit log: append-only, tamper-evident."""

from dataclasses import FrozenInstanceError, replace

import pytest

from aml.audit import AuditLog, OutOfOrderEntryError, entry_digest, verify_chain


def _log() -> AuditLog:
    log = AuditLog()
    log.append(100, "monitoring-engine", "CASE_OPENED", "CASE-0001", score=72.5)
    log.append(105, "case-router", "CASE_ASSIGNED", "CASE-0001", analyst="analyst.kim")
    log.append(400, "analyst.kim", "ESCALATED_L2", "CASE-0001", rationale="structuring")
    log.append(900, "sr.analyst.chen", "SAR_DECISION", "CASE-0001", decision="FILE")
    return log


def test_clean_chain_verifies():
    log = _log()
    res = log.verify()
    assert res.ok
    assert res.entries == 4
    assert log.entries[0].prev_hash == "0" * 64
    pairs = zip(log.entries, log.entries[1:], strict=False)
    assert all(e.prev_hash == p.entry_hash for p, e in pairs)


def test_editing_content_is_detected():
    entries = list(_log().entries)
    entries[2] = replace(entries[2], details=(("rationale", "nothing to see"),))
    res = verify_chain(entries)
    assert not res.ok
    assert res.first_bad_seq == 2
    assert "content" in res.reason


def test_rehashing_an_edit_breaks_the_next_link():
    entries = list(_log().entries)
    e = entries[1]
    det = (("analyst", "someone.else"),)
    forged_hash = entry_digest(e.seq, e.ts, e.actor, e.action, e.case_id, det, e.prev_hash)
    entries[1] = replace(e, details=det, entry_hash=forged_hash)
    res = verify_chain(entries)
    assert not res.ok
    assert res.first_bad_seq == 2
    assert "link" in res.reason


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (lambda es: es[:1] + es[2:], "sequence gap or reorder"),
        (lambda es: [es[1], es[0], *es[2:]], "sequence gap or reorder"),
    ],
    ids=["deletion", "reordering"],
)
def test_deletion_and_reordering_are_detected(mutation, reason):
    res = verify_chain(mutation(list(_log().entries)))
    assert not res.ok
    assert res.reason == reason


def test_entries_are_immutable_and_time_ordered():
    log = _log()
    with pytest.raises(FrozenInstanceError):
        log.entries[0].action = "CASE_DELETED"  # type: ignore[misc]
    assert isinstance(log.entries, tuple)
    with pytest.raises(OutOfOrderEntryError, match="time order"):
        log.append(50, "x", "BACKDATED", "CASE-0001")
