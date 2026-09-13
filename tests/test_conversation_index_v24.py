"""v0.2.4 W10: index additive migration tests (taskbook 13.8 #37-40)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from research_memory_gateway.conversations import (
    ConversationIndexDatabase,
    ConversationIngestionPipeline,
    ImportManifest,
    ObsidianConversationWriter,
)
from research_memory_gateway.conversations.codex_export import CodexExportReader
from tests.test_conversation_ingestion import make_export


def _build_old_style_index(tmp_path: Path) -> Path:
    """Create an index DB with the pre-v0.2.4 schema (no identity columns)."""
    db_path = tmp_path / "legacy-index.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE conversation_documents (
                id TEXT PRIMARY KEY,
                vault_path TEXT NOT NULL,
                title TEXT,
                file_hash TEXT NOT NULL,
                created_date TEXT,
                projects_json TEXT,
                topics_json TEXT,
                parent_thread_id TEXT,
                thread_source TEXT,
                source_system TEXT,
                source_originator TEXT,
                source_surface TEXT,
                source_version TEXT,
                model_provider TEXT,
                model_name TEXT,
                agent_path TEXT,
                indexed_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE conversation_sections (
                id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                vault_path TEXT NOT NULL,
                heading_path TEXT,
                title TEXT,
                content TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                chunk_index INTEGER NOT NULL,
                source_anchors_json TEXT,
                projects_json TEXT,
                date TEXT,
                embedding_identity TEXT,
                indexed_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE VIRTUAL TABLE conversation_sections_fts USING fts5(
                id UNINDEXED, conversation_id, parent_thread_id, heading_path, content,
                tokenize='unicode61'
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE conversation_embeddings (
                id TEXT NOT NULL,
                embedding_identity TEXT NOT NULL,
                model TEXT NOT NULL,
                version TEXT NOT NULL,
                dimension INTEGER NOT NULL,
                vector BLOB NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(embedding_identity, model, version)
            )
            """
        )
    return db_path


def _ingest(tmp_path: Path):
    (tmp_path / "exp").mkdir()
    archive, cid = make_export(tmp_path / "exp")
    output_root = tmp_path / "staging"
    manifest = ImportManifest(output_root / ".ai-memory" / "manifest.sqlite")
    writer = ObsidianConversationWriter(output_root)
    pipeline = ConversationIngestionPipeline(CodexExportReader(archive), writer, manifest)
    results = pipeline.run([cid])
    assert results[0].status == "written"
    return pipeline, results[0].output_path, cid


# --- 13.8 #37: opening an old index DB adds identity columns in place -------------

def test_old_index_database_migrates_additively(tmp_path: Path) -> None:
    db_path = _build_old_style_index(tmp_path)
    with sqlite3.connect(db_path) as conn:
        before_docs = {row[1] for row in conn.execute("PRAGMA table_info(conversation_documents)")}
        before_sections = {row[1] for row in conn.execute("PRAGMA table_info(conversation_sections)")}
    assert "source_key" not in before_docs

    idx = ConversationIndexDatabase(db_path)  # opening triggers additive migration

    with sqlite3.connect(db_path) as conn:
        after_docs = {row[1] for row in conn.execute("PRAGMA table_info(conversation_documents)")}
        after_sections = {row[1] for row in conn.execute("PRAGMA table_info(conversation_sections)")}
    for col in ("source_key", "canonical_conversation_id", "source_conversation_id", "source_thread_id", "source_branch_id"):
        assert col in after_docs
        assert col in after_sections
    # pre-existing rows/columns survive
    assert before_docs.issubset(after_docs)
    assert before_sections.issubset(after_sections)
    assert idx.stats()["documents"] == 0


# --- 13.8 #38 + #39: legacy note without canonical frontmatter indexed via mapping --

def test_legacy_note_indexed_with_identity_mapping(tmp_path: Path) -> None:
    pipeline, note_path, cid = _ingest(tmp_path)
    store = pipeline.identity_store
    record = store.list_source_records()[0]

    # strip the v0.2.4 identity fields from the note (legacy 322-style frontmatter)
    text = Path(note_path).read_text(encoding="utf-8")
    for line in list(text.splitlines()):
        if line.startswith(("canonical_conversation_id:", "source_key:", "source_conversation_id:")):
            text = text.replace(line + "\n", "")
    Path(note_path).write_text(text, encoding="utf-8")

    idx = ConversationIndexDatabase(tmp_path / "index.sqlite")

    def identity_lookup(conversation_id: str) -> dict:
        assert conversation_id == cid
        return {
            "source_key": record.source_key,
            "canonical_conversation_id": record.canonical_conversation_id,
            "source_conversation_id": record.source_conversation_id,
        }

    assert idx.index_file(note_path, identity_lookup=identity_lookup) > 0

    with sqlite3.connect(tmp_path / "index.sqlite") as conn:
        doc = conn.execute(
            "SELECT source_key, canonical_conversation_id, source_conversation_id FROM conversation_documents"
        ).fetchone()
        section = conn.execute(
            "SELECT source_key, canonical_conversation_id FROM conversation_sections LIMIT 1"
        ).fetchone()
    assert doc[0] == record.source_key
    assert doc[1] == record.canonical_conversation_id
    assert doc[2] == cid
    assert section[0] == record.source_key
    assert section[1] == record.canonical_conversation_id


# --- 13.8 #40: second changed-only index run is entirely unchanged -----------------

def test_second_changed_only_index_is_unchanged(tmp_path: Path) -> None:
    pipeline, note_path, cid = _ingest(tmp_path)
    store = pipeline.identity_store
    record = store.list_source_records()[0]
    idx = ConversationIndexDatabase(tmp_path / "index.sqlite")
    identity_lookup = lambda c: {  # noqa: E731
        "source_key": record.source_key,
        "canonical_conversation_id": record.canonical_conversation_id,
        "source_conversation_id": record.source_conversation_id,
    }

    first = idx.index_file(note_path, identity_lookup=identity_lookup)
    assert first > 0
    assert idx.check_changed_reason(note_path) == "unchanged"

    # second run reports unchanged for every file
    files = sorted(Path(note_path).parent.parent.rglob("*.md"))
    reasons = {str(f): idx.check_changed_reason(f) for f in files}
    assert all(reason == "unchanged" for reason in reasons.values()), reasons
