from __future__ import annotations

import json
import zipfile
from pathlib import Path
import pytest

from research_memory_gateway.conversations.chatgpt_export import (
    ChatGPTExportError,
    ChatGPTExportReader,
    detect_account_guid,
    resolve_account_namespace_hash,
)
from research_memory_gateway.conversations.identity import account_namespace_hash
from research_memory_gateway.conversations.identity_store import ConversationIdentityStore
from research_memory_gateway.conversations.pipeline import ConversationIngestionPipeline
from research_memory_gateway.conversations.vault_writer import ObsidianConversationWriter
from research_memory_gateway.conversations.manifest import ImportManifest
from tests.chatgpt_fixtures import (
    GraphBuilder,
    build_export,
    linear_conversation,
    make_message,
)


def _ingest(tmp_path: Path, reader: ChatGPTExportReader, staging_sub: str = "staging", manifest: ImportManifest | None = None, store: ConversationIdentityStore | None = None):
    output_root = tmp_path / staging_sub
    if manifest is None:
        manifest = ImportManifest(output_root / ".ai-memory" / "manifest.sqlite")
    if store is None:
        store = ConversationIdentityStore(manifest.path)
    writer = ObsidianConversationWriter(output_root)
    pipeline = ConversationIngestionPipeline(
        reader,
        writer,
        manifest,
        identity_store=store,
    )
    keys = [ref.effective_import_key for ref in reader.list_sessions()]
    results = pipeline.run(keys)
    return pipeline, store, manifest, results, writer


def test_filename_uuid_never_extracted_or_used(tmp_path: Path) -> None:
    """Requirement 1: Filename UUID fallback is completely deleted."""
    conv = linear_conversation("conv-spoof", [("user", "hello"), ("assistant", "hi")])
    spoofed_zip = tmp_path / "chatgpt_business_backup_selected_12345678-abcd-1234-abcd-123456789abc_2026-09-14.zip"
    build_export(spoofed_zip, [conv], account_id=None)

    # 1. detect_account_guid must NOT inspect filename
    assert detect_account_guid(spoofed_zip) == ""

    # 2. Without explicit label and without default opt-in, must fail closed
    with pytest.raises(ChatGPTExportError) as excinfo:
        resolve_account_namespace_hash(spoofed_zip)
    assert excinfo.value.code == "ACCOUNT_NAMESPACE_REQUIRED"

    # 3. With explicit label, explicit label takes precedence, ignoring filename UUID
    ns_hash, strategy = resolve_account_namespace_hash(spoofed_zip, namespace_label="my-team")
    assert strategy == "explicit_label"
    assert ns_hash == account_namespace_hash("chatgpt", "my-team")
    assert "12345678-abcd-1234-abcd-123456789abc" not in ns_hash


def test_filename_invariance_across_renames_and_repeat_imports(tmp_path: Path) -> None:
    """Requirement 2: Same archive under arbitrary names yields identical identity & idempotency."""
    conv = linear_conversation("conv-inv", [("user", "explain quantum computing"), ("assistant", "it uses qubits")])

    zip1 = tmp_path / "original_filename_chatgpt_backup.zip"
    zip2 = tmp_path / "real-selected-backup.zip"
    zip3 = tmp_path / "completely-renamed.zip"

    for zpath in [zip1, zip2, zip3]:
        build_export(zpath, [conv], account_id=None)

    ns_label = "stable-team-namespace"
    ns1, strat1 = resolve_account_namespace_hash(zip1, namespace_label=ns_label)
    ns2, strat2 = resolve_account_namespace_hash(zip2, namespace_label=ns_label)
    ns3, strat3 = resolve_account_namespace_hash(zip3, namespace_label=ns_label)

    assert ns1 == ns2 == ns3
    assert strat1 == strat2 == strat3 == "explicit_label"

    reader1 = ChatGPTExportReader(zip1, account_namespace_hash=ns1)
    reader2 = ChatGPTExportReader(zip2, account_namespace_hash=ns2)
    reader3 = ChatGPTExportReader(zip3, account_namespace_hash=ns3)

    ref1 = reader1.list_sessions()[0]
    ref2 = reader2.list_sessions()[0]
    ref3 = reader3.list_sessions()[0]

    assert ref1.source_account_namespace_hash == ref2.source_account_namespace_hash == ref3.source_account_namespace_hash
    assert ref1.conversation_id == ref2.conversation_id == ref3.conversation_id
    assert ref1.source_branch_id == ref2.source_branch_id == ref3.source_branch_id
    assert ref1.source_sha256 == ref2.source_sha256 == ref3.source_sha256

    # Ingest zip1
    pipeline1, store, manifest, results1, writer = _ingest(tmp_path, reader1, staging_sub="staging_inv")
    assert len(results1) == 1
    assert results1[0].status == "written"
    note_path = results1[0].output_path
    assert note_path and Path(note_path).exists()
    note_content_1 = Path(note_path).read_text(encoding="utf-8")

    # Ingest zip2 (different filename) using same store & manifest -> idempotency: skipped, no duplicate file
    pipeline2, _, _, results2, _ = _ingest(tmp_path, reader2, staging_sub="staging_inv", manifest=manifest, store=store)
    assert len(results2) == 1
    assert results2[0].status == "skipped"
    assert results2[0].reason in ("unchanged", "same_source_in_new_archive")
    assert results2[0].output_path == note_path
    note_content_2 = Path(note_path).read_text(encoding="utf-8")
    assert note_content_1 == note_content_2

    # Ingest zip3 (third filename) -> still skipped
    pipeline3, _, _, results3, _ = _ingest(tmp_path, reader3, staging_sub="staging_inv", manifest=manifest, store=store)
    assert len(results3) == 1
    assert results3[0].status == "skipped"
    assert results3[0].reason in ("unchanged", "same_source_in_new_archive")

    # Only one markdown file exists in the directory
    md_files = [p for p in (tmp_path / "staging_inv").rglob("*.md") if not p.name.startswith(".")]
    assert len(md_files) == 1


