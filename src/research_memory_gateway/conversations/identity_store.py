"""Additive identity database for conversation sources, snapshots and dedup.

v0.2.4 W3.  All v2 tables live next to the existing ``conversation_imports``
ledger in the same archive-local SQLite file.  The legacy table and its rows
are never dropped or rewritten; this module only adds new tables and rows.

Design invariants:

* ``conversation_source_records.source_key`` is the deterministic
  ``srcv1_<sha256>`` platform-scoped identity.
* ``canonical_conversations`` is a logical grouping; merging uses aliases and
  ``merged_into`` pointers, never physical deletion of source records or notes.
* Snapshots record every distinct raw export version seen for a source; repeat
  sightings update ``last_seen_at``/``seen_count`` instead of inserting
  duplicate rows.
* All writes normalize optional identity components to '' (never NULL) so the
  UNIQUE constraint has consistent SQLite semantics.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from .identity import (
    FINGERPRINT_VERSION,
    FINGERPRINT_VERSION_UNHYDRATED,
    ConversationSourceIdentity,
    TranscriptFingerprints,
    canonical_conversation_id_for,
    codex_legacy_namespace_hash,
    normalize_component,
)

V2_SCHEMA_VERSION = 1

EMPTY_COMPONENT = ""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm(value: Any) -> str:
    return normalize_component(value)


def _migrate_time(value: Any) -> str:
    """Best-effort normalization of legacy timestamps; empty stays empty."""
    return _norm(value)


@dataclass(frozen=True)
class SourceRecord:
    source_key: str
    canonical_conversation_id: str
    source_system: str
    source_account_namespace_hash: str
    source_conversation_id: str
    source_thread_id: str
    source_branch_id: str
    output_path: str
    first_seen_archive_sha256: str
    last_seen_archive_sha256: str
    last_source_entry_sha256: str
    normalized_transcript_sha256: str
    ordered_message_hash: str
    message_set_hash: str
    message_count: int
    fingerprint_version: int
    parser_version: str
    schema_version: str
    status: str
    first_seen_at: str
    last_seen_at: str

    def identity(self) -> ConversationSourceIdentity:
        return ConversationSourceIdentity(
            source_system=self.source_system,
            source_account_namespace_hash=self.source_account_namespace_hash,
            source_conversation_id=self.source_conversation_id,
            source_thread_id=self.source_thread_id,
            source_branch_id=self.source_branch_id,
        )


@dataclass(frozen=True)
class SnapshotRecord:
    snapshot_id: int
    source_key: str
    source_archive_sha256: str
    source_entry_sha256: str
    normalized_transcript_sha256: str
    ordered_message_hash: str
    message_set_hash: str
    message_count: int
    fingerprint_version: int
    first_seen_at: str
    last_seen_at: str
    seen_count: int


@dataclass(frozen=True)
class DuplicateCandidate:
    candidate_id: int
    left_source_key: str
    right_source_key: str
    candidate_type: str
    score: float
    evidence_json: str
    status: str
    created_at: str
    reviewed_at: str

    def evidence(self) -> dict[str, Any]:
        try:
            return json.loads(self.evidence_json or "{}")
        except json.JSONDecodeError:
            return {}


@dataclass(frozen=True)
class CanonicalConversation:
    canonical_conversation_id: str
    status: str
    merged_into_id: str
    primary_source_key: str
    created_at: str
    updated_at: str


@dataclass
class MigrationReport:
    legacy_records: int = 0
    source_records_created: int = 0
    source_records_existing: int = 0
    canonical_created: int = 0
    canonical_existing: int = 0
    snapshots_created: int = 0
    snapshots_existing: int = 0
    output_paths_changed: int = 0
    source_key_collisions: int = 0
    canonical_id_collisions: int = 0
    skipped_legacy_ids: list[str] = field(default_factory=list)


class ConversationIdentityStore:
    """Additive v2 identity tables sharing the archive-local manifest SQLite file."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    # -- connection ---------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _init_db(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS identity_schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS canonical_conversations (
                    canonical_conversation_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    merged_into_id TEXT NOT NULL DEFAULT '',
                    primary_source_key TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS conversation_source_records (
                    source_key TEXT PRIMARY KEY,
                    canonical_conversation_id TEXT NOT NULL,
                    source_system TEXT NOT NULL,
                    source_account_namespace_hash TEXT NOT NULL,
                    source_conversation_id TEXT NOT NULL DEFAULT '',
                    source_thread_id TEXT NOT NULL DEFAULT '',
                    source_branch_id TEXT NOT NULL DEFAULT '',
                    output_path TEXT NOT NULL DEFAULT '',
                    first_seen_archive_sha256 TEXT NOT NULL DEFAULT '',
                    last_seen_archive_sha256 TEXT NOT NULL DEFAULT '',
                    last_source_entry_sha256 TEXT NOT NULL DEFAULT '',
                    normalized_transcript_sha256 TEXT NOT NULL DEFAULT '',
                    ordered_message_hash TEXT NOT NULL DEFAULT '',
                    message_set_hash TEXT NOT NULL DEFAULT '',
                    message_count INTEGER NOT NULL DEFAULT 0,
                    fingerprint_version INTEGER NOT NULL DEFAULT 1,
                    parser_version TEXT NOT NULL DEFAULT '',
                    schema_version TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'active',
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    UNIQUE(
                        source_system,
                        source_account_namespace_hash,
                        source_conversation_id,
                        source_thread_id,
                        source_branch_id
                    )
                );

                CREATE TABLE IF NOT EXISTS conversation_source_snapshots (
                    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_key TEXT NOT NULL,
                    source_archive_sha256 TEXT NOT NULL DEFAULT '',
                    source_entry_sha256 TEXT NOT NULL DEFAULT '',
                    normalized_transcript_sha256 TEXT NOT NULL DEFAULT '',
                    ordered_message_hash TEXT NOT NULL DEFAULT '',
                    message_set_hash TEXT NOT NULL DEFAULT '',
                    message_count INTEGER NOT NULL DEFAULT 0,
                    fingerprint_version INTEGER NOT NULL DEFAULT 1,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    seen_count INTEGER NOT NULL DEFAULT 1,
                    UNIQUE(source_key, source_entry_sha256)
                );

                CREATE TABLE IF NOT EXISTS conversation_message_fingerprints (
                    source_key TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    message_fingerprint TEXT NOT NULL,
                    role TEXT NOT NULL,
                    PRIMARY KEY(source_key, ordinal)
                );

                CREATE TABLE IF NOT EXISTS conversation_duplicate_candidates (
                    candidate_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    left_source_key TEXT NOT NULL,
                    right_source_key TEXT NOT NULL,
                    candidate_type TEXT NOT NULL,
                    score REAL NOT NULL DEFAULT 0.0,
                    evidence_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    reviewed_at TEXT NOT NULL DEFAULT '',
                    UNIQUE(left_source_key, right_source_key, candidate_type)
                );

                CREATE TABLE IF NOT EXISTS canonical_aliases (
                    alias_canonical_id TEXT PRIMARY KEY,
                    active_canonical_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT 'manual_confirm_same'
                );

                CREATE TABLE IF NOT EXISTS dedup_decisions (
                    decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    left_source_key TEXT NOT NULL,
                    right_source_key TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    candidate_type TEXT NOT NULL DEFAULT '',
                    decided_at TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT ''
                );
                """
            )
            connection.execute(
                "INSERT INTO identity_schema_meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(V2_SCHEMA_VERSION),),
            )

    # -- canonical conversations ---------------------------------------------

    def ensure_canonical(self, canonical_id: str, *, primary_source_key: str = "") -> bool:
        """Create the canonical row if missing.  Returns True when created."""
        now = _utc_now()
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT primary_source_key FROM canonical_conversations WHERE canonical_conversation_id = ?",
                (canonical_id,),
            ).fetchone()
            if existing is not None:
                if primary_source_key and not existing["primary_source_key"]:
                    connection.execute(
                        "UPDATE canonical_conversations SET primary_source_key = ?, updated_at = ? "
                        "WHERE canonical_conversation_id = ?",
                        (primary_source_key, now, canonical_id),
                    )
                return False
            connection.execute(
                """
                INSERT INTO canonical_conversations(
                    canonical_conversation_id, created_at, updated_at, status,
                    merged_into_id, primary_source_key
                ) VALUES (?, ?, ?, 'active', '', ?)
                """,
                (canonical_id, now, now, _norm(primary_source_key)),
            )
            return True

    def get_canonical(self, canonical_id: str) -> CanonicalConversation | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM canonical_conversations WHERE canonical_conversation_id = ?",
                (canonical_id,),
            ).fetchone()
        return _canonical_from_row(row) if row is not None else None

    def resolve_canonical(self, canonical_id: str) -> CanonicalConversation | None:
        """Resolve a canonical id through any alias/merge chain to the active one."""
        with self._connect() as connection:
            return self._resolve_canonical_in_conn(connection, canonical_id)

    def _resolve_canonical_in_conn(
        self, connection: sqlite3.Connection, canonical_id: str
    ) -> CanonicalConversation | None:
        seen: set[str] = set()
        current = normalize_component(canonical_id)
        while current and current not in seen:
            seen.add(current)
            row = connection.execute(
                "SELECT * FROM canonical_conversations WHERE canonical_conversation_id = ?",
                (current,),
            ).fetchone()
            if row is None:
                alias = connection.execute(
                    "SELECT active_canonical_id FROM canonical_aliases WHERE alias_canonical_id = ?",
                    (current,),
                ).fetchone()
                if alias is None:
                    return None
                current = alias["active_canonical_id"]
                continue
            canonical = _canonical_from_row(row)
            if canonical.status == "merged" and canonical.merged_into_id:
                current = canonical.merged_into_id
                continue
            return canonical
        return None

    # -- source records -------------------------------------------------------

    def get_source_record(self, source_key: str) -> SourceRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM conversation_source_records WHERE source_key = ?",
                (source_key,),
            ).fetchone()
        return _source_record_from_row(row) if row is not None else None

    def find_source_record_by_identity(
        self, identity: ConversationSourceIdentity
    ) -> SourceRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM conversation_source_records
                WHERE source_system = ?
                  AND source_account_namespace_hash = ?
                  AND source_conversation_id = ?
                  AND source_thread_id = ?
                  AND source_branch_id = ?
                """,
                (
                    _norm(identity.source_system).lower(),
                    _norm(identity.source_account_namespace_hash),
                    _norm(identity.source_conversation_id),
                    _norm(identity.source_thread_id),
                    _norm(identity.source_branch_id),
                ),
            ).fetchone()
        return _source_record_from_row(row) if row is not None else None

    def find_source_records_by_conversation(self, conversation_id: str) -> list[SourceRecord]:
        """All active source records sharing one bare provider conversation id."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM conversation_source_records
                WHERE source_conversation_id = ? AND status = 'active'
                ORDER BY first_seen_at, source_key
                """,
                (_norm(conversation_id),),
            ).fetchall()
        return [_source_record_from_row(row) for row in rows]

    def list_source_records(
        self,
        *,
        source_system: str | None = None,
        status: str = "active",
    ) -> list[SourceRecord]:
        query = "SELECT * FROM conversation_source_records WHERE status = ?"
        params: list[Any] = [status]
        if source_system:
            query += " AND source_system = ?"
            params.append(_norm(source_system).lower())
        query += " ORDER BY first_seen_at, source_key"
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [_source_record_from_row(row) for row in rows]

    def create_source_record(
        self,
        identity: ConversationSourceIdentity,
        *,
        fingerprints: TranscriptFingerprints,
        output_path: str,
        archive_sha256: str,
        source_entry_sha256: str,
        parser_version: str,
        schema_version: str,
        canonical_id: str | None = None,
        first_seen_at: str | None = None,
    ) -> SourceRecord:
        now = first_seen_at or _utc_now()
        source_key = identity.source_key
        canonical = canonical_id or canonical_conversation_id_for(source_key)
        if self.get_source_record(source_key) is not None:
            raise ValueError(f"source record already exists: {source_key}")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO conversation_source_records(
                    source_key, canonical_conversation_id, source_system,
                    source_account_namespace_hash, source_conversation_id,
                    source_thread_id, source_branch_id, output_path,
                    first_seen_archive_sha256, last_seen_archive_sha256,
                    last_source_entry_sha256, normalized_transcript_sha256,
                    ordered_message_hash, message_set_hash, message_count,
                    fingerprint_version, parser_version, schema_version,
                    status, first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
                """,
                (
                    source_key,
                    canonical,
                    _norm(identity.source_system).lower(),
                    _norm(identity.source_account_namespace_hash),
                    _norm(identity.source_conversation_id),
                    _norm(identity.source_thread_id),
                    _norm(identity.source_branch_id),
                    _norm(output_path),
                    _norm(archive_sha256),
                    _norm(archive_sha256),
                    _norm(source_entry_sha256),
                    fingerprints.normalized_transcript_sha256,
                    fingerprints.ordered_message_hash,
                    fingerprints.message_set_hash,
                    int(fingerprints.message_count),
                    int(fingerprints.fingerprint_version),
                    _norm(parser_version),
                    _norm(schema_version),
                    now,
                    now,
                ),
            )
        self.ensure_canonical(canonical, primary_source_key=source_key)
        self.write_message_fingerprints(source_key, fingerprints)
        self.record_snapshot(
            source_key,
            archive_sha256=archive_sha256,
            source_entry_sha256=source_entry_sha256,
            fingerprints=fingerprints,
        )
        record = self.get_source_record(source_key)
        assert record is not None
        return record

    def hydrate_source_record(
        self,
        source_key: str,
        *,
        fingerprints: TranscriptFingerprints,
        archive_sha256: str,
        source_entry_sha256: str,
        parser_version: str = "",
        schema_version: str = "",
    ) -> bool:
        """Fill in fingerprints for a migrated-but-unhydrated record.

        Metadata-only: never rewrites Markdown, never touches the output
        path.  Runs once (version 0 -> versioned); a repeat call on an
        already-hydrated record is a safe no-op and never regresses.
        """
        now = _utc_now()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT fingerprint_version FROM conversation_source_records WHERE source_key = ?",
                (source_key,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown source record: {source_key}")
            if row["fingerprint_version"] != FINGERPRINT_VERSION_UNHYDRATED:
                return False
            connection.execute(
                """
                UPDATE conversation_source_records SET
                    last_seen_archive_sha256 = ?,
                    last_source_entry_sha256 = ?,
                    normalized_transcript_sha256 = ?,
                    ordered_message_hash = ?,
                    message_set_hash = ?,
                    message_count = ?,
                    fingerprint_version = ?,
                    parser_version = CASE WHEN ? != '' THEN ? ELSE parser_version END,
                    schema_version = CASE WHEN ? != '' THEN ? ELSE schema_version END,
                    last_seen_at = ?
                WHERE source_key = ?
                """,
                (
                    _norm(archive_sha256),
                    _norm(source_entry_sha256),
                    fingerprints.normalized_transcript_sha256,
                    fingerprints.ordered_message_hash,
                    fingerprints.message_set_hash,
                    int(fingerprints.message_count),
                    int(fingerprints.fingerprint_version),
                    _norm(parser_version),
                    _norm(parser_version),
                    _norm(schema_version),
                    _norm(schema_version),
                    now,
                    source_key,
                ),
            )
            # Hydrate the migration-era snapshot row for this entry (its
            # fingerprint fields were recorded as unknown).
            connection.execute(
                """
                UPDATE conversation_source_snapshots SET
                    normalized_transcript_sha256 = ?,
                    ordered_message_hash = ?,
                    message_set_hash = ?,
                    message_count = ?,
                    fingerprint_version = ?
                WHERE source_key = ? AND source_entry_sha256 = ?
                  AND fingerprint_version = ?
                """,
                (
                    fingerprints.normalized_transcript_sha256,
                    fingerprints.ordered_message_hash,
                    fingerprints.message_set_hash,
                    int(fingerprints.message_count),
                    int(fingerprints.fingerprint_version),
                    source_key,
                    _norm(source_entry_sha256),
                    FINGERPRINT_VERSION_UNHYDRATED,
                ),
            )
            connection.execute(
                "DELETE FROM conversation_message_fingerprints WHERE source_key = ?",
                (source_key,),
            )
            connection.executemany(
                """
                INSERT INTO conversation_message_fingerprints(
                    source_key, ordinal, message_fingerprint, role
                ) VALUES (?, ?, ?, ?)
                """,
                [
                    (source_key, ordinal, fp, role)
                    for ordinal, (fp, role) in enumerate(
                        zip(fingerprints.message_fingerprints, fingerprints.message_roles)
                    )
                ],
            )
        return True

    def update_source_content(
        self,
        source_key: str,
        *,
        fingerprints: TranscriptFingerprints,
        archive_sha256: str,
        source_entry_sha256: str,
        parser_version: str = "",
        schema_version: str = "",
    ) -> None:
        """Adopt a new export version for a known source (continuation path)."""
        now = _utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE conversation_source_records SET
                    last_seen_archive_sha256 = ?,
                    last_source_entry_sha256 = ?,
                    normalized_transcript_sha256 = ?,
                    ordered_message_hash = ?,
                    message_set_hash = ?,
                    message_count = ?,
                    fingerprint_version = ?,
                    parser_version = CASE WHEN ? != '' THEN ? ELSE parser_version END,
                    schema_version = CASE WHEN ? != '' THEN ? ELSE schema_version END,
                    last_seen_at = ?
                WHERE source_key = ?
                """,
                (
                    _norm(archive_sha256),
                    _norm(source_entry_sha256),
                    fingerprints.normalized_transcript_sha256,
                    fingerprints.ordered_message_hash,
                    fingerprints.message_set_hash,
                    int(fingerprints.message_count),
                    int(fingerprints.fingerprint_version),
                    _norm(parser_version),
                    _norm(parser_version),
                    _norm(schema_version),
                    _norm(schema_version),
                    now,
                    source_key,
                ),
            )
        self.write_message_fingerprints(source_key, fingerprints)

    def mark_seen(
        self,
        source_key: str,
        *,
        archive_sha256: str,
        last_source_entry_sha256: str | None = None,
    ) -> None:
        """Refresh last_seen metadata without changing stored content hashes."""
        now = _utc_now()
        with self._connect() as connection:
            if last_source_entry_sha256 is None:
                connection.execute(
                    "UPDATE conversation_source_records SET last_seen_archive_sha256 = ?, last_seen_at = ? "
                    "WHERE source_key = ?",
                    (_norm(archive_sha256), now, source_key),
                )
            else:
                connection.execute(
                    "UPDATE conversation_source_records SET last_seen_archive_sha256 = ?, "
                    "last_source_entry_sha256 = ?, last_seen_at = ? WHERE source_key = ?",
                    (_norm(archive_sha256), _norm(last_source_entry_sha256), now, source_key),
                )

    def set_output_path(self, source_key: str, output_path: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE conversation_source_records SET output_path = ? WHERE source_key = ?",
                (_norm(output_path), source_key),
            )

    def source_key_collision_count(self) -> int:
        """Rows sharing an identical identity tuple under different source keys."""
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) FROM (
                    SELECT source_system, source_account_namespace_hash,
                           source_conversation_id, source_thread_id, source_branch_id,
                           COUNT(*) AS n
                    FROM conversation_source_records
                    GROUP BY 1, 2, 3, 4, 5
                    HAVING COUNT(*) > 1
                )
                """
            ).fetchone()
        return int(row[0])

    # -- snapshots -------------------------------------------------------------

    def record_snapshot(
        self,
        source_key: str,
        *,
        archive_sha256: str,
        source_entry_sha256: str,
        fingerprints: TranscriptFingerprints,
    ) -> bool:
        """Insert or refresh one export-version sighting.  True when inserted.

        An existing snapshot row recorded as unhydrated (fingerprint_version
        0) gets its fingerprint fields backfilled from the incoming, verifiably
        identical entry -- this is what makes migration-era snapshots catch up
        instead of staying empty forever.
        """
        now = _utc_now()
        with self._connect() as connection:
            existing = connection.execute(
                """
                SELECT snapshot_id, fingerprint_version FROM conversation_source_snapshots
                WHERE source_key = ? AND source_entry_sha256 = ?
                """,
                (source_key, _norm(source_entry_sha256)),
            ).fetchone()
            if existing is not None:
                if existing["fingerprint_version"] == FINGERPRINT_VERSION_UNHYDRATED:
                    connection.execute(
                        """
                        UPDATE conversation_source_snapshots
                        SET normalized_transcript_sha256 = ?,
                            ordered_message_hash = ?,
                            message_set_hash = ?,
                            message_count = ?,
                            fingerprint_version = ?,
                            last_seen_at = ?,
                            source_archive_sha256 = ?
                        WHERE snapshot_id = ?
                        """,
                        (
                            fingerprints.normalized_transcript_sha256,
                            fingerprints.ordered_message_hash,
                            fingerprints.message_set_hash,
                            int(fingerprints.message_count),
                            int(fingerprints.fingerprint_version),
                            now,
                            _norm(archive_sha256),
                            existing["snapshot_id"],
                        ),
                    )
                else:
                    connection.execute(
                        """
                        UPDATE conversation_source_snapshots
                        SET last_seen_at = ?, seen_count = seen_count + 1,
                            source_archive_sha256 = ?
                        WHERE snapshot_id = ?
                        """,
                        (now, _norm(archive_sha256), existing["snapshot_id"]),
                    )
                return False
            connection.execute(
                """
                INSERT INTO conversation_source_snapshots(
                    source_key, source_archive_sha256, source_entry_sha256,
                    normalized_transcript_sha256, ordered_message_hash,
                    message_set_hash, message_count, fingerprint_version,
                    first_seen_at, last_seen_at, seen_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    source_key,
                    _norm(archive_sha256),
                    _norm(source_entry_sha256),
                    fingerprints.normalized_transcript_sha256,
                    fingerprints.ordered_message_hash,
                    fingerprints.message_set_hash,
                    int(fingerprints.message_count),
                    int(fingerprints.fingerprint_version),
                    now,
                    now,
                ),
            )
            return True

    def list_snapshots(self, source_key: str) -> list[SnapshotRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM conversation_source_snapshots WHERE source_key = ? ORDER BY first_seen_at, snapshot_id",
                (source_key,),
            ).fetchall()
        return [
            SnapshotRecord(
                snapshot_id=row["snapshot_id"],
                source_key=row["source_key"],
                source_archive_sha256=row["source_archive_sha256"],
                source_entry_sha256=row["source_entry_sha256"],
                normalized_transcript_sha256=row["normalized_transcript_sha256"],
                ordered_message_hash=row["ordered_message_hash"],
                message_set_hash=row["message_set_hash"],
                message_count=row["message_count"],
                fingerprint_version=row["fingerprint_version"],
                first_seen_at=row["first_seen_at"],
                last_seen_at=row["last_seen_at"],
                seen_count=row["seen_count"],
            )
            for row in rows
        ]

    # -- message fingerprints ----------------------------------------------------

    def write_message_fingerprints(self, source_key: str, fingerprints: TranscriptFingerprints) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM conversation_message_fingerprints WHERE source_key = ?",
                (source_key,),
            )
            connection.executemany(
                """
                INSERT INTO conversation_message_fingerprints(
                    source_key, ordinal, message_fingerprint, role
                ) VALUES (?, ?, ?, ?)
                """,
                [
                    (source_key, ordinal, fp, role)
                    for ordinal, (fp, role) in enumerate(
                        zip(fingerprints.message_fingerprints, fingerprints.message_roles)
                    )
                ],
            )

    def read_message_fingerprints(self, source_key: str) -> list[tuple[int, str, str]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT ordinal, message_fingerprint, role
                FROM conversation_message_fingerprints
                WHERE source_key = ? ORDER BY ordinal
                """,
                (source_key,),
            ).fetchall()
        return [(int(row["ordinal"]), row["message_fingerprint"], row["role"]) for row in rows]

    def message_fingerprint_sequence(self, source_key: str) -> list[str]:
        return [fp for _, fp, _ in self.read_message_fingerprints(source_key)]

    # -- duplicate candidates -----------------------------------------------------

    def get_candidate(self, candidate_id: int) -> DuplicateCandidate | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM conversation_duplicate_candidates WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
        return _candidate_from_row(row) if row is not None else None

    def find_candidate(
        self, left_source_key: str, right_source_key: str, candidate_type: str
    ) -> DuplicateCandidate | None:
        left, right = sorted([left_source_key, right_source_key])
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM conversation_duplicate_candidates
                WHERE left_source_key = ? AND right_source_key = ? AND candidate_type = ?
                """,
                (left, right, candidate_type),
            ).fetchone()
        return _candidate_from_row(row) if row is not None else None

    def upsert_candidate(
        self,
        left_source_key: str,
        right_source_key: str,
        candidate_type: str,
        *,
        score: float,
        evidence: dict[str, Any],
    ) -> tuple[DuplicateCandidate, bool]:
        """Create a pending candidate unless one exists.  Returns (candidate, created).

        A pair already recorded as rejected is never resurrected as pending by
        the detector; only an explicit human re-review may change its status.
        """
        left, right = sorted([left_source_key, right_source_key])
        evidence_json = json.dumps(evidence, ensure_ascii=False, sort_keys=True)
        now = _utc_now()
        with self._connect() as connection:
            existing = connection.execute(
                """
                SELECT * FROM conversation_duplicate_candidates
                WHERE left_source_key = ? AND right_source_key = ? AND candidate_type = ?
                """,
                (left, right, candidate_type),
            ).fetchone()
            if existing is not None:
                if existing["status"] == "pending":
                    connection.execute(
                        "UPDATE conversation_duplicate_candidates SET score = ?, evidence_json = ? "
                        "WHERE candidate_id = ?",
                        (float(score), evidence_json, existing["candidate_id"]),
                    )
                    refreshed = connection.execute(
                        "SELECT * FROM conversation_duplicate_candidates WHERE candidate_id = ?",
                        (existing["candidate_id"],),
                    ).fetchone()
                    assert refreshed is not None
                    return _candidate_from_row(refreshed), False
                return _candidate_from_row(existing), False
            cursor = connection.execute(
                """
                INSERT INTO conversation_duplicate_candidates(
                    left_source_key, right_source_key, candidate_type, score,
                    evidence_json, status, created_at, reviewed_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', ?, '')
                """,
                (left, right, candidate_type, float(score), evidence_json, now),
            )
            candidate_id = int(cursor.lastrowid)
        created = self.get_candidate(candidate_id)
        assert created is not None
        return created, True

    def resolve_candidate(
        self,
        candidate_id: int,
        *,
        decision: str,
        note: str = "",
    ) -> DuplicateCandidate:
        if decision not in {"confirmed_same", "rejected"}:
            raise ValueError(f"Unsupported candidate decision: {decision}")
        candidate = self.get_candidate(candidate_id)
        if candidate is None:
            raise KeyError(f"Unknown candidate id: {candidate_id}")
        if candidate.status == decision:
            # Idempotent replay: no duplicate decision row, no state change.
            return candidate
        if candidate.status in {"confirmed_same", "rejected"}:
            raise ValueError(
                f"candidate already decided as {candidate.status}; changing it to "
                f"{decision} requires an explicit reopen flow (not available in v0.2.4)"
            )
        now = _utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE conversation_duplicate_candidates
                SET status = ?, reviewed_at = ? WHERE candidate_id = ?
                """,
                (decision, now, candidate_id),
            )
            connection.execute(
                """
                INSERT INTO dedup_decisions(
                    left_source_key, right_source_key, decision, candidate_type, decided_at, note
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate.left_source_key,
                    candidate.right_source_key,
                    decision,
                    candidate.candidate_type,
                    now,
                    _norm(note),
                ),
            )
        updated = self.get_candidate(candidate_id)
        assert updated is not None
        return updated

    def list_candidates(
        self,
        *,
        status: str | None = None,
        source_system: str | None = None,
        candidate_type: str | None = None,
    ) -> list[DuplicateCandidate]:
        query = """
            SELECT c.* FROM conversation_duplicate_candidates c
        """
        joins = ""
        conditions: list[str] = []
        params: list[Any] = []
        if source_system:
            joins += """
                JOIN conversation_source_records l ON l.source_key = c.left_source_key
                JOIN conversation_source_records r ON r.source_key = c.right_source_key
            """
            conditions.append("(l.source_system = ? OR r.source_system = ?)")
            params.extend([_norm(source_system).lower(), _norm(source_system).lower()])
        if status:
            conditions.append("c.status = ?")
            params.append(_norm(status))
        if candidate_type:
            conditions.append("c.candidate_type = ?")
            params.append(_norm(candidate_type))
        if conditions:
            query += joins + " WHERE " + " AND ".join(conditions)
        query += " ORDER BY c.created_at, c.candidate_id"
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [_candidate_from_row(row) for row in rows]

    # -- manual canonical linking (confirm same) ----------------------------------

    def link_sources_to_canonical(
        self,
        left_source_key: str,
        right_source_key: str,
        *,
        winner_source_key: str,
    ) -> str:
        """Link two source records under one active canonical via alias/merged_into.

        No source record, canonical row or Markdown note is ever deleted.  The
        loser canonical stays resolvable through ``canonical_aliases`` and a
        ``merged`` row pointing at the winner.  Defensive checks make this
        idempotent and self-merge-proof even when called directly.
        """
        left = self.get_source_record(left_source_key)
        right = self.get_source_record(right_source_key)
        if left is None or right is None:
            raise KeyError("Both source records must exist before linking")
        winner = self.get_source_record(winner_source_key)
        if winner is None:
            raise KeyError(f"Winner source record does not exist: {winner_source_key}")
        if winner_source_key not in {left_source_key, right_source_key}:
            raise ValueError("winner must be one of the two linked sources")
        with self._connect() as connection:
            winner_canonical = self._resolve_canonical_in_conn(
                connection, winner.canonical_conversation_id
            )
            loser_record = right if winner_source_key == left_source_key else left
            loser_canonical = self._resolve_canonical_in_conn(
                connection, loser_record.canonical_conversation_id
            )
            assert winner_canonical is not None and loser_canonical is not None
            if loser_canonical.canonical_conversation_id == winner_canonical.canonical_conversation_id:
                # Already linked: idempotent no-op, never self-merge.
                return winner_canonical.canonical_conversation_id
            self._link_in_conn(
                connection,
                loser_canonical.canonical_conversation_id,
                winner_canonical.canonical_conversation_id,
            )
        return winner_canonical.canonical_conversation_id

    def _link_in_conn(
        self,
        connection: sqlite3.Connection,
        loser_canonical_id: str,
        winner_canonical_id: str,
    ) -> None:
        """Write the alias/merge rows; refuses any self-merge by construction."""
        if loser_canonical_id == winner_canonical_id:
            raise ValueError("self-merge refused: loser canonical equals winner canonical")
        now = _utc_now()
        connection.execute(
            """
            INSERT INTO canonical_aliases(alias_canonical_id, active_canonical_id, created_at, reason)
            VALUES (?, ?, ?, 'manual_confirm_same')
            ON CONFLICT(alias_canonical_id) DO UPDATE SET active_canonical_id = excluded.active_canonical_id
            """,
            (loser_canonical_id, winner_canonical_id, now),
        )
        connection.execute(
            """
            UPDATE canonical_conversations
            SET status = 'merged', merged_into_id = ?, updated_at = ?
            WHERE canonical_conversation_id = ?
            """,
            (winner_canonical_id, now, loser_canonical_id),
        )
        connection.execute(
            "UPDATE conversation_source_records SET canonical_conversation_id = ?, last_seen_at = ? "
            "WHERE canonical_conversation_id = ?",
            (winner_canonical_id, now, loser_canonical_id),
        )

    def confirm_candidate_link(
        self,
        candidate_id: int,
        *,
        winner_source_key: str = "",
        note: str = "",
    ) -> dict[str, Any]:
        """Record a confirmed_same decision and link the two sources atomically.

        The candidate decision and the canonical link happen inside one SQLite
        transaction, so a failed link can never leave a permanently-confirmed
        candidate behind with inconsistent canonical rows.  Repeated confirms
        are idempotent no-ops; a rejected candidate refuses confirmation.
        """
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM conversation_duplicate_candidates WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown candidate id: {candidate_id}")
            candidate = _candidate_from_row(row)
            if candidate.status == "rejected":
                raise ValueError(
                    "candidate is rejected; confirmation requires an explicit reopen flow "
                    "(not available in v0.2.4)"
                )
            left = connection.execute(
                "SELECT * FROM conversation_source_records WHERE source_key = ?",
                (candidate.left_source_key,),
            ).fetchone()
            right = connection.execute(
                "SELECT * FROM conversation_source_records WHERE source_key = ?",
                (candidate.right_source_key,),
            ).fetchone()
            if left is None or right is None:
                raise KeyError("Both source records must exist before linking")
            left_record = _source_record_from_row(left)
            right_record = _source_record_from_row(right)
            winner_key = normalize_component(winner_source_key) or sorted(
                [left_record, right_record], key=lambda r: (r.first_seen_at, r.source_key)
            )[0].source_key
            if winner_key not in {left_record.source_key, right_record.source_key}:
                raise ValueError("winner must be one of the two candidate sides")
            winner_record = left_record if winner_key == left_record.source_key else right_record
            loser_record = right_record if winner_key == left_record.source_key else left_record
            winner_canonical = self._resolve_canonical_in_conn(
                connection, winner_record.canonical_conversation_id
            )
            loser_canonical = self._resolve_canonical_in_conn(
                connection, loser_record.canonical_conversation_id
            )
            assert winner_canonical is not None and loser_canonical is not None
            winner_id = winner_canonical.canonical_conversation_id
            loser_id = loser_canonical.canonical_conversation_id
            already_linked = loser_id == winner_id
            outcome = "no_op" if (already_linked and candidate.status == "confirmed_same") else (
                "no_op" if already_linked else "linked"
            )
            now = _utc_now()
            if candidate.status == "pending":
                connection.execute(
                    """
                    UPDATE conversation_duplicate_candidates
                    SET status = 'confirmed_same', reviewed_at = ? WHERE candidate_id = ?
                    """,
                    (now, candidate_id),
                )
                connection.execute(
                    """
                    INSERT INTO dedup_decisions(
                        left_source_key, right_source_key, decision, candidate_type, decided_at, note
                    ) VALUES (?, ?, 'confirmed_same', ?, ?, ?)
                    """,
                    (candidate.left_source_key, candidate.right_source_key, candidate.candidate_type, now, _norm(note)),
                )
            if not already_linked:
                self._link_in_conn(connection, loser_id, winner_id)
            # Post-conditions inside the same transaction.
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM canonical_aliases WHERE alias_canonical_id = ?",
                    (winner_id,),
                ).fetchone()[0]
                == 0
            ), "self-alias created"
            winner_row = connection.execute(
                "SELECT status, merged_into_id FROM canonical_conversations WHERE canonical_conversation_id = ?",
                (winner_id,),
            ).fetchone()
            assert winner_row is not None and winner_row["status"] == "active", "winner canonical must stay active"
            assert not winner_row["merged_into_id"], "winner canonical must not carry merged_into_id"
        return {
            "candidate_id": candidate_id,
            "decision": "confirmed_same",
            "outcome": outcome,
            "winner_source_key": winner_key,
            "active_canonical_conversation_id": winner_id,
            "loser_canonical_resolves": True,
        }

    def list_orphan_source_records(self) -> list[SourceRecord]:
        """Active source records whose canonical row is missing or merged-away."""
        orphans: list[SourceRecord] = []
        with self._connect() as connection:
            for record in self.list_source_records():
                row = connection.execute(
                    "SELECT status, merged_into_id FROM canonical_conversations WHERE canonical_conversation_id = ?",
                    (record.canonical_conversation_id,),
                ).fetchone()
                if row is None:
                    alias = connection.execute(
                        "SELECT active_canonical_id FROM canonical_aliases WHERE alias_canonical_id = ?",
                        (record.canonical_conversation_id,),
                    ).fetchone()
                    if alias is None:
                        orphans.append(record)
        return orphans

    def duplicate_output_paths(self) -> list[tuple[str, int]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT output_path, COUNT(*) AS n FROM conversation_source_records
                WHERE output_path != '' AND status = 'active'
                GROUP BY output_path HAVING COUNT(*) > 1
                """
            ).fetchall()
        return [(row["output_path"], int(row["n"])) for row in rows]

    def identity_stats(self) -> dict[str, Any]:
        with self._connect() as connection:
            canonical = connection.execute(
                "SELECT COUNT(*) FROM canonical_conversations WHERE status = 'active'"
            ).fetchone()[0]
            records = connection.execute(
                "SELECT COUNT(*) FROM conversation_source_records WHERE status = 'active'"
            ).fetchone()[0]
            pending = connection.execute(
                "SELECT COUNT(*) FROM conversation_duplicate_candidates WHERE status = 'pending'"
            ).fetchone()[0]
            confirmed = connection.execute(
                "SELECT COUNT(*) FROM conversation_duplicate_candidates WHERE status = 'confirmed_same'"
            ).fetchone()[0]
            rejected = connection.execute(
                "SELECT COUNT(*) FROM conversation_duplicate_candidates WHERE status = 'rejected'"
            ).fetchone()[0]
            distribution: dict[str, int] = {}
            for row in connection.execute(
                "SELECT source_system, COUNT(*) FROM conversation_source_records "
                "WHERE status = 'active' GROUP BY source_system"
            ).fetchall():
                distribution[row[0] or "unknown"] = int(row[1])
        return {
            "canonical_conversations": int(canonical),
            "source_records": int(records),
            "pending_duplicate_candidates": int(pending),
            "confirmed_duplicate_links": int(confirmed),
            "rejected_duplicate_candidates": int(rejected),
            "source_system_distribution": distribution,
        }

    # -- legacy migration ----------------------------------------------------------

    def migrate_legacy_imports(
        self,
        legacy_rows: Sequence[dict[str, Any]],
        *,
        parser_version_default: str = "",
        schema_version_default: str = "",
    ) -> MigrationReport:
        """Map legacy ``conversation_imports`` rows into v2 tables, idempotently.

        Each legacy record becomes exactly one source record under the stable
        Codex legacy namespace, one canonical conversation and one initial
        snapshot.  Legacy rows are never modified or deleted by this method.
        """
        report = MigrationReport()
        namespace_hash = codex_legacy_namespace_hash()
        for row in legacy_rows:
            conversation_id = _norm(row.get("conversation_id"))
            if not conversation_id:
                report.skipped_legacy_ids.append(_norm(row.get("source_entry") or "<blank>"))
                continue
            report.legacy_records += 1
            identity = ConversationSourceIdentity(
                source_system="codex",
                source_account_namespace_hash=namespace_hash,
                source_conversation_id=conversation_id,
            )
            source_key = identity.source_key
            canonical_id = canonical_conversation_id_for(source_key)
            output_path = _norm(row.get("output_path"))
            archive_sha = _norm(row.get("source_archive_sha256"))
            entry_sha = _norm(row.get("source_entry_sha256"))
            imported_at = _migrate_time(row.get("imported_at")) or _utc_now()
            parser_version = _norm(row.get("parser_version")) or parser_version_default
            schema_version = _norm(row.get("schema_version")) or schema_version_default
            status = "active"

            existing = self.get_source_record(source_key)
            if existing is not None:
                report.source_records_existing += 1
                # Idempotent replay: never duplicate, and never rewrite the
                # legacy output path mapping.
                if existing.output_path != output_path and output_path:
                    report.output_paths_changed += 1
                if existing.canonical_conversation_id != canonical_id:
                    report.canonical_id_collisions += 1
                canonical_row = self.get_canonical(canonical_id)
                if canonical_row is not None:
                    report.canonical_existing += 1
                snapshots = [
                    snap
                    for snap in self.list_snapshots(source_key)
                    if snap.source_entry_sha256 == entry_sha
                ]
                if snapshots:
                    report.snapshots_existing += 1
                continue

            # Deterministic canonical id derived from source_key; a collision
            # here means the uuid5 derivation broke and must stop the line.
            collision = self.get_canonical(canonical_id)
            if collision is not None and collision.primary_source_key not in ("", source_key):
                report.canonical_id_collisions += 1
                continue

            created_canonical = self.ensure_canonical(canonical_id, primary_source_key=source_key)
            if created_canonical:
                report.canonical_created += 1
            else:
                report.canonical_existing += 1

            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO conversation_source_records(
                        source_key, canonical_conversation_id, source_system,
                        source_account_namespace_hash, source_conversation_id,
                        source_thread_id, source_branch_id, output_path,
                        first_seen_archive_sha256, last_seen_archive_sha256,
                        last_source_entry_sha256, normalized_transcript_sha256,
                        ordered_message_hash, message_set_hash, message_count,
                        fingerprint_version, parser_version, schema_version,
                        status, first_seen_at, last_seen_at
                    ) VALUES (?, ?, 'codex', ?, ?, '', '', ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, 'active', ?, ?)
                    """,
                    (
                        source_key,
                        canonical_id,
                        namespace_hash,
                        conversation_id,
                        output_path,
                        archive_sha,
                        archive_sha,
                        entry_sha,
                        "",
                        "",
                        "",
                        # Migrated legacy records carry no per-message fingerprints
                        # yet: version 0 explicitly means "unknown", so the
                        # continuation/stale/divergence state machine stays
                        # disabled until the first same-entry hydration.
                        FINGERPRINT_VERSION_UNHYDRATED,
                        parser_version,
                        schema_version,
                        imported_at,
                        imported_at,
                    ),
                )
            report.source_records_created += 1

            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO conversation_source_snapshots(
                        source_key, source_archive_sha256, source_entry_sha256,
                        normalized_transcript_sha256, ordered_message_hash,
                        message_set_hash, message_count, fingerprint_version,
                        first_seen_at, last_seen_at, seen_count
                    ) VALUES (?, ?, ?, '', '', '', 0, ?, ?, ?, 1)
                    ON CONFLICT(source_key, source_entry_sha256) DO UPDATE SET
                        last_seen_at = excluded.last_seen_at,
                        seen_count = conversation_source_snapshots.seen_count + 1
                    """,
                    (
                        source_key,
                        archive_sha,
                        entry_sha,
                        FINGERPRINT_VERSION_UNHYDRATED,
                        imported_at,
                        imported_at,
                    ),
                )
            report.snapshots_created += 1

        report.source_key_collisions = self.source_key_collision_count()
        return report


def load_legacy_import_rows(manifest_path: str | Path) -> list[dict[str, Any]]:
    """Read all legacy conversation_imports rows without modifying them."""
    path = Path(manifest_path)
    if not path.exists():
        return []
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                "SELECT * FROM conversation_imports ORDER BY imported_at, conversation_id"
            ).fetchall()
        except sqlite3.OperationalError:
            return []
    return [dict(row) for row in rows]


def _source_record_from_row(row: sqlite3.Row) -> SourceRecord:
    return SourceRecord(
        source_key=row["source_key"],
        canonical_conversation_id=row["canonical_conversation_id"],
        source_system=row["source_system"],
        source_account_namespace_hash=row["source_account_namespace_hash"],
        source_conversation_id=row["source_conversation_id"] or "",
        source_thread_id=row["source_thread_id"] or "",
        source_branch_id=row["source_branch_id"] or "",
        output_path=row["output_path"] or "",
        first_seen_archive_sha256=row["first_seen_archive_sha256"] or "",
        last_seen_archive_sha256=row["last_seen_archive_sha256"] or "",
        last_source_entry_sha256=row["last_source_entry_sha256"] or "",
        normalized_transcript_sha256=row["normalized_transcript_sha256"] or "",
        ordered_message_hash=row["ordered_message_hash"] or "",
        message_set_hash=row["message_set_hash"] or "",
        message_count=int(row["message_count"] or 0),
        fingerprint_version=int(row["fingerprint_version"] or 0),
        parser_version=row["parser_version"] or "",
        schema_version=row["schema_version"] or "",
        status=row["status"],
        first_seen_at=row["first_seen_at"],
        last_seen_at=row["last_seen_at"],
    )


def _snapshot_from_row(row: sqlite3.Row) -> SnapshotRecord:
    return SnapshotRecord(
        snapshot_id=int(row["snapshot_id"]),
        source_key=row["source_key"],
        source_archive_sha256=row["source_archive_sha256"] or "",
        source_entry_sha256=row["source_entry_sha256"] or "",
        normalized_transcript_sha256=row["normalized_transcript_sha256"] or "",
        ordered_message_hash=row["ordered_message_hash"] or "",
        message_set_hash=row["message_set_hash"] or "",
        message_count=int(row["message_count"] or 0),
        fingerprint_version=int(row["fingerprint_version"] or 0),
        first_seen_at=row["first_seen_at"],
        last_seen_at=row["last_seen_at"],
        seen_count=int(row["seen_count"] or 1),
    )


def _candidate_from_row(row: sqlite3.Row) -> DuplicateCandidate:
    return DuplicateCandidate(
        candidate_id=int(row["candidate_id"]),
        left_source_key=row["left_source_key"],
        right_source_key=row["right_source_key"],
        candidate_type=row["candidate_type"],
        score=float(row["score"] or 0.0),
        evidence_json=row["evidence_json"] or "{}",
        status=row["status"],
        created_at=row["created_at"],
        reviewed_at=row["reviewed_at"] or "",
    )


def _canonical_from_row(row: sqlite3.Row) -> CanonicalConversation:
    return CanonicalConversation(
        canonical_conversation_id=row["canonical_conversation_id"],
        status=row["status"],
        merged_into_id=row["merged_into_id"] or "",
        primary_source_key=row["primary_source_key"] or "",
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
