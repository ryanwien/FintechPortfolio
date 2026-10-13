"""Payment lifecycle state machine.

    create(capture=False)          capture
          ──────────────▶ pending ─────────▶ posted ──reverse──▶ reversed
                             │
                             └─────fail─────▶ failed

``pending`` holds funds (an authorization): the amount leaves the source's
*available* balance but nothing is written to the journal. ``posted`` writes the
journal entry and releases the hold in the same transaction. ``failed`` releases
the hold. ``reversed`` writes a reversing journal entry. ``failed`` and
``reversed`` are terminal.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from ledger.domain import IllegalTransitionError


class PaymentStatus(StrEnum):
    PENDING = "pending"
    POSTED = "posted"
    FAILED = "failed"
    REVERSED = "reversed"


TRANSITIONS: Final[dict[PaymentStatus, frozenset[PaymentStatus]]] = {
    PaymentStatus.PENDING: frozenset({PaymentStatus.POSTED, PaymentStatus.FAILED}),
    PaymentStatus.POSTED: frozenset({PaymentStatus.REVERSED}),
    PaymentStatus.FAILED: frozenset(),
    PaymentStatus.REVERSED: frozenset(),
}

TERMINAL: Final = frozenset(s for s, nxt in TRANSITIONS.items() if not nxt)


def can_transition(current: PaymentStatus, target: PaymentStatus) -> bool:
    """Whether the state machine allows ``current -> target``."""
    return target in TRANSITIONS[current]


def assert_transition(current: PaymentStatus | str, target: PaymentStatus | str) -> None:
    """Raise :class:`IllegalTransitionError` unless ``current -> target`` is allowed."""
    cur, tgt = PaymentStatus(current), PaymentStatus(target)
    if not can_transition(cur, tgt):
        allowed = sorted(TRANSITIONS[cur]) or ["none (terminal)"]
        raise IllegalTransitionError(
            f"payment cannot move from {cur} to {tgt}; allowed: {', '.join(allowed)}"
        )