def test_selected_single_conversation_detection_and_ingestion(tmp_path: Path) -> None:
    """Selected single conversation JSON file inside ZIP is auto-discovered."""
    conv = linear_conversation("conv-single", [("user", "test query"), ("assistant", "test response")])
    zip_path = tmp_path / "selected_single.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        # Business selected backup uses filename like: 标题_hash.json
        zf.writestr("My_Research_Topic_2aeea129deca.json", json.dumps(conv))

    ns_hash, _ = resolve_account_namespace_hash(zip_path, namespace_label="single-test-ns")
    reader = ChatGPTExportReader(zip_path, account_namespace_hash=ns_hash)
    shards = reader.conversation_shards()
    assert shards == ["My_Research_Topic_2aeea129deca.json"]

    refs = reader.list_sessions()
    assert len(refs) == 1
    assert refs[0].conversation_id == "conv-single"

    _, _, _, results, _ = _ingest(tmp_path, reader, staging_sub="staging_single")
    assert len(results) == 1
    assert results[0].status == "written"


def test_stale_snapshot_protection_preserves_newer_continuation(tmp_path: Path) -> None:
    """Importing an older snapshot after a newer continuation must NOT overwrite or duplicate."""
    # Version A: short conversation
    c_short = linear_conversation(
        "conv-cont",
        [("user", "step 1"), ("assistant", "done 1")],
        update_time=1700000100.0,
    )
    # Version B: extended conversation (continuation)
    c_long = linear_conversation(
        "conv-cont",
        [
            ("user", "step 1"),
            ("assistant", "done 1"),
            ("user", "step 2"),
            ("assistant", "done 2"),
        ],
        update_time=1700000200.0,
    )

    zip_short = tmp_path / "short_v1.zip"
    zip_long = tmp_path / "long_v2.zip"
    build_export(zip_short, [c_short], account_id=None)
    build_export(zip_long, [c_long], account_id=None)

    ns_label = "test-continuation-ns"
    ns_hash, _ = resolve_account_namespace_hash(zip_long, namespace_label=ns_label)
    reader_long = ChatGPTExportReader(zip_long, account_namespace_hash=ns_hash)
    reader_short = ChatGPTExportReader(zip_short, account_namespace_hash=ns_hash)

    # First ingest the newer/longer conversation
    _, store, manifest, res_long, _ = _ingest(tmp_path, reader_long, staging_sub="staging_cont")
    assert len(res_long) == 1
    assert res_long[0].status == "written"
    note_path = res_long[0].output_path
    long_content = Path(note_path).read_text(encoding="utf-8")
    assert "step 2" in long_content

    # Now ingest the older/shorter snapshot using same manifest & store
    _, _, _, res_short, _ = _ingest(tmp_path, reader_short, staging_sub="staging_cont", manifest=manifest, store=store)
    assert len(res_short) == 1
    # Must detect stale_snapshot and SKIP without corrupting newer note
    assert res_short[0].status == "skipped"
    assert res_short[0].reason == "stale_snapshot"
    current_content = Path(note_path).read_text(encoding="utf-8")
    assert current_content == long_content
    assert current_content == long_content
    assert "step 2" in current_content


def test_branch_identity_preservation(tmp_path: Path) -> None:
    """ChatGPT branching conversations preserve alternate branches under same family."""
    builder = GraphBuilder("conv-branched")
    builder.append(make_message("user", "prompt"))
    fork = builder.tail
    builder.append(make_message("assistant", "answer A"))
    main_leaf = builder.tail
    builder.attach_branch_under(fork, make_message("assistant", "answer B"))
    alt_leaf = builder.tail
    conv = builder.to_conversation(current_node=main_leaf)
    zip_path = tmp_path / "branched.zip"
    build_export(zip_path, [conv], account_id=None)

    ns_hash, _ = resolve_account_namespace_hash(zip_path, namespace_label="branch-ns")
    reader = ChatGPTExportReader(zip_path, account_namespace_hash=ns_hash)
    refs = reader.list_sessions()
    assert len(refs) == 2

    # Both branches share the same conversation_id and account_namespace_hash
    assert refs[0].conversation_id == refs[1].conversation_id == "conv-branched"
    assert refs[0].source_account_namespace_hash == refs[1].source_account_namespace_hash == ns_hash
    # Branches differ in source_branch_id
    assert refs[0].source_branch_id != refs[1].source_branch_id

    _, store, _, results, _ = _ingest(tmp_path, reader, staging_sub="staging_branches")
    assert len(results) == 2
    assert results[0].status == "written"
    assert results[1].status == "written"

    # Both belong to the same canonical family
    family = store.find_family_records(
        source_system="chatgpt",
        source_account_namespace_hash=ns_hash,
        source_conversation_id="conv-branched",
    )
    assert len(family) == 2
    canonical_ids = {r.canonical_conversation_id for r in family}
    assert len(canonical_ids) == 1, "Sibling branches must share one canonical family"
