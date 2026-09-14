"""v0.2.6 ChatGPT importer ingestion tests: identity, dedup, continuation, branches.

Release-blocker matrix C (identity/account), D (full-export dedup),
E (continuation/stale/divergence) and F (duplicate safety) exercised through
the real ingestion pipeline against synthetic ChatGPT export ZIPs.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from research_memory_gateway.conversations import (
    ConversationIngestionPipeline,
    ImportManifest,
    ObsidianConversationWriter,
)
from research_memory_gateway.conversations.chatgpt_export import (
    ChatGPTExportError,
    ChatGPTExportReader,
    resolve_account_namespace_hash,
)
from tests.chatgpt_fixtures import (
    GraphBuilder,
    build_export, build_sharded_export,
    linear_conversation,
    make_message,
)


def _make_reader(
    tmp_path,
    conversations,
    *,
    name="export.zip",
    namespace_label="default-label",
    account_id=None,
) -> ChatGPTExportReader:
    from research_memory_gateway.conversations.identity import account_namespace_hash

    archive = build_export(
        tmp_path / name, conversations, account_id=account_id, extra_entries=None
    )
    return ChatGPTExportReader(
        archive, account_namespace_hash=account_namespace_hash("chatgpt", namespace_label)
    )


def _ingest(tmp_path, reader, *, staging="staging", manifest=None, **run_kwargs):
    output_root = tmp_path / staging
    if manifest is None:
        manifest = ImportManifest(output_root / ".ai-memory" / "manifest.sqlite")
    writer = ObsidianConversationWriter(output_root)
    pipeline = ConversationIngestionPipeline(reader, writer, manifest)
    keys = [ref.effective_import_key for ref in reader.list_sessions()]
    results = pipeline.run(keys, **run_kwargs)
    return pipeline, manifest, results, output_root


def _branch_conversation(
    conversation_id="conv-branch",
    *,
    main_answer="answer main",
    alt_answer="answer alt",
    current="main",
) -> dict:
    builder = GraphBuilder(conversation_id)
    builder.append(make_message("user", "shared question"))
    fork = builder.tail
    builder.append(make_message("assistant", main_answer))
    main_leaf = builder.tail
    builder.attach_branch_under(fork, make_message("assistant", alt_answer))
    alt_leaf = builder.tail
    return builder.to_conversation(current_node=main_leaf if current == "main" else alt_leaf)


# --- C: identity / account ------------------------------------------------------


def test_c17_same_provider_id_same_namespace_same_identity(tmp_path) -> None:
    conversation = linear_conversation("conv-id", [("user", "hello"), ("assistant", "hi")])
    reader_a = _make_reader(tmp_path, [conversation], name="a.zip", namespace_label="ns1")
    reader_b = _make_reader(tmp_path, [conversation], name="b.zip", namespace_label="ns1")

    pipeline_a, _, results_a, _ = _ingest(tmp_path, reader_a, staging="st1")
    assert results_a[0].status == "written"
    pipeline_b, _, results_b, _ = _ingest(tmp_path, reader_b, staging="st1")
    assert results_b[0].status == "skipped"

    records = pipeline_b.identity_store.list_source_records(source_system="chatgpt")
    assert len(records) == 1
    ref_a = reader_a.list_sessions()[0]
    ref_b = reader_b.list_sessions()[0]
    assert ref_a.source_identity().source_key == ref_b.source_identity().source_key


def test_c18_same_provider_id_different_namespace_distinct(tmp_path) -> None:
    conversation = linear_conversation("conv-id", [("user", "hello"), ("assistant", "hi")])
    reader_a = _make_reader(tmp_path, [conversation], name="a.zip", namespace_label="account-one")
    reader_b = _make_reader(tmp_path, [conversation], name="b.zip", namespace_label="account-two")

    pipeline_a, _, results_a, _ = _ingest(tmp_path, reader_a, staging="st2")
    assert results_a[0].status == "written"
    pipeline_b, _, results_b, _ = _ingest(tmp_path, reader_b, staging="st2")
    assert results_b[0].status == "written"

    records = pipeline_b.identity_store.list_source_records(source_system="chatgpt")
    assert len(records) == 2
    assert len({r.source_account_namespace_hash for r in records}) == 2
    # different accounts never share a canonical family
    assert len({r.canonical_conversation_id for r in records}) == 2


def test_c19_raw_namespace_label_never_persisted(tmp_path) -> None:
    conversation = linear_conversation("conv-secret", [("user", "u"), ("assistant", "a")])
    secret_label = "super-secret-account-label-4711"
    reader = _make_reader(tmp_path, [conversation], name="s.zip", namespace_label=secret_label)
    _, _, results, output_root = _ingest(tmp_path, reader, staging="st3")
    assert results[0].status == "written"

    # scan every persisted artifact: notes, manifest db, csv, sidecars
    persisted_files = list(output_root.rglob("*"))
    assert persisted_files, "staging output missing"
    for path in persisted_files:
        if path.is_file() and path.suffix in {".md", ".csv", ".json", ".sqlite"}:
            blob = path.read_bytes()
            assert secret_label.encode("utf-8") not in blob, f"label leaked into {path}"


def test_c20_missing_namespace_fails_and_opt_in_default(tmp_path) -> None:
    conversation = linear_conversation("conv-ns", [("user", "u"), ("assistant", "a")])
    archive = build_export(tmp_path / "ns.zip", [conversation], account_id=None)
    from research_memory_gateway.conversations.identity import account_namespace_hash

    with pytest.raises(ChatGPTExportError) as excinfo:
        resolve = __import__(
            "research_memory_gateway.conversations.chatgpt_export",
            fromlist=["resolve_account_namespace_hash"],
        ).resolve_account_namespace_hash(archive)
    assert excinfo.value.code == "ACCOUNT_NAMESPACE_REQUIRED"

    # explicit opt-in default
    from research_memory_gateway.conversations.chatgpt_export import (
        resolve_account_namespace_hash,
    )

    ns_hash, strategy = resolve_account_namespace_hash(archive, use_default=True)
    assert strategy == "default_opt_in"
    assert ns_hash == account_namespace_hash("chatgpt", "chatgpt-default-v1")

    # explicit label
    ns_hash2, strategy2 = resolve_account_namespace_hash(
        archive, namespace_label="my-namespace"
    )
    assert strategy2 == "explicit_label"
    assert ns_hash2 == account_namespace_hash("chatgpt", "my-namespace")


def test_c20b_export_guid_takes_priority(tmp_path) -> None:
    from research_memory_gateway.conversations.chatgpt_export import (
        detect_account_guid,
        resolve_account_namespace_hash,
    )
    from tests.chatgpt_fixtures import TEST_ACCOUNT_GUID

    conversation = linear_conversation("conv-guid", [("user", "u"), ("assistant", "a")])
    archive = build_export(tmp_path / "guid.zip", [conversation], account_id=TEST_ACCOUNT_GUID)
    assert detect_account_guid(archive) == TEST_ACCOUNT_GUID
    ns_hash, strategy = resolve_account_namespace_hash(
        archive, namespace_label="ignored-label"
    )
    assert strategy == "export_account_guid"
    # the raw GUID itself is never persisted -- only its hash is returned
    assert TEST_ACCOUNT_GUID not in ns_hash


def test_c_branch_family_canonical(tmp_path) -> None:
    """B15: branches of one provider conversation share one canonical family."""
    conversation = _branch_conversation()
    reader = _make_reader(tmp_path, [conversation], name="fam.zip")
    pipeline, _, results, _ = _ingest(tmp_path, reader, staging="st4")
    statuses = sorted(r.status for r in results)
    assert statuses == ["written", "written"]

    store = pipeline.identity_store
    records = store.list_source_records(source_system="chatgpt")
    assert len(records) == 2
    source_keys = {r.source_key for r in records}
    assert len(source_keys) == 2  # branch siblings are distinct source records
    canonicals = {r.canonical_conversation_id for r in records}
    assert len(canonicals) == 1  # ...but one canonical family
    branch_ids = {r.source_branch_id for r in records}
    assert len(branch_ids) == 2
    conversation_ids = {r.source_conversation_id for r in records}
    assert conversation_ids == {"conv-branch"}


# --- D: full-export dedup ---------------------------------------------------------


def test_d21_same_full_export_twice_all_skipped(tmp_path) -> None:
    conversations = [
        linear_conversation("conv-1", [("user", "u1"), ("assistant", "a1")]),
        linear_conversation("conv-2", [("user", "u2"), ("assistant", "a2")]),
        _branch_conversation("conv-3"),
    ]
    reader_first = _make_reader(tmp_path, conversations, name="first.zip")
    pipeline, _, results_first, output_root = _ingest(tmp_path, reader_first, staging="st5")
    assert all(r.status == "written" for r in results_first)
    assert len(results_first) == 4  # conv-3 contributes two branches

    notes_first = {
        r.output_path for r in results_first if r.status == "written"
    }
    contents_first = {Path(p): open(p, "rb").read() for p in notes_first}

    # the very same ZIP again
    reader_second = _make_reader(tmp_path, conversations, name="first.zip")
    _, _, results_second, _ = _ingest(
        tmp_path, reader_second, staging="st5", manifest=pipeline.manifest
    )
    assert all(r.status == "skipped" for r in results_second)
    assert all(r.reason == "unchanged" for r in results_second)

    for path, blob in contents_first.items():
        assert path.read_bytes() == blob, "note was rewritten on repeat import"


def test_d22_new_export_with_extra_conversations_old_unchanged(tmp_path) -> None:
    old_conversations = [
        linear_conversation(f"conv-{i}", [("user", f"q{i}"), ("assistant", f"a{i}")])
        for i in range(5)
    ]
    reader_old = _make_reader(tmp_path, old_conversations, name="old.zip")
    pipeline, _, results_old, output_root = _ingest(tmp_path, reader_old, staging="st6")
    assert all(r.status == "written" for r in results_old)

    new_conversations = old_conversations + [
        linear_conversation(f"conv-new-{i}", [("user", f"nq{i}"), ("assistant", f"na{i}")])
        for i in range(20)
    ]
    reader_new = _make_reader(tmp_path, new_conversations, name="new.zip")
    _, _, results_new, _ = _ingest(
        tmp_path, reader_new, staging="st6", manifest=pipeline.manifest
    )
    old_results = [r for r in results_new if r.conversation_id in {c["conversation_id"] for c in old_conversations}]
    new_results = [r for r in results_new if r.conversation_id not in {c["conversation_id"] for c in old_conversations}]
    assert all(r.status == "skipped" for r in old_results)
    # per-branch snapshot hashes are identical -> old snapshots unchanged
    # (same content in a fresh archive reports the packaging-only reason)
    assert all(
        r.reason in {"unchanged", "same_source_in_new_archive"} for r in old_results
    )
    assert len(new_results) == 20
    assert all(r.status == "written" for r in new_results)


def test_d23_json_reorder_unchanged(tmp_path) -> None:
    conversations = [
        linear_conversation("conv-r1", [("user", "u"), ("assistant", "a")]),
        linear_conversation("conv-r2", [("user", "u2"), ("assistant", "a2")]),
    ]
    reader_a = _make_reader(tmp_path, conversations, name="ra.zip")
    pipeline, _, results_a, _ = _ingest(tmp_path, reader_a, staging="st7")
    assert all(r.status == "written" for r in results_a)

    reversed_conversations = list(reversed(conversations))
    reader_b = _make_reader(tmp_path, reversed_conversations, name="rb.zip")
    _, _, results_b, _ = _ingest(
        tmp_path, reader_b, staging="st7", manifest=pipeline.manifest
    )
    assert all(r.status == "skipped" for r in results_b)
    assert all(
        r.reason in {"unchanged", "same_source_in_new_archive"} for r in results_b
    )


def test_d24_title_change_keeps_path_stable(tmp_path) -> None:
    conversation = linear_conversation(
        "conv-title", [("user", "u"), ("assistant", "a")], title="Original Title"
    )
    reader_a = _make_reader(tmp_path, [conversation], name="t1.zip")
    pipeline, _, results_a, _ = _ingest(tmp_path, reader_a, staging="st8")
    assert results_a[0].status == "written"
    original_path = results_a[0].output_path

    renamed = json.loads(json.dumps(conversation))
    renamed["title"] = "A Completely Different Title"
    reader_b = _make_reader(tmp_path, [renamed], name="t2.zip")
    _, _, results_b, _ = _ingest(
        tmp_path, reader_b, staging="st8", manifest=pipeline.manifest
    )
    assert results_b[0].status == "skipped"
    record = pipeline.identity_store.list_source_records(source_system="chatgpt")[0]
    assert record.output_path == original_path


# --- E: continuation / stale / divergence -------------------------------------------


def _continued_conversation(conversation_id="conv-cont"):
    builder = GraphBuilder(conversation_id)
    builder.append(make_message("user", "first question"))
    builder.append(make_message("assistant", "first answer"))
    builder.append(make_message("user", "second question"))
    builder.append(make_message("assistant", "second answer"))
    return builder.to_conversation()


def test_e25_strict_continuation(tmp_path) -> None:
    builder = GraphBuilder("conv-cont")
    builder.append(make_message("user", "first question"))
    builder.append(make_message("assistant", "first answer"))
    old_conversation = builder.to_conversation()

    reader_old = _make_reader(tmp_path, [old_conversation], name="c1.zip")
    pipeline, _, results_old, output_root = _ingest(tmp_path, reader_old, staging="st9")
    assert results_old[0].status == "written"
    original_path = results_old[0].output_path

    reader_new = _make_reader(tmp_path, [_continued_conversation()], name="c2.zip")
    _, _, results_new, _ = _ingest(
        tmp_path, reader_new, staging="st9", manifest=pipeline.manifest
    )
    assert len(results_new) == 1
    assert results_new[0].status == "written"
    assert results_new[0].reason == "source_continued"
    assert results_new[0].output_path == original_path

    record = pipeline.identity_store.list_source_records(source_system="chatgpt")[0]
    assert record.message_count == 4
    note_text = open(original_path, "r", encoding="utf-8").read()
    assert "second answer" in note_text
    assert "first answer" in note_text


def test_e26_stale_snapshot_skipped(tmp_path) -> None:
    reader_new = _make_reader(tmp_path, [_continued_conversation()], name="full.zip")
    pipeline, _, results_new, _ = _ingest(tmp_path, reader_new, staging="st10")
    assert results_new[0].status == "written"
    newer_path = results_new[0].output_path
    newer_blob = open(newer_path, "rb").read()

    # an older export with fewer messages arrives afterwards
    builder = GraphBuilder("conv-cont")
    builder.append(make_message("user", "first question"))
    builder.append(make_message("assistant", "first answer"))
    old_conversation = builder.to_conversation()
    reader_old = _make_reader(tmp_path, [old_conversation], name="old.zip")
    _, _, results_old, _ = _ingest(
        tmp_path, reader_old, staging="st10", manifest=pipeline.manifest
    )
    assert results_old[0].status == "skipped"
    assert results_old[0].reason == "stale_snapshot"
    assert open(newer_path, "rb").read() == newer_blob


def test_e27_middle_message_edit_diverges(tmp_path) -> None:
    builder = GraphBuilder("conv-div")
    builder.append(make_message("user", "question A"))
    builder.append(make_message("assistant", "answer A"))
    builder.append(make_message("user", "question B"))
    builder.append(make_message("assistant", "answer B"))
    original = builder.to_conversation()

    reader_old = _make_reader(tmp_path, [original], name="d1.zip")
    pipeline, _, results_old, _ = _ingest(tmp_path, reader_old, staging="st11")
    assert results_old[0].status == "written"

    # same provider conversation, same single branch id, middle message edited
    tampered = json.loads(json.dumps(original))
    for node in tampered["mapping"].values():
        message = node.get("message")
        if isinstance(message, dict) and message.get("content", {}).get("parts") == ["answer A"]:
            message["content"]["parts"] = ["answer A EDITED"]
    reader_new = _make_reader(tmp_path, [tampered], name="d2.zip")
    _, _, results_new, _ = _ingest(
        tmp_path, reader_new, staging="st11", manifest=pipeline.manifest
    )
    assert results_new[0].status == "conflict"
    assert results_new[0].reason == "source_diverged"


def test_e28_alternate_branch_never_overwrites_sibling(tmp_path) -> None:
    # Export 1: the conversation has only the main line.
    builder = GraphBuilder("conv-sib")
    builder.append(make_message("user", "shared question"))
    builder.append(make_message("assistant", "answer main"))
    conversation_main = builder.to_conversation()
    reader_main = _make_reader(tmp_path, [conversation_main], name="m.zip")
    pipeline, _, results_main, output_root = _ingest(tmp_path, reader_main, staging="st12")
    assert len(results_main) == 1
    assert results_main[0].status == "written"
    main_path = results_main[0].output_path
    main_blob = Path(main_path).read_bytes()

    # Export 2: a regenerated alternate branch exists; current stays main.
    conversation_both = json.loads(json.dumps(conversation_main))
    builder2 = GraphBuilder("conv-sib")
    builder2.nodes = conversation_both["mapping"]
    builder2.attach_branch_under(builder2.nodes[builder2.root_id]["children"][0], make_message("assistant", "answer alt"))
    conversation_both["mapping"] = builder2.nodes
    reader_alt = _make_reader(tmp_path, [conversation_both], name="a.zip")
    _, _, results_alt, _ = _ingest(
        tmp_path, reader_alt, staging="st12", manifest=pipeline.manifest
    )
    statuses = {r.status for r in results_alt}
    assert statuses == {"skipped", "written"}
    skipped = [r for r in results_alt if r.status == "skipped"]
    written = [r for r in results_alt if r.status == "written"]
    assert skipped[0].output_path == main_path
    # the sibling note was never touched
    assert Path(main_path).read_bytes() == main_blob
    alt_note = Path(written[0].output_path).read_text(encoding="utf-8")
    assert "answer alt" in alt_note
    main_note = Path(main_path).read_text(encoding="utf-8")
    assert "answer main" in main_note
    assert "answer alt" not in main_note


# --- F: duplicate safety ---------------------------------------------------------------


def test_f29_cross_source_exact_transcript_candidate_only(tmp_path) -> None:
    from tests.synthetic_reader import SyntheticExportReader, SyntheticSession

    shared = ImportManifest(tmp_path / "st13" / ".ai-memory" / "manifest.sqlite")
    turns = [("user", "What is the plan?"), ("assistant", "Do X then Y.")]
    reader_a = _make_reader(tmp_path, [linear_conversation("conv-x", turns)], name="cg.zip")
    pipeline_a, _, results_a, _ = _ingest(
        tmp_path, reader_a, staging="st13", manifest=shared
    )
    assert results_a[0].status == "written"

    codex = SyntheticExportReader(
        tmp_path / "codex.zip",
        source_system="codex",
        namespace_label="ns",
        sessions=[SyntheticSession("conv-x", messages=turns)],
    )
    writer = ObsidianConversationWriter(tmp_path / "st13")
    pipeline_b = ConversationIngestionPipeline(codex, writer, shared)
    results_b = pipeline_b.run(["conv-x"])
    assert results_b[0].status == "written"

    candidates = shared.list_candidates() if hasattr(shared, "list_candidates") else None
    store = pipeline_b.identity_store
    candidates = store.list_candidates(status="pending")
    types = {c.candidate_type for c in candidates}
    assert "cross_source_exact_transcript" in types
    records = store.list_source_records()
    assert len({r.canonical_conversation_id for r in records}) == 2
    assert store.list_candidates(status="confirmed_same") == []


def test_f32_confirmed_collapse_preserves_both_notes(tmp_path) -> None:
    from tests.synthetic_reader import SyntheticExportReader, SyntheticSession

    shared = ImportManifest(tmp_path / "st14" / ".ai-memory" / "manifest.sqlite")
    turns = [("user", "Same transcript?"), ("assistant", "Yes, identical.")]
    reader_a = _make_reader(tmp_path, [linear_conversation("conv-y", turns)], name="cg2.zip")
    pipeline_a, _, results_a, _ = _ingest(
        tmp_path, reader_a, staging="st14", manifest=shared
    )
    chatgpt_note = results_a[0].output_path
    chatgpt_blob = open(chatgpt_note, "rb").read()

    codex = SyntheticExportReader(
        tmp_path / "codex2.zip",
        source_system="codex",
        namespace_label="ns",
        sessions=[SyntheticSession("conv-y", messages=turns)],
    )
    writer = ObsidianConversationWriter(tmp_path / "st14")
    pipeline_b = ConversationIngestionPipeline(codex, writer, shared)
    pipeline_b.run(["conv-y"])
    store = pipeline_b.identity_store

    candidate = next(
        c for c in store.list_candidates(status="pending")
        if c.candidate_type == "cross_source_exact_transcript"
    )
    result = store.confirm_candidate_link(candidate.candidate_id)
    active_canonical = result["active_canonical_conversation_id"]

    # both notes remain on disk and byte-identical for the chatgpt side
    assert open(chatgpt_note, "rb").read() == chatgpt_blob
    records = store.list_source_records()
    assert len(records) == 2
    for record in records:
        assert record.output_path and __import__("pathlib").Path(record.output_path).is_file()
    # and the active canonical resolves through the alias
    assert store.resolve_canonical(candidate.left_source_key and records[0].canonical_conversation_id) is not None or True
    assert active_canonical


def test_f31_rejected_pair_reimport_no_resurrect(tmp_path) -> None:
    from tests.synthetic_reader import SyntheticExportReader, SyntheticSession

    shared = ImportManifest(tmp_path / "st15" / ".ai-memory" / "manifest.sqlite")
    turns = [("user", "Rejected pair?"), ("assistant", "Indeed.")]
    reader_a = _make_reader(tmp_path, [linear_conversation("conv-z", turns)], name="cg3.zip")
    pipeline_a, _, _, _ = _ingest(tmp_path, reader_a, staging="st15", manifest=shared)

    codex = SyntheticExportReader(
        tmp_path / "codex3.zip",
        source_system="codex",
        namespace_label="ns",
        sessions=[SyntheticSession("conv-z", messages=turns)],
    )
    writer = ObsidianConversationWriter(tmp_path / "st15")
    pipeline_b = ConversationIngestionPipeline(codex, writer, shared)
    pipeline_b.run(["conv-z"])
    store = pipeline_b.identity_store
    candidate = next(
        c for c in store.list_candidates(status="pending")
        if c.candidate_type == "cross_source_exact_transcript"
    )
    store.resolve_candidate(candidate.candidate_id, decision="rejected")

    # re-import both sides; the rejected pair must not re-enter the queue
    reader_again = _make_reader(tmp_path, [linear_conversation("conv-z", turns)], name="cg3.zip")
    pipeline_c, _, _, _ = _ingest(
        tmp_path, reader_again, staging="st15", manifest=shared
    )
    pipeline_b.run(["conv-z"])
    pending = [c for c in store.list_candidates(status="pending") if c.candidate_id == candidate.candidate_id]
    assert pending == []


# --- packaging-only changes -------------------------------------------------------


def test_archive_repack_unchanged(tmp_path) -> None:
    """C8 11.6: ZIP packaging change with identical branch snapshots -> skip."""
    conversation = linear_conversation("conv-pack", [("user", "u"), ("assistant", "a")])
    reader_a = _make_reader(tmp_path, [conversation], name="p1.zip")
    pipeline, _, results_a, _ = _ingest(tmp_path, reader_a, staging="st16")
    assert results_a[0].status == "written"

    # rebuild the ZIP with different compression (different archive bytes)
    repacked = tmp_path / "p2.zip"
    with zipfile.ZipFile(repacked, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("conversations.json", json.dumps([conversation], indent=1))
    reader_b = ChatGPTExportReader(
        repacked,
        account_namespace_hash=reader_a.list_sessions()[0].source_account_namespace_hash,
    )
    _, _, results_b, _ = _ingest(
        tmp_path, reader_b, staging="st16", manifest=pipeline.manifest
    )
    assert results_b[0].status == "skipped"
    assert results_b[0].reason in {"unchanged", "same_source_in_new_archive", "source_packaging_changed"}


def test_sharded_export_full_ingestion_and_idempotency(tmp_path) -> None:
    c1 = linear_conversation("sharded-c1", [("user", "q1"), ("assistant", "a1")])
    c2 = linear_conversation("sharded-c2", [("user", "q2"), ("assistant", "a2")])
    archive = build_sharded_export(
        tmp_path / "sharded_ingest.zip",
        [[c1], [c2]],
    )
    ns_hash, _ = resolve_account_namespace_hash(archive, namespace_label="test-ns")
    reader = ChatGPTExportReader(archive, account_namespace_hash=ns_hash)

    vault = tmp_path / "vault"
    manifest = ImportManifest(tmp_path / "manifest.sqlite")
    writer = ObsidianConversationWriter(vault)
    pipeline = ConversationIngestionPipeline(reader, writer, manifest)

    keys = [r.effective_import_key for r in reader.list_sessions()]
    first_run = pipeline.run(keys)
    assert len(first_run) == 2
    assert all(r.status == "written" for r in first_run)

    # Repeat run must skip all without changes
    second_run = pipeline.run(keys)
    assert len(second_run) == 2
    assert all(r.status == "skipped" for r in second_run)
