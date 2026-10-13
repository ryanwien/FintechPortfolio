"""Injectable time source.

Every timestamp the ledger writes (``created_at``, ``updated_at``, default
``effective_date``) comes from one :class:`Clock`, so tests can pin time and the
service never calls ``datetime.now`` directly.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime


def utcnow() -> datetime:
    """Timezone-aware current time in UTC."""
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class Clock:
    """Formats the injected time source the way the database stores it."""

    source: Callable[[], datetime] = utcnow

    def timestamp(self) -> str:
        """ISO-8601 timestamp with microseconds, used for audit columns."""
        return self.source().isoformat(timespec="microseconds")

    def today(self) -> str:
        """ISO date, used as the default ``effective_date`` of an entry."""
        return self.source().date().isoformat()
