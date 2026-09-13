from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from .models import ExportSessionRef, PARSER_VERSION, SCHEMA_VERSION
from .vault_writer import compute_managed_hash, compute_manual_hash


@dataclass(frozen=True)
class ImportDecision:
    action: str
    reason: str
    output_path: str = ""

    @property
    def status(self) -> str:
        mapping = {
            "new_conversation": "new",
            "source_changed": "changed_source",
            "same_conversation_in_new_archive": "unchanged",
            "pipeline_version_changed": "parser_upgrade",
        }
        return mapping.get(self.reason, self.reason)


class ImportManifest:
    """Incremental import ledger. It is rebuildable control data, not canonical memory."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_db(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_imports (
                    conversation_id TEXT PRIMARY KEY,
                    source_archive_path TEXT NOT NULL,
                    source_archive_sha256 TEXT NOT NULL,
                    source_entry TEXT NOT NULL,
                    source_entry_sha256 TEXT NOT NULL,
                    source_size_bytes INTEGER NOT NULL,
                    output_path TEXT NOT NULL,
                    output_sha256 TEXT NOT NULL,
                    managed_output_sha256 TEXT,
                    whole_output_sha256 TEXT,
                    manual_region_sha256 TEXT,
                    index_source_hash TEXT,
                    imported_at TEXT NOT NULL,
                    parser_version TEXT NOT NULL,
                    schema_version TEXT NOT NULL,
                    parent_thread_id TEXT,
                    attachment_inventory_hash TEXT,
                    content_section_hashes TEXT,
                    last_indexed_at TEXT,
                    embedding_model TEXT,
                    embedding_version TEXT,
                    embedding_dimension INTEGER,
                    status TEXT NOT NULL DEFAULT 'written',
                    error TEXT
                )
                """
            )
            _migrate_table(connection)

    def decide(
        self,
        ref: ExportSessionRef,
        *,
        archive_sha256: str,
        parser_version: str = PARSER_VERSION,
        schema_version: str = SCHEMA_VERSION,
        attachment_inventory_hash: str | None = None,
        check_index: bool = False,
        dry_run: bool = False,
    ) -> ImportDecision:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM conversation_imports WHERE conversation_id = ?",
                (ref.conversation_id,),
            ).fetchone()
        if row is None:
            return ImportDecision("write", "new_conversation")
        if row["status"] in {"failed", "failed_retryable"}:
            return ImportDecision("write", "failed_retryable", row["output_path"])
        if row["source_entry_sha256"] != ref.source_sha256:
            return ImportDecision("write", "source_changed", row["output_path"])
        if row["parser_version"] != parser_version or row["schema_version"] != schema_version:
            action = "dry_run" if dry_run else "rebuild"
            return ImportDecision(action, "parser_upgrade", row["output_path"])
        output_path = Path(row["output_path"]) if row["output_path"] else Path("")
        if not output_path.exists() or not output_path.is_file():
            return ImportDecision("write", "output_missing", row["output_path"])

        # Check managed output modifications vs manual modifications
        current_text = output_path.read_text(encoding="utf-8")
        current_managed_hash = compute_managed_hash(current_text)
        stored_managed_hash = row["managed_output_sha256"]
        if stored_managed_hash:
            if current_managed_hash != stored_managed_hash:
                return ImportDecision("conflict", "managed_output_modified", row["output_path"])
        else:
            # Fallback if managed_output_sha256 wasn't recorded
            if _sha256_file(output_path) != row["output_sha256"]:
                return ImportDecision("conflict", "managed_output_modified", row["output_path"])

        if attachment_inventory_hash and row["attachment_inventory_hash"] and row["attachment_inventory_hash"] != attachment_inventory_hash:
            return ImportDecision("write", "attachment_changed", row["output_path"])

        if row["status"] == "index_stale":
            return ImportDecision("index", "index_stale", row["output_path"])
        # The conversation index includes searchable manual notes, so its
        # freshness hash is the complete Markdown input rather than the
        # machine-managed conflict hash. Once a document has been indexed,
        # ingestion may report stale index state even when check_index=False;
        # import itself must not silently rewrite the note to repair indexing.
        cur_index_hash = _sha256_file(output_path)
        if check_index:
            if not row["last_indexed_at"] or not row["index_source_hash"] or row["index_source_hash"] != cur_index_hash:
                return ImportDecision("index", "index_stale", row["output_path"])
        elif row["last_indexed_at"] and row["index_source_hash"] and row["index_source_hash"] != cur_index_hash:
            return ImportDecision("index", "index_stale", row["output_path"])

        if row["source_archive_sha256"] != archive_sha256:
            return ImportDecision("skip", "same_conversation_in_new_archive", row["output_path"])
        return ImportDecision("skip", "unchanged", row["output_path"])

    def record(
        self,
        ref: ExportSessionRef,
        *,
        archive_path: str,
        archive_sha256: str,
        output_path: str | Path,
        managed_output_sha256: str = "",
        whole_output_sha256: str = "",
        manual_region_sha256: str = "",
        index_source_hash: str = "",
        parent_thread_id: str = "",
        attachment_inventory_hash: str = "",
        content_section_hashes: Sequence[str] | dict[str, Any] | str = "",
        last_indexed_at: str = "",
        embedding_model: str = "",
        embedding_version: str = "",
        embedding_dimension: int | None = None,
        status: str = "written",
        error: str = "",
        parser_version: str = PARSER_VERSION,
        schema_version: str = SCHEMA_VERSION,
        record_key: str = "",
    ) -> None:
        """Record one import in the legacy ledger.

        ``record_key`` overrides the ledger row key.  Codex keeps the bare
        provider conversation id (historical behaviour); branched sources use
        a branch-scoped key so siblings never fight over one row.
        """
        ledger_key = (record_key or "").strip() or ref.conversation_id
        output = Path(output_path) if output_path else Path("")
        output_resolved = str(output.resolve()) if str(output_path).strip() else ""
        output_sha = whole_output_sha256 or ""
        managed_sha = managed_output_sha256 or ""
        manual_sha = manual_region_sha256 or ""

        try:
            output_is_file = output.is_file()
        except (OSError, ValueError):
            output_is_file = False

        if output_is_file:
            try:
                if not output_sha:
                    output_sha = _sha256_file(output)
                if not managed_sha or not manual_sha:
                    text = output.read_text(encoding="utf-8")
                    if not managed_sha:
                        managed_sha = compute_managed_hash(text)
                    if not manual_sha:
                        manual_sha = compute_manual_hash(text)
            except Exception:
                pass

        section_hashes_json = (
            content_section_hashes
            if isinstance(content_section_hashes, str)
            else json.dumps(content_section_hashes)
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO conversation_imports(
                    conversation_id, source_archive_path, source_archive_sha256,
                    source_entry, source_entry_sha256, source_size_bytes,
                    output_path, output_sha256, managed_output_sha256, whole_output_sha256,
                    manual_region_sha256, index_source_hash, imported_at, parser_version,
                    schema_version, parent_thread_id, attachment_inventory_hash,
                    content_section_hashes, last_indexed_at, embedding_model,
                    embedding_version, embedding_dimension, status, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    source_archive_path=excluded.source_archive_path,
                    source_archive_sha256=excluded.source_archive_sha256,
                    source_entry=excluded.source_entry,
                    source_entry_sha256=excluded.source_entry_sha256,
                    source_size_bytes=excluded.source_size_bytes,
                    output_path=excluded.output_path,
                    output_sha256=excluded.output_sha256,
                    managed_output_sha256=COALESCE(NULLIF(excluded.managed_output_sha256, ''), conversation_imports.managed_output_sha256),
                    whole_output_sha256=COALESCE(NULLIF(excluded.whole_output_sha256, ''), conversation_imports.whole_output_sha256),
                    manual_region_sha256=COALESCE(NULLIF(excluded.manual_region_sha256, ''), conversation_imports.manual_region_sha256),
                    index_source_hash=COALESCE(NULLIF(excluded.index_source_hash, ''), conversation_imports.index_source_hash),
                    imported_at=excluded.imported_at,
                    parser_version=excluded.parser_version,
                    schema_version=excluded.schema_version,
                    parent_thread_id=COALESCE(NULLIF(excluded.parent_thread_id, ''), conversation_imports.parent_thread_id),
                    attachment_inventory_hash=COALESCE(NULLIF(excluded.attachment_inventory_hash, ''), conversation_imports.attachment_inventory_hash),
                    content_section_hashes=COALESCE(NULLIF(excluded.content_section_hashes, ''), conversation_imports.content_section_hashes),
                    last_indexed_at=COALESCE(NULLIF(excluded.last_indexed_at, ''), conversation_imports.last_indexed_at),
                    embedding_model=COALESCE(NULLIF(excluded.embedding_model, ''), conversation_imports.embedding_model),
                    embedding_version=COALESCE(NULLIF(excluded.embedding_version, ''), conversation_imports.embedding_version),
                    embedding_dimension=COALESCE(excluded.embedding_dimension, conversation_imports.embedding_dimension),
                    status=excluded.status,
                    error=excluded.error
                """,
                (
                    ledger_key,
                    archive_path,
                    archive_sha256,
                    ref.source_entry,
                    ref.source_sha256,
                    ref.source_size_bytes,
                    output_resolved,
                    output_sha,
                    managed_sha,
                    output_sha,
                    manual_sha,
                    index_source_hash,
                    datetime.now(timezone.utc).isoformat(),
                    parser_version,
                    schema_version,
                    parent_thread_id,
                    attachment_inventory_hash,
                    section_hashes_json,
                    last_indexed_at,
                    embedding_model,
                    embedding_version,
                    embedding_dimension,
                    status,
                    error,
                ),
            )

    def record_indexing(
        self,
        conversation_id: str,
        *,
        index_source_hash: str,
        last_indexed_at: str,
        embedding_model: str = "",
        embedding_version: str = "",
        embedding_dimension: int | None = None,
        content_section_hashes: Sequence[str] | dict[str, Any] | str = "",
    ) -> None:
        section_hashes_json = (
            content_section_hashes
            if isinstance(content_section_hashes, str)
            else json.dumps(content_section_hashes)
        )
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE conversation_imports SET
                    index_source_hash = ?,
                    last_indexed_at = ?,
                    embedding_model = ?,
                    embedding_version = ?,
                    embedding_dimension = ?,
                    content_section_hashes = COALESCE(NULLIF(?, ''), content_section_hashes),
                    status = CASE WHEN status = 'index_stale' THEN 'written' ELSE status END
                WHERE conversation_id = ?
                """,
                (
                    index_source_hash,
                    last_indexed_at,
                    embedding_model,
                    embedding_version,
                    embedding_dimension,
                    section_hashes_json,
                    conversation_id,
                ),
            )

    def get_record(self, conversation_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM conversation_imports WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def export_csv(self, path: str | Path) -> Path:
        output = Path(path)
        output.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            cursor = connection.execute(
                "SELECT * FROM conversation_imports ORDER BY imported_at, conversation_id"
            )
            columns = [description[0] for description in cursor.description]
            rows = cursor.fetchall()
        with output.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=columns)
            writer.writeheader()
            writer.writerows(dict(row) for row in rows)
        return output

    def export_json(self, path: str | Path) -> Path:
        output = Path(path)
        output.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            cursor = connection.execute(
                "SELECT * FROM conversation_imports ORDER BY imported_at, conversation_id"
            )
            rows = [dict(r) for r in cursor.fetchall()]
        output.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        return output


def _migrate_table(connection: sqlite3.Connection) -> None:
    cursor = connection.execute("PRAGMA table_info(conversation_imports)")
    existing_columns = {row[1] for row in cursor.fetchall()}
    columns_to_add = [
        ("parent_thread_id", "TEXT"),
        ("attachment_inventory_hash", "TEXT"),
        ("content_section_hashes", "TEXT"),
        ("last_indexed_at", "TEXT"),
        ("embedding_model", "TEXT"),
        ("embedding_version", "TEXT"),
        ("embedding_dimension", "INTEGER"),
        ("managed_output_sha256", "TEXT"),
        ("whole_output_sha256", "TEXT"),
        ("manual_region_sha256", "TEXT"),
        ("index_source_hash", "TEXT"),
        ("error", "TEXT"),
    ]
    for col_name, col_type in columns_to_add:
        if col_name not in existing_columns:
            connection.execute(f"ALTER TABLE conversation_imports ADD COLUMN {col_name} {col_type}")


def _sha256_file(path: Path) -> str:
    if not path.exists() or not path.is_file():
        return ""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()

