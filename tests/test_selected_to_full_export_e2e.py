from __future__ import annotations

from pathlib import Path
import pytest

from research_memory_gateway.conversations.chatgpt_export import (
    ChatGPTExportReader,
    resolve_account_namespace_hash,
)
from research_memory_gateway.conversations.identity import account_namespace_hash
from research_memory_gateway.conversations.identity_store import ConversationIdentityStore
from research_memory_gateway.conversations.manifest import ImportManifest
from research_memory_gateway.conversations.pipeline import ConversationIngestionPipeline
from research_memory_gateway.conversations.vault_writer import ObsidianConversationWriter
from research_memory_gateway.conversations.index import ConversationIndexDatabase


SEL_BACKUP_PATH = Path(r"D:\Download\chatgpt_business_backup_selected_fe828cbb-7312-4979-8049-9a25c3362b7c_2026-08-12.zip")
ALL_BACKUP_PATH = Path(r"D:\Download\chatgpt_business_backup_all_fe828cbb-7312-4979-8049-9a25c3362b7c_2026-09-04.zip")
TARGET_CONV_ID = "6a7613ce-ef20-83e8-b01f-4c7b9ba6534e"


@pytest.mark.skipif(
    not (SEL_BACKUP_PATH.exists() and ALL_BACKUP_PATH.exists()),
    reason="Real backup archives not available",
)
def test_real_selected_backup_then_full_export_continuation_and_stale_protection(tmp_path: Path) -> None:
    """Requirement 3: Selected backup -> Full export using same account_namespace.

    Prove:
    - Same provider family
    - Same canonical family
    - No duplicate conversation markdown file
    - No duplicate index sections
    - Stale snapshot protection: older snapshot never truncates newer note
    """
    account_namespace = "business-research-group"

    # 1. Resolve namespace hash for both using the SAME explicit namespace
    ns_hash_sel, strat_sel = resolve_account_namespace_hash(SEL_BACKUP_PATH, namespace_label=account_namespace)
    ns_hash_all, strat_all = resolve_account_namespace_hash(ALL_BACKUP_PATH, namespace_label=account_namespace)

    assert ns_hash_sel == ns_hash_all == account_namespace_hash("chatgpt", account_namespace)
    assert strat_sel == strat_all == "explicit_label"

    reader_sel = ChatGPTExportReader(SEL_BACKUP_PATH, account_namespace_hash=ns_hash_sel)
    reader_all = ChatGPTExportReader(ALL_BACKUP_PATH, account_namespace_hash=ns_hash_all)

    # Filter keys for conversation A
    keys_sel = [r.effective_import_key for r in reader_sel.list_sessions() if r.conversation_id == TARGET_CONV_ID]
    keys_all = [r.effective_import_key for r in reader_all.list_sessions() if r.conversation_id == TARGET_CONV_ID]
    assert len(keys_sel) >= 1
    assert len(keys_all) >= 1

    # Shared storage components
    output_root = tmp_path / "staging_merge"
    manifest = ImportManifest(output_root / ".ai-memory" / "manifest.sqlite")
    store = ConversationIdentityStore(manifest.path)
    writer = ObsidianConversationWriter(output_root)
    index_db = ConversationIndexDatabase(tmp_path / "conv_idx.sqlite")

    # -------------------------------------------------------------------------
    # Step 1: Ingest conversation A from selected backup (earlier version)
    # -------------------------------------------------------------------------
    pipeline_sel = ConversationIngestionPipeline(
        reader_sel,
        writer,
        manifest,
        identity_store=store,
    )
    results_sel = pipeline_sel.run(keys_sel)
    assert len(results_sel) >= 1
    written_results = [r for r in results_sel if r.status == "written"]
    assert len(written_results) >= 1
    note_path = Path(written_results[0].output_path)
    assert note_path.exists()
    sel_content = note_path.read_text(encoding="utf-8")

    # Index into FTS
    index_db.index_file(note_path)
    with index_db._connect() as conn:
        doc_row = conn.execute("SELECT id FROM conversation_documents WHERE vault_path = ?", [str(note_path)]).fetchone()
        assert doc_row is not None
        doc_id_1 = doc_row[0]

    # Verify identity in store
    rec_sel = store.find_family_records(
        source_system="chatgpt",
        source_account_namespace_hash=ns_hash_sel,
        source_conversation_id=TARGET_CONV_ID,
    )
    assert len(rec_sel) >= 1
    canonical_sel = rec_sel[0].canonical_conversation_id

    # Count markdown files
    md_files_step1 = [p for p in output_root.rglob("*.md") if not p.name.startswith(".")]
    assert len(md_files_step1) == len(keys_sel) == 6

    # -------------------------------------------------------------------------
    # Step 2: Ingest conversation A from full export (later continuation)
    # -------------------------------------------------------------------------
    pipeline_all = ConversationIngestionPipeline(
        reader_all,
        writer,
        manifest,
        identity_store=store,
    )
    results_all = pipeline_all.run(keys_all)
    assert len(results_all) == len(keys_all) == 10

    # The continuation is written to the SAME note path
    rec_all = store.find_family_records(
        source_system="chatgpt",
        source_account_namespace_hash=ns_hash_all,
        source_conversation_id=TARGET_CONV_ID,
    )
    assert len(rec_all) == 10
    # All branches share the exact same canonical conversation ID family
    canonicals_all = {r.canonical_conversation_id for r in rec_all}
    assert len(canonicals_all) == 1
    assert canonicals_all.pop() == canonical_sel

    # PROVE: No duplicate files created for already existing branches; 4 new branches added
    md_files_step2 = [p for p in output_root.rglob("*.md") if not p.name.startswith(".")]
    assert len(md_files_step2) == len(keys_all) == 10
    assert note_path in md_files_step2

    # The note has been updated with the continuation
    all_content = note_path.read_text(encoding="utf-8")
    assert len(all_content) >= len(sel_content)

    # Re-index into FTS
    index_db.index_file(note_path)
    with index_db._connect() as conn:
        doc_row_2 = conn.execute("SELECT id FROM conversation_documents WHERE vault_path = ?", [str(note_path)]).fetchone()
        assert doc_row_2 is not None
        doc_id_2 = doc_row_2[0]
        assert doc_id_2 == doc_id_1

        # PROVE: No duplicate index documents for the updated note path
        doc_count = conn.execute("SELECT COUNT(*) FROM conversation_documents WHERE vault_path = ?", [str(note_path)]).fetchone()[0]
        assert doc_count == 1

    # -------------------------------------------------------------------------
    # Step 3: Stale snapshot protection test (re-importing selected backup now)
    # -------------------------------------------------------------------------
    # Since all_backup has newer/longer continuation, importing sel_backup now
    # must be recognized as an older snapshot and must NOT overwrite the newer note!
    results_stale = pipeline_sel.run(keys_sel)
    assert len(results_stale) >= 1
    assert all(r.status == "skipped" for r in results_stale)
    # The continued primary branch is stale_snapshot; identical sibling branches are same_source_in_new_archive
    assert any(r.reason == "stale_snapshot" for r in results_stale)
    assert all(r.reason in ("stale_snapshot", "same_source_in_new_archive", "unchanged") for r in results_stale)

    # Note content is unchanged and not truncated
    content_after_stale = note_path.read_text(encoding="utf-8")
    assert content_after_stale == all_content
