"""SQLite connection management and transaction boundaries.

Owns the only places that issue ``BEGIN``/``COMMIT``/``ROLLBACK``. Write
transactions use ``BEGIN IMMEDIATE``, which takes SQLite's write lock up front, so
every read-check-write sequence inside one is serialized against other writers.

Lock ordering is fixed: the in-process guard (``RLock`` for ``:memory:``, write
mutex for files) is always acquired *before* SQLite's own lock. SQLite's busy
handler polls with sleeps, which turns contention into 100ms+ tail latency; the
mutex hands the lock to the next writer immediately, while ``BEGIN IMMEDIATE``
still protects against writers in *other* processes.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from pathlib import Path
from typing import Final

from ledger.schema import SCHEMA

MEMORY_PATH: Final = ":memory:"
SYNCHRONOUS_MODES: Final = frozenset({"OFF", "NORMAL", "FULL", "EXTRA"})
DEFAULT_SYNCHRONOUS: Final = "FULL"
DEFAULT_BUSY_TIMEOUT_S: Final = 30.0


class Database:
    """Connections to one SQLite database, plus the transaction context manager.

    ``path`` may be ``":memory:"`` (single shared connection, operations
    serialized with a lock; used by property-based tests) or a file path
    (WAL mode, one connection per thread; used by the API and benchmarks).
    """

    def __init__(
        self,
        path: str | Path = MEMORY_PATH,
        *,
        synchronous: str = DEFAULT_SYNCHRONOUS,
        busy_timeout_s: float = DEFAULT_BUSY_TIMEOUT_S,
    ) -> None:
        """Open the database and create the schema.

        Raises ValueError for an unknown ``synchronous`` mode.
        """
        if synchronous.upper() not in SYNCHRONOUS_MODES:
            raise ValueError(f"bad synchronous mode {synchronous!r}")
        self.path = str(path)
        self._synchronous = synchronous.upper()
        self._busy_timeout_s = busy_timeout_s
        self._memory = self.path == MEMORY_PATH
        # Each thread gets its own connection through this local. _all_conns keeps a handle to every
        # one so close() can reach them.
        self._local = threading.local()
        self._all_conns: list[sqlite3.Connection] = []
        self._conns_lock = threading.Lock()
        # In-memory: one connection, so every transaction is serialized here.
        # File: writers queue on an in-process mutex before BEGIN IMMEDIATE.
        self._mem_lock = threading.RLock() if self._memory else None
        self._write_lock = threading.Lock()
        self._mem_conn = self._open() if self._memory else None
        # executescript issues its own COMMIT, so the schema is created before any transaction is
        # opened.
        self.connection().executescript(SCHEMA)

    def _open(self) -> sqlite3.Connection:
        """Open a connection with foreign keys on, WAL for files, and track it for close()."""
        conn = sqlite3.connect(
            self.path,
            isolation_level=None,  # we issue BEGIN/COMMIT ourselves
            check_same_thread=False,
            timeout=self._busy_timeout_s,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        # WAL lets readers run while a writer commits. In-memory databases have no journal file to
        # switch.
        if not self._memory:
            conn.execute("PRAGMA journal_mode = WAL")
        # synchronous is per connection, so every connection sets it, not only the first one.
        conn.execute(f"PRAGMA synchronous = {self._synchronous}")
        with self._conns_lock:
            self._all_conns.append(conn)
        return conn

    def connection(self) -> sqlite3.Connection:
        """The calling thread's connection (the shared one for ``:memory:``)."""
        if self._mem_conn is not None:
            return self._mem_conn
        # Opened on first use per thread. Later calls on the same thread reuse the connection.
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._open()
            self._local.conn = conn
        return conn

    def _guard(self, *, write: bool) -> AbstractContextManager[object]:
        """Return the in-process lock for this mode: the shared RLock for ``:memory:``,
        the write mutex for writes, or a no-op for reads.
        """
        if self._mem_lock is not None:
            return self._mem_lock
        return self._write_lock if write else nullcontext()

    @contextmanager
    def transaction(self, *, write: bool = True) -> Iterator[sqlite3.Connection]:
        """One SQLite transaction: ``BEGIN IMMEDIATE`` for writes, ``BEGIN`` for reads.

        Any exception (including ``BaseException``) rolls back and re-raises;
        otherwise the transaction commits on exit.
        """
        conn = self.connection()
        # In-process lock first, then BEGIN. Waiting threads queue here rather than in SQLite's busy
        # handler, which polls with sleeps.
        with self._guard(write=write):
            # BEGIN IMMEDIATE takes the SQLite write lock now, so the reads and writes in the body
            # cannot interleave with another process's writer.
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            # Reached only when the body did not raise. A failed body has already rolled back and
            # re-raised.
            conn.execute("COMMIT")

    def close(self) -> None:
        """Close every connection opened by any thread."""
        with self._conns_lock:
            for conn in self._all_conns:
                conn.close()
            self._all_conns.clear()
        # Drop the thread-local storage so a later connection() opens a fresh connection rather than
        # reusing a closed one.
        self._local = threading.local()
