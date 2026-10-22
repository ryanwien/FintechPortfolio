"""Idempotency records: exactly-once effects under client retries.

A record maps a client key to a fingerprint of (operation, canonical params)
and the JSON response. It is read and written inside the *same* write
transaction as the effect it describes, so a request is applied exactly once
even if the client retries concurrently or the process dies between the write
and the response. Failed operations roll back and store nothing.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from typing import Any

from ledger.domain import (
    MAX_IDEMPOTENCY_KEY_LENGTH,
    IdempotencyConflictError,
    IdempotencyKey,
    ValidationError,
)


def validate_key(idempotency: IdempotencyKey) -> None:
    """Raise ValidationError unless the key is 1 to MAX_IDEMPOTENCY_KEY_LENGTH characters."""
    if not 1 <= len(idempotency.key) <= MAX_IDEMPOTENCY_KEY_LENGTH:
        raise ValidationError(f"Idempotency-Key must be 1-{MAX_IDEMPOTENCY_KEY_LENGTH} characters")


def fingerprint(op: str, params: Mapping[str, Any]) -> str:
    """SHA-256 of the canonical JSON of (operation, params)."""
    blob = json.dumps({"op": op, "params": params}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def stored_response(
    conn: sqlite3.Connection, idempotency: IdempotencyKey, fp: str
) -> dict[str, Any] | None:
    """The response recorded for this key, or ``None`` if the key is new.

    Marks ``idempotency.replayed`` on a hit; raises
    :class:`IdempotencyConflictError` if the key was used for a different request.
    """
    row = conn.execute(
        "SELECT fingerprint, response FROM idempotency_keys WHERE key = ?",
        (idempotency.key,),
    ).fetchone()
    if row is None:
        return None
    if row["fingerprint"] != fp:
        raise IdempotencyConflictError("Idempotency-Key was already used with a different request")
    idempotency.replayed = True
    return json.loads(row["response"])


def record_response(
    conn: sqlite3.Connection,
    idempotency: IdempotencyKey,
    fp: str,
    response: Mapping[str, Any],
    *,
    created_at: str,
) -> None:
    """Store the response for this key.

    Runs inside the caller's write transaction, so the record commits or rolls back with
    the effect it describes.
    """
    conn.execute(
        "INSERT INTO idempotency_keys (key, fingerprint, response, created_at) VALUES (?, ?, ?, ?)",
        (idempotency.key, fp, json.dumps(response), created_at),
    )
