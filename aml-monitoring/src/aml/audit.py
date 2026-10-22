"""Append-only, hash-chained audit trail.

Each entry commits to its own content and the previous entry's hash, so editing,
deleting, inserting or reordering any historical entry breaks verification from that
point on. Entries are frozen dataclasses and the log only exposes a tuple view.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

SHA256_HEX_LEN: Final = 64
GENESIS: Final = "0" * SHA256_HEX_LEN


class OutOfOrderEntryError(ValueError):
    """Raised when an entry would be appended with a timestamp before the last one."""


def entry_digest(
    seq: int,
    ts: int,
    actor: str,
    action: str,
    case_id: str,
    details: tuple[tuple[str, str], ...],
    prev_hash: str,
) -> str:
    """SHA-256 over the canonical JSON of an entry's content and its predecessor's hash."""
    body = json.dumps(
        {
            "seq": seq,
            "ts": ts,
            "actor": actor,
            "action": action,
            "case_id": case_id,
            "details": [list(kv) for kv in details],
            "prev": prev_hash,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(body.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class AuditEntry:
    """One link in the hash chain; ``entry_hash`` covers every field and ``prev_hash``."""

    seq: int
    ts: int
    actor: str
    action: str
    case_id: str
    details: tuple[tuple[str, str], ...]
    prev_hash: str
    entry_hash: str


@dataclass(frozen=True, slots=True)
class VerifyResult:
    """Outcome of a chain check; on failure, the first bad seq and why."""

    ok: bool
    entries: int
    first_bad_seq: int | None = None
    reason: str = ""


class AuditLog:
    """Append-only log; entries must arrive in non-decreasing time order."""

    def __init__(self) -> None:
        self._entries: list[AuditEntry] = []

    def append(
        self, ts: int, actor: str, action: str, case_id: str, **details: object
    ) -> AuditEntry:
        """Chain a new entry; ``details`` are stringified and stored sorted by key."""
        if self._entries and ts < self._entries[-1].ts:
            raise OutOfOrderEntryError("audit entries must be appended in time order")
        seq = len(self._entries)
        prev = self._entries[-1].entry_hash if self._entries else GENESIS
        det = tuple(sorted((str(k), str(v)) for k, v in details.items()))
        entry = AuditEntry(
            seq,
            ts,
            actor,
            action,
            case_id,
            det,
            prev,
            entry_digest(seq, ts, actor, action, case_id, det, prev),
        )
        self._entries.append(entry)
        return entry

    @property
    def entries(self) -> tuple[AuditEntry, ...]:
        """Read-only snapshot of all entries in sequence order."""
        return tuple(self._entries)

    @property
    def head(self) -> str:
        """Hash of the newest entry, or ``GENESIS`` for an empty log."""
        return self._entries[-1].entry_hash if self._entries else GENESIS

    def for_case(self, case_id: str) -> list[AuditEntry]:
        """Entries recorded against ``case_id``, in sequence order."""
        return [e for e in self._entries if e.case_id == case_id]

    def verify(self) -> VerifyResult:
        """Check the whole chain with ``verify_chain``; the log itself is never modified."""
        return verify_chain(self._entries)


def verify_chain(entries: Sequence[AuditEntry]) -> VerifyResult:
    """Recompute every hash and link; report the first entry that does not check out."""
    prev = GENESIS
    last_ts: int | None = None
    for i, e in enumerate(entries):
        if e.seq != i:
            return VerifyResult(False, len(entries), i, "sequence gap or reorder")
        if e.prev_hash != prev:
            return VerifyResult(False, len(entries), e.seq, "broken link to previous entry")
        if e.entry_hash != entry_digest(
            e.seq, e.ts, e.actor, e.action, e.case_id, e.details, e.prev_hash
        ):
            return VerifyResult(False, len(entries), e.seq, "content does not match hash")
        if last_ts is not None and e.ts < last_ts:
            return VerifyResult(False, len(entries), e.seq, "timestamp went backwards")
        prev, last_ts = e.entry_hash, e.ts
    return VerifyResult(True, len(entries))
