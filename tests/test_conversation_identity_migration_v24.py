"""v0.2.4 W10: additive legacy migration tests (taskbook 13.2 #6-10)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from research_memory_gateway.conversations.identity import canonical_conversation_id_for
from research_memory_gateway.conversations.identity_store import (
    ConversationIdentityStore,
    load_legacy_import_rows,
)
from research_memory_gateway.conversations.manifest import ImportManifest
from research_memory_gateway.conversations.models import ExportSessionRef


def _ref(conversation_id: str, archive: Path) -> ExportSessionRef:
    return ExportSessionRef(
        conversation_id=conversation_id,
        title=f"Title {conversation_id}",
        cwd="G:\\LLM\\memory",
        updated_at="2026-09-10T01:00:00Z",
        source_entry=f"files/{conversation_id}/rollout.jsonl",
        source_size_bytes=128,
        source_sha256=f"entry-{conversation_id}",
    )


def _seed_legacy_manifest(tmp_path: Path, count: int = 5) -> tuple[ImportManifest, list[str]]:
    root = tmp_path / "staging"
    root.mkdir(parents=True, exist_ok=True)
    manifest = ImportManifest(root / ".ai-memory" / "manifest.sqlite")
    ids: list[str] = []
    for i in range(count):
        cid = f"legacy-{i:04d}"
        ids.append(cid)
        manifest.record(
            _ref(cid, root),
            archive_path=str(root),
            archive_sha256=f"archive-{i}",
            output_path=str(root / f"note-{i}.md"),
            status="written",
        )
    return manifest, ids


# --- 13.2 #6: legacy manifest -> v2 tables, record counts match -----------------

def test_migration_maps_all_legacy_records(tmp_path: Path) -> None:
    manifest, ids = _seed_legacy_manifest(tmp_path, count=5)
    store = ConversationIdentityStore(manifest.path)
    legacy_rows = load_legacy_import_rows(manifest.path)
    assert len(legacy_rows) == 5

    report = store.migrate_legacy_imports(legacy_rows)
    assert report.legacy_records == 5
    assert report.source_records_created == 5
    assert report.canonical_created == 5
    assert report.source_key_collisions == 0
    assert report.canonical_id_collisions == 0
    assert report.output_paths_changed == 0

    records = store.list_source_records()
    assert len(records) == 5
    canonicals = {r.canonical_conversation_id for r in records}
    assert len(canonicals) == 5
    for record in records:
        assert record.source_system == "codex"
        assert record.source_conversation_id in ids
        assert record.canonical_conversation_id == canonical_conversation_id_for(record.source_key)
        # one snapshot per migrated record
        assert len(store.list_snapshots(record.source_key)) == 1


# --- 13.2 #7: second migration run creates zero duplicates ----------------------

def test_migration_is_idempotent(tmp_path: Path) -> None:
    manifest, _ = _seed_legacy_manifest(tmp_path, count=4)
    store = ConversationIdentityStore(manifest.path)
    legacy_rows = load_legacy_import_rows(manifest.path)

    first = store.migrate_legacy_imports(legacy_rows)
    assert first.source_records_created == 4

    second = store.migrate_legacy_imports(legacy_rows)
    assert second.legacy_records == 4
    assert second.source_records_created == 0
    assert second.source_records_existing == 4
    assert second.canonical_created == 0
    assert second.snapshots_created == 0
    assert second.source_key_collisions == 0
    assert second.canonical_id_collisions == 0

    assert len(store.list_source_records()) == 4


# --- 13.2 #8: legacy output paths unchanged -------------------------------------

def test_migration_preserves_output_paths(tmp_path: Path) -> None:
    manifest, ids = _seed_legacy_manifest(tmp_path, count=3)
    store = ConversationIdentityStore(manifest.path)
    legacy_rows = load_legacy_import_rows(manifest.path)
    store.migrate_legacy_imports(legacy_rows)

    for i, cid in enumerate(ids):
        record = store.find_source_records_by_conversation(cid)[0]
        assert Path(record.output_path).name == f"note-{i}.md"


# --- 13.2 #9: legacy table never dropped ----------------------------------------

def test_migration_keeps_legacy_table_and_rows(tmp_path: Path) -> None:
    manifest, ids = _seed_legacy_manifest(tmp_path, count=3)
    store = ConversationIdentityStore(manifest.path)
    store.migrate_legacy_imports(load_legacy_import_rows(manifest.path))

    with sqlite3.connect(manifest.path) as conn:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        assert "conversation_imports" in tables
        count = conn.execute("SELECT COUNT(*) FROM conversation_imports").fetchone()[0]
        assert count == 3
        row = conn.execute(
            "SELECT conversation_id FROM conversation_imports WHERE conversation_id = ?",
            (ids[0],),
        ).fetchone()
        assert row is not None


# --- 13.2 #10: legacy Codex results still searchable by bare conversation_id ----

def test_legacy_result_resolvable_via_bare_conversation_id(tmp_path: Path) -> None:
    manifest, ids = _seed_legacy_manifest(tmp_path, count=2)
    store = ConversationIdentityStore(manifest.path)
    store.migrate_legacy_imports(load_legacy_import_rows(manifest.path))

    found = store.find_source_records_by_conversation(ids[1])
    assert len(found) == 1
    assert found[0].source_conversation_id == ids[1]
    # legacy manifest row untouched and still readable
    legacy_row = manifest.get_record(ids[1])
    assert legacy_row is not None
    assert legacy_row["output_path"].endswith("note-1.md")


def test_migration_blank_conversation_id_is_skipped_safely(tmp_path: Path) -> None:
    manifest, _ = _seed_legacy_manifest(tmp_path, count=1)
    # inject one row with a blank conversation id directly
    with sqlite3.connect(manifest.path) as conn:
        conn.execute(
            """
            INSERT INTO conversation_imports(
                conversation_id, source_archive_path, source_archive_sha256, source_entry,
                source_entry_sha256, source_size_bytes, output_path, output_sha256,
                imported_at, parser_version, schema_version, status
            ) VALUES ('', 'a', 'b', 'c', 'd', 1, 'e', 'f', 'now', 'p', 's', 'written')
            """
        )
    store = ConversationIdentityStore(manifest.path)
    rows = load_legacy_import_rows(manifest.path)
    assert len(rows) == 2
    report = store.migrate_legacy_imports(rows)
    assert report.legacy_records == 1
    assert len(report.skipped_legacy_ids) == 1
