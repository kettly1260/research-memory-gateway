"""Durable server-side ledger for hook-captured conversation events.

The ledger is the *source of truth* for bridge-ingested conversations; the
Markdown archive note is a materialized view rendered from it.  That ordering
is what makes the whole path idempotent:

1. events are inserted under a ``PRIMARY KEY (event_id)`` -- a replay can only
   ever produce ``duplicates``, never a second archived message;
2. the note is (re-)rendered from the ledger, so a crash between "ledger commit"
   and "note rewrite" self-heals on the next event instead of duplicating;
3. a replay carrying the same ``event_id`` but different content is reported as
   ``event_id_conflict`` instead of silently overwriting history.

Every event also receives a monotonically increasing ``seq`` from a counter that
is never reset.  The session row records ``last_materialized_seq``, which makes
incremental materialization exact in O(1) -- including after retention pruning
has removed the older rows, where a naive "re-render everything" would silently
truncate the archived note.

SQLite is configured with WAL and a busy timeout so the ingest HTTP handler and
the MCP read path can share the process without lock storms.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS ingest_events (
    event_id                 TEXT PRIMARY KEY,
    seq                      INTEGER NOT NULL DEFAULT 0,
    schema_version           INTEGER NOT NULL,
    source_system            TEXT NOT NULL,
    source_account_namespace TEXT NOT NULL DEFAULT '',
    session_id               TEXT NOT NULL DEFAULT '',
    conversation_id          TEXT NOT NULL DEFAULT '',
    thread_id                TEXT NOT NULL DEFAULT '',
    branch_id                TEXT NOT NULL DEFAULT '',
    message_id               TEXT NOT NULL DEFAULT '',
    turn_id                  TEXT NOT NULL DEFAULT '',
    event_type               TEXT NOT NULL,
    role                     TEXT NOT NULL DEFAULT '',
    content                  TEXT NOT NULL DEFAULT '',
    content_hash             TEXT NOT NULL,
    timestamp                TEXT NOT NULL DEFAULT '',
    metadata_json            TEXT NOT NULL DEFAULT '{}',
    client_id                TEXT NOT NULL DEFAULT '',
    received_at              TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ingest_events_thread
    ON ingest_events(source_system, conversation_id, thread_id, branch_id, seq);
CREATE INDEX IF NOT EXISTS idx_ingest_events_session
    ON ingest_events(session_id);

CREATE TABLE IF NOT EXISTS ingest_sessions (
    session_key              TEXT PRIMARY KEY,
    source_system            TEXT NOT NULL,
    source_account_namespace TEXT NOT NULL DEFAULT '',
    session_id               TEXT NOT NULL DEFAULT '',
    conversation_id          TEXT NOT NULL DEFAULT '',
    thread_id                TEXT NOT NULL DEFAULT '',
    branch_id                TEXT NOT NULL DEFAULT '',
    title                    TEXT NOT NULL DEFAULT '',
    model                    TEXT NOT NULL DEFAULT '',
    last_message_id          TEXT NOT NULL DEFAULT '',
    observed_message_count   INTEGER NOT NULL DEFAULT 0,
    stored_message_count     INTEGER NOT NULL DEFAULT 0,
    ended                    INTEGER NOT NULL DEFAULT 0,
    ended_at                 TEXT NOT NULL DEFAULT '',
    events_pruned            INTEGER NOT NULL DEFAULT 0,
    note_path                TEXT NOT NULL DEFAULT '',
    last_materialized_seq    INTEGER NOT NULL DEFAULT 0,
    materialized_event_count INTEGER NOT NULL DEFAULT 0,
    last_materialized_at     TEXT NOT NULL DEFAULT '',
    updated_at               TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ingest_clients (
    client_id           TEXT PRIMARY KEY,
    source_system       TEXT NOT NULL DEFAULT '',
    first_seen_at       TEXT NOT NULL,
    last_seen_at        TEXT NOT NULL,
    accepted_total      INTEGER NOT NULL DEFAULT 0,
    duplicate_total     INTEGER NOT NULL DEFAULT 0,
    rejected_total      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS ingest_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

COUNTER_KEYS = ("accepted_total", "duplicate_total", "rejected_total")
SEQ_KEY = "next_seq"

EVENT_COLUMNS = (
    "event_id",
    "seq",
    "schema_version",
    "source_system",
    "source_account_namespace",
    "session_id",
    "conversation_id",
    "thread_id",
    "branch_id",
    "message_id",
    "turn_id",
    "event_type",
    "role",
    "content",
    "content_hash",
    "timestamp",
    "metadata_json",
    "client_id",
    "received_at",
)

_INSERT_SQL = (
    f"INSERT INTO ingest_events({', '.join(EVENT_COLUMNS)}) "
    f"VALUES ({', '.join('?' for _ in EVENT_COLUMNS)})"
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class IngestStore:
    """SQLite-backed ingest ledger.

    One connection is held for the lifetime of the store and guarded by a
    re-entrant lock.  Opening and closing a connection per statement is
    surprisingly expensive with WAL (every close checkpoints), and the ingest
    handler performs a dozen small reads and writes per request; reusing the
    connection keeps a single-event ingest in the low milliseconds instead of
    seconds.  ``check_same_thread=False`` plus the lock keeps it safe for the
    ASGI threadpool.
    """

    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 5000) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.busy_timeout_ms = int(busy_timeout_ms)
        self._lock = threading.RLock()
        self._depth = 0
        self._conn: sqlite3.Connection | None = None
        self.initialize()

    # -- connection handling -------------------------------------------------

    def _new_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.path,
            timeout=self.busy_timeout_ms / 1000.0,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _ensure_connection(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = self._new_connection()
        return self._conn

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection, None, None]:
        with self._lock:
            conn = self._ensure_connection()
            self._depth += 1
            try:
                yield conn
                if self._depth == 1:
                    conn.commit()
            except Exception:
                if self._depth == 1:
                    conn.rollback()
                raise
            finally:
                self._depth -= 1

    def initialize(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)

    def close(self) -> None:
        """Release the shared connection (used by tests and shutdown paths)."""
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    # -- events --------------------------------------------------------------

    def get_event(self, event_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM ingest_events WHERE event_id = ?", (event_id,)
            ).fetchone()
        return _decode_row(row) if row is not None else None

    def insert_events(
        self, rows: Sequence[dict[str, Any]]
    ) -> tuple[list[str], list[str], list[tuple[str, str]]]:
        """Insert events in one transaction.

        Returns ``(accepted_ids, duplicate_ids, conflicts)`` where each conflict
        is ``(event_id, stored_content_hash)``.  A conflict means the same
        ``event_id`` was seen before with a *different* content identity; that is
        a client bug or a tampered payload and must never be treated as an ACK.
        """
        accepted: list[str] = []
        duplicates: list[str] = []
        conflicts: list[tuple[str, str]] = []
        if not rows:
            return accepted, duplicates, conflicts

        with self._connect() as conn:
            next_seq = self._read_seq(conn)
            for row in rows:
                event_id = row["event_id"]
                existing = conn.execute(
                    "SELECT content_hash FROM ingest_events WHERE event_id = ?",
                    (event_id,),
                ).fetchone()
                if existing is not None:
                    if existing["content_hash"] == row["content_hash"]:
                        duplicates.append(event_id)
                    else:
                        conflicts.append((event_id, existing["content_hash"]))
                    continue
                next_seq += 1
                conn.execute(
                    _INSERT_SQL,
                    (
                        event_id,
                        next_seq,
                        row["schema_version"],
                        row["source_system"],
                        row.get("source_account_namespace", ""),
                        row.get("session_id", ""),
                        row.get("conversation_id", ""),
                        row.get("thread_id", ""),
                        row.get("branch_id", ""),
                        row.get("message_id", ""),
                        row.get("turn_id", ""),
                        row["event_type"],
                        row.get("role", ""),
                        row.get("content", ""),
                        row["content_hash"],
                        row.get("timestamp", ""),
                        row.get("metadata_json", "{}"),
                        row.get("client_id", ""),
                        row.get("received_at") or utc_now(),
                    ),
                )
                accepted.append(event_id)
            if next_seq > self._read_seq(conn):
                self._write_seq(conn, next_seq)
        return accepted, duplicates, conflicts

    def _read_seq(self, conn: sqlite3.Connection) -> int:
        row = conn.execute("SELECT value FROM ingest_meta WHERE key = ?", (SEQ_KEY,)).fetchone()
        if row is None:
            return 0
        try:
            return int(row["value"])
        except (TypeError, ValueError):
            return 0

    def _write_seq(self, conn: sqlite3.Connection, value: int) -> None:
        conn.execute(
            """
            INSERT INTO ingest_meta(key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (SEQ_KEY, str(int(value))),
        )

    def events_for_thread(
        self,
        *,
        source_system: str,
        conversation_id: str,
        thread_id: str = "",
        branch_id: str = "",
        min_seq_exclusive: int = 0,
    ) -> list[dict[str, Any]]:
        """Events of one thread in archived order, optionally only new ones.

        Ordering is by the event's own ``timestamp`` when present and by
        ``seq`` otherwise, so a replay never reshuffles the rendered transcript.
        """
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM ingest_events
                WHERE source_system = ?
                  AND conversation_id = ?
                  AND thread_id = ?
                  AND branch_id = ?
                  AND seq > ?
                ORDER BY
                  CASE WHEN timestamp = '' THEN 1 ELSE 0 END,
                  timestamp,
                  seq
                """,
                (source_system, conversation_id, thread_id, branch_id, int(min_seq_exclusive)),
            ).fetchall()
        return [_decode_row(row) for row in rows]

    def count_messages(
        self,
        *,
        source_system: str,
        conversation_id: str,
        thread_id: str = "",
        branch_id: str = "",
    ) -> int:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS n FROM ingest_events
                WHERE source_system = ? AND conversation_id = ?
                  AND thread_id = ? AND branch_id = ?
                  AND event_type IN ('user_prompt', 'assistant_message')
                """,
                (source_system, conversation_id, thread_id, branch_id),
            ).fetchone()
        return int(row["n"]) if row is not None else 0

    def count_all_events(
        self,
        *,
        source_system: str,
        conversation_id: str,
        thread_id: str = "",
        branch_id: str = "",
    ) -> int:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS n FROM ingest_events
                WHERE source_system = ? AND conversation_id = ?
                  AND thread_id = ? AND branch_id = ?
                """,
                (source_system, conversation_id, thread_id, branch_id),
            ).fetchone()
        return int(row["n"]) if row is not None else 0

    def last_message_id(
        self,
        *,
        source_system: str,
        conversation_id: str,
        thread_id: str = "",
        branch_id: str = "",
    ) -> str:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT message_id FROM ingest_events
                WHERE source_system = ? AND conversation_id = ?
                  AND thread_id = ? AND branch_id = ?
                  AND message_id != ''
                ORDER BY seq DESC LIMIT 1
                """,
                (source_system, conversation_id, thread_id, branch_id),
            ).fetchone()
        return str(row["message_id"]) if row is not None else ""

    def max_seq(
        self,
        *,
        source_system: str,
        conversation_id: str,
        thread_id: str = "",
        branch_id: str = "",
    ) -> int:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT MAX(seq) AS n FROM ingest_events
                WHERE source_system = ? AND conversation_id = ?
                  AND thread_id = ? AND branch_id = ?
                """,
                (source_system, conversation_id, thread_id, branch_id),
            ).fetchone()
        return int(row["n"]) if row is not None and row["n"] is not None else 0

    # -- sessions ------------------------------------------------------------

    def get_session(self, session_key: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM ingest_sessions WHERE session_key = ?", (session_key,)
            ).fetchone()
        return dict(row) if row is not None else None

    def upsert_session(
        self,
        *,
        session_key: str,
        source_system: str,
        source_account_namespace: str = "",
        session_id: str = "",
        conversation_id: str = "",
        thread_id: str = "",
        branch_id: str = "",
        title: str = "",
        model: str = "",
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO ingest_sessions(
                    session_key, source_system, source_account_namespace, session_id,
                    conversation_id, thread_id, branch_id, title, model, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_key) DO UPDATE SET
                    title = CASE WHEN excluded.title != '' THEN excluded.title
                                 ELSE ingest_sessions.title END,
                    model = CASE WHEN excluded.model != '' THEN excluded.model
                                 ELSE ingest_sessions.model END,
                    updated_at = excluded.updated_at
                """,
                (
                    session_key,
                    source_system,
                    source_account_namespace,
                    session_id,
                    conversation_id,
                    thread_id,
                    branch_id,
                    title,
                    model,
                    utc_now(),
                ),
            )

    def update_session_progress(
        self,
        *,
        session_key: str,
        stored_message_count: int,
        last_materialized_seq: int,
        materialized_event_count: int,
        last_message_id: str = "",
        note_path: str = "",
    ) -> None:
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE ingest_sessions SET
                    stored_message_count = ?,
                    last_materialized_seq = ?,
                    materialized_event_count = ?,
                    last_message_id = CASE WHEN ? != '' THEN ? ELSE last_message_id END,
                    note_path = CASE WHEN ? != '' THEN ? ELSE note_path END,
                    last_materialized_at = ?,
                    updated_at = ?
                WHERE session_key = ?
                """,
                (
                    int(stored_message_count),
                    int(last_materialized_seq),
                    int(materialized_event_count),
                    last_message_id,
                    last_message_id,
                    note_path,
                    note_path,
                    now,
                    now,
                    session_key,
                ),
            )

    def mark_session_ended(
        self,
        *,
        session_key: str,
        ended_at: str = "",
        observed_message_count: int = 0,
        last_message_id: str = "",
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE ingest_sessions SET
                    ended = 1,
                    ended_at = ?,
                    observed_message_count = ?,
                    last_message_id = CASE WHEN ? != '' THEN ? ELSE last_message_id END,
                    updated_at = ?
                WHERE session_key = ?
                """,
                (
                    ended_at or utc_now(),
                    int(observed_message_count),
                    last_message_id,
                    last_message_id,
                    utc_now(),
                    session_key,
                ),
            )

    def reopen_session(self, session_key: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE ingest_sessions SET ended = 0, updated_at = ? WHERE session_key = ?",
                (utc_now(), session_key),
            )

    def mark_session_pruned(self, session_key: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE ingest_sessions SET events_pruned = 1, updated_at = ? WHERE session_key = ?",
                (utc_now(), session_key),
            )

    def sessions_for_thread(
        self,
        *,
        source_system: str,
        conversation_id: str,
        thread_id: str = "",
        branch_id: str = "",
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM ingest_sessions
                WHERE source_system = ? AND conversation_id = ?
                  AND thread_id = ? AND branch_id = ?
                """,
                (source_system, conversation_id, thread_id, branch_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def sessions_for_conversation(
        self, *, source_system: str, conversation_id: str
    ) -> list[dict[str, Any]]:
        """All session rows of one provider conversation, any thread/branch.

        Observability and tests need to find a session without knowing the exact
        ``(thread_id, branch_id)`` pair the adapter chose -- for Codex, the
        conversation id and the thread id are the same value, while a generic
        adapter may leave the thread empty.
        """
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM ingest_sessions
                WHERE source_system = ? AND conversation_id = ?
                ORDER BY updated_at
                """,
                (source_system, conversation_id),
            ).fetchall()
        return [dict(row) for row in rows]

    # -- clients and counters ------------------------------------------------

    def record_client(
        self,
        *,
        client_id: str,
        source_system: str,
        accepted: int,
        duplicates: int,
        rejected: int,
    ) -> None:
        if not client_id:
            return
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO ingest_clients(
                    client_id, source_system, first_seen_at, last_seen_at,
                    accepted_total, duplicate_total, rejected_total
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(client_id) DO UPDATE SET
                    source_system = excluded.source_system,
                    last_seen_at = excluded.last_seen_at,
                    accepted_total = ingest_clients.accepted_total + excluded.accepted_total,
                    duplicate_total = ingest_clients.duplicate_total + excluded.duplicate_total,
                    rejected_total = ingest_clients.rejected_total + excluded.rejected_total
                """,
                (
                    client_id,
                    source_system,
                    now,
                    now,
                    int(accepted),
                    int(duplicates),
                    int(rejected),
                ),
            )

    def bump_counters(self, *, accepted: int = 0, duplicates: int = 0, rejected: int = 0) -> None:
        if not (accepted or duplicates or rejected):
            return
        with self._connect() as conn:
            for key, delta in (
                ("accepted_total", accepted),
                ("duplicate_total", duplicates),
                ("rejected_total", rejected),
            ):
                if not delta:
                    continue
                conn.execute(
                    """
                    INSERT INTO ingest_meta(key, value) VALUES (?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = CAST(
                        CAST(ingest_meta.value AS INTEGER) + CAST(excluded.value AS INTEGER) AS TEXT
                    )
                    """,
                    (key, str(int(delta))),
                )
            conn.execute(
                """
                INSERT INTO ingest_meta(key, value) VALUES ('last_ingest_at', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (utc_now(),),
            )

    def counters(self) -> dict[str, Any]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT key, value FROM ingest_meta WHERE key != ?", (SEQ_KEY,)
            ).fetchall()
        data: dict[str, Any] = {key: 0 for key in COUNTER_KEYS}
        data["last_ingest_at"] = ""
        for row in rows:
            if row["key"] in COUNTER_KEYS:
                try:
                    data[row["key"]] = int(row["value"])
                except (TypeError, ValueError):
                    data[row["key"]] = 0
            elif row["key"] == "last_ingest_at":
                data[row["key"]] = row["value"]
        return data

    def client_distribution(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT client_id, source_system, last_seen_at, accepted_total,
                       duplicate_total, rejected_total
                FROM ingest_clients
                ORDER BY last_seen_at DESC LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        return [dict(row) for row in rows]

    def source_distribution(self) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT source_system, COUNT(*) AS n FROM ingest_events
                GROUP BY source_system ORDER BY n DESC
                """
            ).fetchall()
        return {str(row["source_system"]): int(row["n"]) for row in rows}

    def pending_summary(self) -> dict[str, Any]:
        with self._connect() as conn:
            events = conn.execute("SELECT COUNT(*) AS n FROM ingest_events").fetchone()["n"]
            sessions = conn.execute("SELECT COUNT(*) AS n FROM ingest_sessions").fetchone()["n"]
            open_sessions = conn.execute(
                "SELECT COUNT(*) AS n FROM ingest_sessions WHERE ended = 0"
            ).fetchone()["n"]
        return {
            "stored_events": int(events),
            "sessions": int(sessions),
            "open_sessions": int(open_sessions),
        }

    def stats(self) -> dict[str, Any]:
        return {
            **self.counters(),
            **self.pending_summary(),
            "source_distribution": self.source_distribution(),
            "recent_clients": self.client_distribution(limit=10),
        }

    # -- retention -----------------------------------------------------------

    def prune(self, *, retention_days: int) -> dict[str, int]:
        """Delete ACKed events of ended sessions older than the retention window.

        The materialized Markdown note is the durable archive and is never
        touched here.  Because materialization is driven by the monotonic
        ``seq`` cursor rather than by "re-render the whole ledger", pruning can
        never truncate an already-archived note.
        """
        if retention_days <= 0:
            return {"pruned_events": 0, "pruned_sessions": 0}
        cutoff = (datetime.now(UTC) - timedelta(days=int(retention_days))).isoformat()
        pruned_events = 0
        pruned_sessions = 0
        with self._connect() as conn:
            stale = conn.execute(
                """
                SELECT session_key, source_system, conversation_id, thread_id, branch_id
                FROM ingest_sessions
                WHERE ended = 1 AND updated_at < ?
                """,
                (cutoff,),
            ).fetchall()
            for row in stale:
                cursor = conn.execute(
                    """
                    DELETE FROM ingest_events
                    WHERE source_system = ? AND conversation_id = ?
                      AND thread_id = ? AND branch_id = ?
                    """,
                    (
                        row["source_system"],
                        row["conversation_id"],
                        row["thread_id"],
                        row["branch_id"],
                    ),
                )
                pruned_events += int(cursor.rowcount or 0)
                conn.execute(
                    "UPDATE ingest_sessions SET events_pruned = 1 WHERE session_key = ?",
                    (row["session_key"],),
                )
                pruned_sessions += 1
        return {"pruned_events": pruned_events, "pruned_sessions": pruned_sessions}


def metadata_json(metadata: dict[str, Any] | None) -> str:
    return json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def metadata_bytes(metadata: dict[str, Any] | None) -> int:
    return len(metadata_json(metadata).encode("utf-8"))


def _decode_row(row: sqlite3.Row) -> dict[str, Any]:
    """Decode one stored event row, exposing ``metadata`` as a parsed mapping.

    Downstream rendering expects a ``metadata`` dict, while the column is a
    JSON string; decoding here keeps that detail out of every caller.
    """
    data = dict(row)
    raw = data.get("metadata_json") or "{}"
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError):
        decoded = {}
    data["metadata"] = decoded if isinstance(decoded, dict) else {}
    return data
