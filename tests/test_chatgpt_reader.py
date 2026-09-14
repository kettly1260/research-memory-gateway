"""v0.2.6 ChatGPT Export reader tests: schema audit, graph, branches, mapping.

Release-blocker matrix A (reader/schema) and B (branches) plus the C4
NormalizedConversation mapping rules.
"""

from __future__ import annotations

import hashlib
import json
import zipfile

import pytest

from research_memory_gateway.conversations.chatgpt_export import (
    BRANCH_KIND_ALTERNATE,
    BRANCH_KIND_PRIMARY,
    ChatGPTExportError,
    ChatGPTExportReader,
    canonical_branch_snapshot,
    detect_account_guid,
    detect_export_format,
    plan_branches,
    resolve_account_namespace_hash,
)
from tests.chatgpt_fixtures import (
    GraphBuilder,
    TEST_ACCOUNT_GUID,
    build_export, build_sharded_export,
    image_part,
    linear_conversation,
    make_message,
    multimodal_message,
)

NAMESPACE_HASH = "a" * 64


def _reader(tmp_path, conversations, *, name="export.zip", **kwargs):
    archive = build_export(tmp_path / name, conversations, **kwargs)
    return ChatGPTExportReader(archive, account_namespace_hash=NAMESPACE_HASH), archive


# --- A1: conversations.json list parses ---------------------------------------


def test_a1_conversations_list_parses(tmp_path) -> None:
    conversation = linear_conversation("conv-a1", [("user", "hello"), ("assistant", "hi")])
    reader, _ = _reader(tmp_path, [conversation])
    refs = reader.list_sessions()
    assert len(refs) == 1
    ref = refs[0]
    assert ref.conversation_id == "conv-a1"
    assert ref.source_conversation_id == "conv-a1"
    assert ref.source_system == "chatgpt"
    assert ref.source_branch_id
    assert ref.import_key.endswith(f"branch={ref.source_branch_id}")
    conversation_out = reader.parse(ref.import_key)
    assert [m.text for m in conversation_out.messages] == ["hello", "hi"]
    assert [m.role for m in conversation_out.messages] == ["user", "assistant"]


# --- A2: missing conversations.json -> clear unsupported error ----------------


def test_a2_missing_conversations_json_fails_unsupported(tmp_path) -> None:
    archive = tmp_path / "html-only.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("chat.html", "<html><body>export</body></html>")
    reader = ChatGPTExportReader(archive, account_namespace_hash=NAMESPACE_HASH)
    with pytest.raises(ChatGPTExportError) as excinfo:
        reader.list_sessions()
    assert excinfo.value.code == "UNSUPPORTED_EXPORT_FORMAT"


# --- A3: malformed top-level -> fail closed -------------------------------------


def test_a3_malformed_top_level_fails_closed(tmp_path) -> None:
    archive = tmp_path / "malformed.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("conversations.json", json.dumps({"not": "a list"}))
    with pytest.raises(ChatGPTExportError) as excinfo:
        ChatGPTExportReader(archive, account_namespace_hash=NAMESPACE_HASH).list_sessions()
    assert excinfo.value.code == "MALFORMED_EXPORT"

    archive2 = tmp_path / "malformed2.zip"
    with zipfile.ZipFile(archive2, "w") as zf:
        zf.writestr("conversations.json", "{not json at all")
    with pytest.raises(ChatGPTExportError) as excinfo2:
        ChatGPTExportReader(archive2, account_namespace_hash=NAMESPACE_HASH).list_sessions()
    assert excinfo2.value.code == "MALFORMED_EXPORT"


# --- A4: deterministic list order (JSON reorder must not matter) ---------------


def test_a4_list_order_deterministic_under_reorder(tmp_path) -> None:
    conv1 = linear_conversation("conv-1", [("user", "one")], create_time=100.0)
    conv2 = linear_conversation("conv-2", [("user", "two")], create_time=200.0)
    reader_a, archive_a = _reader(tmp_path, [conv1, conv2], name="a.zip")
    reader_b, archive_b = _reader(tmp_path, [conv2, conv1], name="b.zip")

    refs_a = reader_a.list_sessions()
    refs_b = reader_b.list_sessions()
    assert [r.conversation_id for r in refs_a] == [r.conversation_id for r in refs_b]
    assert [r.source_sha256 for r in refs_a] == [r.source_sha256 for r in refs_b]
    assert [r.import_key for r in refs_a] == [r.import_key for r in refs_b]

    parsed_a = reader_a.parse(refs_a[0].import_key)
    parsed_b = reader_b.parse(refs_b[0].import_key)
    assert [m.message_id for m in parsed_a.messages] == [m.message_id for m in parsed_b.messages]


# --- A5: timestamps null / float / int ------------------------------------------


def test_a5_timestamp_shapes(tmp_path) -> None:
    builder = GraphBuilder("conv-ts")
    builder.append(make_message("user", "null time", create_time=None))
    builder.append(make_message("assistant", "float time", create_time=1757800123.5))
    builder.append(make_message("user", "int time", create_time=1757800200))
    conversation = builder.to_conversation()
    reader, _ = _reader(tmp_path, [conversation])
    ref = reader.list_sessions()[0]
    parsed = reader.parse(ref.import_key)
    assert parsed.messages[0].timestamp == ""
    assert parsed.messages[1].timestamp == "2025-09-13T21:48:43.500000+00:00"
    assert parsed.messages[2].timestamp == "2025-09-13T21:50:00+00:00"


# --- A6: unknown content_type never silently dropped ----------------------------


def test_a6_unknown_content_type_counted(tmp_path) -> None:
    builder = GraphBuilder("conv-unknown")
    builder.append(make_message("user", "draw a widget"))
    builder.append(
        make_message(
            "assistant",
            "",
            content={"content_type": "weird_future_widget", "parts": [{"secret": "shape"}]},
        )
    )
    conversation = builder.to_conversation()
    reader, _ = _reader(tmp_path, [conversation])
    ref = reader.list_sessions()[0]
    parsed = reader.parse(ref.import_key)
    assistant = parsed.messages[1]
    assert "[unsupported-content:" in assistant.text
    assert "{'secret'" not in assistant.text  # never a Python dict repr
    meta = parsed.session_meta
    assert meta["unsupported_content_count"] >= 1
    assert meta["content_type_histogram"].get("weird_future_widget") == 1


# --- A7: mapping missing parent is reported, graph still usable -----------------


def test_a7_missing_parent_reported(tmp_path) -> None:
    builder = GraphBuilder("conv-orphan")
    builder.append(make_message("user", "main user"))
    builder.append(make_message("assistant", "main assistant"))
    conversation = builder.to_conversation()
    # Inject an orphan node whose parent does not exist.
    conversation["mapping"]["orphan-node"] = {
        "id": "orphan-node",
        "message": make_message("user", "orphan text"),
        "parent": "does-not-exist",
        "children": [],
    }
    reader, _ = _reader(tmp_path, [conversation])
    graph, plans = plan_branches(conversation)
    assert graph.orphan_node_count == 1
    assert len(plans) >= 1
    audit = reader.schema_audit()
    assert audit["graph_validation"]["orphan_nodes"] == 1


# --- A8: graph cycle rejected -----------------------------------------------------


def test_a8_graph_cycle_rejected(tmp_path) -> None:
    builder = GraphBuilder("conv-cycle")
    builder.append(make_message("user", "before cycle"))
    conversation = builder.to_conversation()
    n1, n2 = "cycle-a", "cycle-b"
    conversation["mapping"][n1] = {
        "id": n1,
        "message": make_message("assistant", "loop a"),
        "parent": n2,
        "children": [n2],
    }
    conversation["mapping"][n2] = {
        "id": n2,
        "message": make_message("assistant", "loop b"),
        "parent": n1,
        "children": [n1],
    }
    conversation["current_node"] = n1
    with pytest.raises(ChatGPTExportError) as excinfo:
        plan_branches(conversation, fail_closed=True)
    assert excinfo.value.code == "GRAPH_CYCLE"

    reader, _ = _reader(tmp_path, [conversation])
    with pytest.raises(ChatGPTExportError):
        reader.list_sessions()


# --- B9: simple linear -> 1 branch -------------------------------------------------


def test_b9_linear_conversation_single_branch(tmp_path) -> None:
    conversation = linear_conversation("conv-linear", [("user", "u1"), ("assistant", "a1")])
    graph, plans = plan_branches(conversation)
    assert len(plans) == 1
    assert plans[0].branch_kind == BRANCH_KIND_PRIMARY


# --- B10: current_node defines the primary branch -----------------------------------


def test_b10_current_node_primary(tmp_path) -> None:
    builder = GraphBuilder("conv-cur")
    user = make_message("user", "edited question")
    answer = make_message("assistant", "edited answer")
    builder.append(user)
    user_node = builder.tail
    builder.append(answer)
    alt_answer = make_message("assistant", "original answer")
    alt_node = builder.attach_branch_under(user_node, alt_answer)
    # current_node points at the ALTERNATE leaf: primary is that branch.
    conversation = builder.to_conversation(current_node=alt_node)
    _, plans = plan_branches(conversation)
    assert plans[0].branch_kind == BRANCH_KIND_PRIMARY
    assert plans[0].branch_id == alt_node
    assert plans[0].path[-1] == alt_node
    assert sum(1 for p in plans if p.branch_kind == BRANCH_KIND_PRIMARY) == 1


# --- B11: regenerate assistant -> 2 branches -----------------------------------------


def test_b11_regenerate_creates_two_branches(tmp_path) -> None:
    builder = GraphBuilder("conv-regen")
    builder.append(make_message("user", "summarize X"))
    regen_parent = builder.tail
    builder.append(make_message("assistant", "summary v2"))
    builder.attach_branch_under(regen_parent, make_message("assistant", "summary v1"))
    conversation = builder.to_conversation()
    _, plans = plan_branches(conversation)
    assert len(plans) == 2
    kinds = sorted(p.branch_kind for p in plans)
    assert kinds == [BRANCH_KIND_ALTERNATE, BRANCH_KIND_PRIMARY]
    texts = set()
    for plan in plans:
        seq = _visible_sequence_for(conversation, plan)
        texts.add(seq[-1][1])
    assert texts == {"summary v1", "summary v2"}


def _visible_sequence_for(conversation, plan):
    from research_memory_gateway.conversations.chatgpt_export import (
        ChatGPTGraph,
        _visible_sequence,
    )

    graph = ChatGPTGraph.build(conversation)
    return _visible_sequence(graph, plan.path)


# --- B12: edited user message -> 2 branches ------------------------------------------


def test_b12_edited_user_message_creates_two_branches(tmp_path) -> None:
    builder = GraphBuilder("conv-edit")
    original_user = make_message("user", "question v1")
    builder.append(original_user)
    assistant = make_message("assistant", "answer v1")
    builder.append(assistant)
    # Edited user message forks from the root.
    edited_user_node = builder.attach_branch_under(
        builder.root_id, make_message("user", "question v2")
    )
    builder.extend_under(
        edited_user_node,
        [make_message("assistant", "answer v2")],
    )
    conversation = builder.to_conversation()
    _, plans = plan_branches(conversation)
    assert len(plans) == 2
    all_first_user = []
    for plan in plans:
        seq = _visible_sequence_for(conversation, plan)
        all_first_user.append(seq[0][1])
    assert set(all_first_user) == {"question v1", "question v2"}


# --- B13: shared prefix does not fabricate a wrong provider id ------------------------


def test_b13_shared_prefix_provider_id_preserved(tmp_path) -> None:
    builder = GraphBuilder("conv-shared")
    builder.append(make_message("user", "shared question"))
    fork = builder.tail
    builder.append(make_message("assistant", "answer A"))
    builder.attach_branch_under(fork, make_message("assistant", "answer B"))
    conversation = builder.to_conversation()
    _, plans = plan_branches(conversation)
    for plan in plans:
        # branch id is the terminal leaf node id, never a composite of the
        # provider conversation id
        assert plan.branch_id in conversation["mapping"]
        assert plan.conversation_id == "conv-shared"
        assert "conv-shared" not in plan.branch_id


# --- B14: branch import keys and source keys are distinct -----------------------------


def test_b14_branch_refs_distinct(tmp_path) -> None:
    builder = GraphBuilder("conv-distinct")
    builder.append(make_message("user", "question"))
    fork = builder.tail
    builder.append(make_message("assistant", "answer A"))
    builder.attach_branch_under(fork, make_message("assistant", "answer B"))
    reader, _ = _reader(tmp_path, [builder.to_conversation()])
    refs = reader.list_sessions()
    assert len(refs) == 2
    keys = {ref.import_key for ref in refs}
    branch_ids = {ref.source_branch_id for ref in refs}
    conversation_ids = {ref.conversation_id for ref in refs}
    assert len(keys) == 2
    assert len(branch_ids) == 2
    assert conversation_ids == {"conv-distinct"}
    # provider conversation id is preserved verbatim on every branch ref
    assert all(ref.source_conversation_id == "conv-distinct" for ref in refs)
    identities = {ref.source_identity().source_key for ref in refs}
    assert len(identities) == 2


# --- B16: branch ordering deterministic ------------------------------------------------


def test_b16_branch_ordering_deterministic(tmp_path) -> None:
    builder = GraphBuilder("conv-order")
    builder.append(make_message("user", "question"))
    fork = builder.tail
    builder.append(make_message("assistant", "answer main"))
    leaf_main = builder.tail
    leaf_1 = builder.attach_branch_under(fork, make_message("assistant", "answer alt 1"))
    builder.extend_under(leaf_1, [make_message("user", "followup alt")])
    conversation = builder.to_conversation(current_node=leaf_main)
    reader_a, archive_a = _reader(tmp_path, [conversation], name="o1.zip")
    reader_b, archive_b = _reader(tmp_path, [conversation], name="o2.zip")
    keys_a = [r.import_key for r in reader_a.list_sessions()]
    keys_b = [r.import_key for r in reader_b.list_sessions()]
    assert keys_a == keys_b
    # primary first
    primary = reader_a.get_session_ref(keys_a[0])
    assert primary.source_branch_id == leaf_main


# --- C4: mapping rules -------------------------------------------------------------------


def test_c4_mapping_identity_and_meta(tmp_path) -> None:
    conversation = linear_conversation(
        "conv-meta",
        [("user", "what model?"), ("assistant", "I am the export's model.")],
        model_slug="synthetic-gpt-test",
    )
    reader, _ = _reader(tmp_path, [conversation])
    ref = reader.list_sessions()[0]
    parsed = reader.parse(ref.import_key)
    meta = parsed.session_meta
    assert meta["source_system"] == "chatgpt"
    assert meta["source_originator"] == "ChatGPT Data Export"
    assert meta["source_surface"] == "chatgpt_export"
    assert meta["provider_conversation_id"] == "conv-meta"
    assert meta["branch_id"] == ref.source_branch_id
    assert meta["branch_kind"] == BRANCH_KIND_PRIMARY
    assert meta["primary_current_node"] == conversation["current_node"]
    # model only from export evidence
    assert meta["model_histogram"] == {"synthetic-gpt-test": 1}
    assert parsed.model_name == "synthetic-gpt-test"
    assert parsed.model_provider == "openai"


def test_c4_no_model_evidence_means_empty_model(tmp_path) -> None:
    conversation = linear_conversation("conv-nomodel", [("user", "u"), ("assistant", "a")])
    reader, _ = _reader(tmp_path, [conversation])
    parsed = reader.parse(reader.list_sessions()[0].import_key)
    assert parsed.model_name == ""
    assert parsed.model_provider == ""
    assert "model_provider" not in parsed.session_meta


def test_c4_system_and_tool_messages_never_become_visible(tmp_path) -> None:
    builder = GraphBuilder("conv-sys")
    builder.append(make_message("system", "You are a helpful assistant."))
    builder.append(make_message("user", "visible question"))
    builder.append(make_message("assistant", "tool call", recipient="tool_zk"))
    builder.append(make_message("tool", "tool result"))
    builder.append(make_message("assistant", "visible answer"))
    conversation = builder.to_conversation()
    reader, _ = _reader(tmp_path, [conversation])
    parsed = reader.parse(reader.list_sessions()[0].import_key)
    visible = [m for m in parsed.messages if not m.is_injected and m.role in {"user", "assistant"}]
    assert [m.text for m in visible] == ["visible question", "visible answer"]
    tool_texts = [t.excerpt for t in parsed.tools]
    assert "tool result" in tool_texts
    # system message is retained as injected, never as visible transcript
    system_messages = [m for m in parsed.messages if m.role == "system"]
    assert len(system_messages) == 1
    assert system_messages[0].is_injected


def test_c4_hidden_user_message_not_in_fingerprints(tmp_path) -> None:
    from research_memory_gateway.conversations.identity import compute_transcript_fingerprints

    builder = GraphBuilder("conv-hidden")
    builder.append(make_message("user", "context junk", hidden=True))
    builder.append(make_message("user", "real question"))
    builder.append(make_message("assistant", "real answer"))
    conversation = builder.to_conversation()
    reader, _ = _reader(tmp_path, [conversation])
    parsed = reader.parse(reader.list_sessions()[0].import_key)
    fingerprints = compute_transcript_fingerprints(parsed)
    assert fingerprints.message_count == 2
    assert parsed.session_meta["hidden_message_count"] == 1


def test_c4_multimodal_content_placeholder_and_attachment(tmp_path) -> None:
    builder = GraphBuilder("conv-mm")
    builder.append(
        multimodal_message(
            "user",
            "what is in this picture?",
            "file-service://file-ABC123",
            message_id="mm-1",
        )
    )
    builder.append(make_message("assistant", "It is a synthetic test image."))
    conversation = builder.to_conversation()
    reader, _ = _reader(tmp_path, [conversation])
    parsed = reader.parse(reader.list_sessions()[0].import_key)
    user_message = parsed.messages[0]
    assert "what is in this picture?" in user_message.text
    assert "[attachment: file-service://file-ABC123]" in user_message.text
    assert len(parsed.attachments) == 1
    attachment = parsed.attachments[0]
    assert attachment.content_type == "image_asset_pointer"
    assert attachment.locator == "file-service://file-ABC123"
    assert attachment.content_hash == hashlib.sha256(
        json.dumps("file-service://file-ABC123", ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


# --- snapshot provenance (C5) --------------------------------------------------------------


def test_c5_snapshot_hash_independent_of_json_ordering(tmp_path) -> None:
    conversation = linear_conversation("conv-snap", [("user", "u"), ("assistant", "a")])
    # reorder mapping dict keys and the conversations list
    reordered = json.loads(json.dumps(conversation))
    reordered["mapping"] = dict(reversed(list(reordered["mapping"].items())))
    reader_a, _ = _reader(tmp_path, [conversation], name="s1.zip")
    reader_b, _ = _reader(tmp_path, [reordered], name="s2.zip")
    ref_a = reader_a.list_sessions()[0]
    ref_b = reader_b.list_sessions()[0]
    assert ref_a.source_sha256 == ref_b.source_sha256
    assert ref_a.source_entry == ref_b.source_entry
    assert ref_a.source_entry.startswith("conversations.json#conversation=conv-snap#branch=")


def test_c5_snapshot_scoped_to_branch(tmp_path) -> None:
    builder = GraphBuilder("conv-scope")
    builder.append(make_message("user", "question"))
    fork = builder.tail
    builder.append(make_message("assistant", "answer main"))
    builder.attach_branch_under(fork, make_message("assistant", "answer alt"))
    conversation = builder.to_conversation()
    graph, plans = plan_branches(conversation)
    assert len(plans) == 2
    primary_snapshot = next(p for p in plans if p.branch_kind == BRANCH_KIND_PRIMARY)
    alternate = next(p for p in plans if p.branch_kind == BRANCH_KIND_ALTERNATE)
    raw_a = canonical_branch_snapshot(conversation, primary_snapshot.branch_id, primary_snapshot.path, graph)
    raw_b = canonical_branch_snapshot(conversation, alternate.branch_id, alternate.path, graph)
    assert raw_a != raw_b
    # the alternate branch snapshot does not include the sibling answer
    assert b"answer main" not in raw_b
    assert b"answer alt" in raw_b


def test_c5_sibling_branch_addition_keeps_primary_snapshot_hash(tmp_path) -> None:
    builder = GraphBuilder("conv-stable")
    builder.append(make_message("user", "question"))
    fork = builder.tail
    builder.append(make_message("assistant", "answer main"))
    conversation_v1 = builder.to_conversation()
    # Later export: a regenerated branch appears (sibling under the same fork)
    conversation_v2 = json.loads(json.dumps(conversation_v1))
    builder2 = GraphBuilder("unused")
    builder2.nodes = conversation_v2["mapping"]
    builder2.attach_branch_under(fork, make_message("assistant", "answer alt"))
    conversation_v2["mapping"] = builder2.nodes

    reader_a, _ = _reader(tmp_path, [conversation_v1], name="v1.zip")
    reader_b, _ = _reader(tmp_path, [conversation_v2], name="v2.zip")
    keys_a = {r.import_key: r for r in reader_a.list_sessions()}
    keys_b = {r.import_key: r for r in reader_b.list_sessions()}
    assert len(keys_a) == 1
    assert len(keys_b) == 2
    primary_key = next(iter(keys_a))
    assert primary_key in keys_b
    assert keys_a[primary_key].source_sha256 == keys_b[primary_key].source_sha256


# --- audit (C1) ------------------------------------------------------------------------------


def test_c1_schema_audit_shapes(tmp_path) -> None:
    builder = GraphBuilder("conv-audit")
    builder.append(make_message("user", "question", create_time=1757800100))
    builder.append(make_message("assistant", "answer", create_time=1757800160.5, model_slug="test-model"))
    conversation = builder.to_conversation()
    reader, _ = _reader(tmp_path, [conversation])
    audit = reader.schema_audit()
    assert audit["conversation_count"] == 1
    assert audit["conversations_top_level_type"] == "list"
    assert audit["content_type_inventory"] == {"text": 2}
    assert audit["role_inventory"] == {"assistant": 1, "user": 1}
    assert audit["timestamp_shapes"] == {"epoch_seconds_float": 1, "epoch_seconds_int": 1}
    assert audit["branch_shape"]["primary_branches"] == 1
    assert audit["graph_validation"]["cycle_conversations"] == 0
    # privacy: no transcript text and no account id in the audit payload
    payload = json.dumps(audit)
    assert "question" not in payload
    assert "answer" not in payload
    assert TEST_ACCOUNT_GUID not in payload


# --- format detection --------------------------------------------------------------------------


def test_detect_formats(tmp_path) -> None:
    conversation = linear_conversation("conv-detect", [("user", "u")])
    chatgpt_zip = build_export(tmp_path / "chatgpt.zip", [conversation])
    assert detect_export_format(chatgpt_zip) == "chatgpt"

    codex_zip = tmp_path / "codex.zip"
    with zipfile.ZipFile(codex_zip, "w") as zf:
        zf.writestr("manifest.json", json.dumps({"kind": "codex-session-export", "sessions": []}))
    assert detect_export_format(codex_zip) == "codex"

    both = tmp_path / "both.zip"
    with zipfile.ZipFile(both, "w") as zf:
        zf.writestr("manifest.json", json.dumps({"kind": "codex-session-export", "sessions": []}))
        zf.writestr("conversations.json", json.dumps([conversation]))
    assert detect_export_format(both) == "ambiguous"

    junk = tmp_path / "junk.zip"
    with zipfile.ZipFile(junk, "w") as zf:
        zf.writestr("random.txt", "nothing")
    assert detect_export_format(junk) == "unknown"


def test_detect_account_guid(tmp_path) -> None:
    conversation = linear_conversation("conv-guid", [("user", "u")])
    archive = build_export(tmp_path / "guid.zip", [conversation], account_id=TEST_ACCOUNT_GUID)
    assert detect_account_guid(archive) == TEST_ACCOUNT_GUID

    no_guid = build_export(
        tmp_path / "noguid.zip", [conversation], account_id=None
    )
    assert detect_account_guid(no_guid) == ""

    email_guid = build_export(
        tmp_path / "emailguid.zip", [conversation], account_id="user@example.com"
    )
    assert detect_account_guid(email_guid) == ""


# --- sharded/numbered exports (v0.2.6 remediation) -----------------------------


def test_sharded_export_auto_discovery(tmp_path) -> None:
    c1 = linear_conversation("conv-s1", [("user", "s1-u"), ("assistant", "s1-a")])
    c2 = linear_conversation("conv-s2", [("user", "s2-u"), ("assistant", "s2-a")])
    sharded_zip = build_sharded_export(
        tmp_path / "sharded.zip",
        [[c1], [c2]],
    )
    assert detect_export_format(sharded_zip) == "chatgpt"

    ns_hash, _ = resolve_account_namespace_hash(sharded_zip, namespace_label="test-ns")
    reader = ChatGPTExportReader(sharded_zip, account_namespace_hash=ns_hash)
    shards = reader.conversation_shards()
    assert shards == ["conversations-001.json", "conversations-002.json"]

    audit = reader.schema_audit()
    assert audit["conversation_shard_count"] == 2
    assert audit["conversation_shards"] == ["conversations-001.json", "conversations-002.json"]
    assert audit["conversation_count"] == 2

    sessions = reader.list_sessions()
    assert len(sessions) == 2
    assert {s.conversation_id for s in sessions} == {"conv-s1", "conv-s2"}


def test_sharded_export_file_order_independence(tmp_path) -> None:
    c1 = linear_conversation("conv-ord1", [("user", "u1")], create_time=1757800010.0)
    c2 = linear_conversation("conv-ord2", [("user", "u2")], create_time=1757800020.0)

    # Archive A: shard 1 written first, then shard 2
    zip_a = tmp_path / "order_a.zip"
    build_sharded_export(
        zip_a,
        {"conversations-001.json": [c1], "conversations-002.json": [c2]},
    )

    # Archive B: shard 2 written first, then shard 1
    zip_b = tmp_path / "order_b.zip"
    build_sharded_export(
        zip_b,
        {"conversations-002.json": [c2], "conversations-001.json": [c1]},
    )

    ns_hash = "fixed_test_ns"
    reader_a = ChatGPTExportReader(zip_a, account_namespace_hash=ns_hash)
    reader_b = ChatGPTExportReader(zip_b, account_namespace_hash=ns_hash)

    assert reader_a.conversation_shards() == reader_b.conversation_shards()

    sessions_a = reader_a.list_sessions()
    sessions_b = reader_b.list_sessions()

    assert [s.import_key for s in sessions_a] == [s.import_key for s in sessions_b]
    assert [s.source_sha256 for s in sessions_a] == [s.source_sha256 for s in sessions_b]


def test_sharded_numeric_shard_sorting(tmp_path) -> None:
    c1 = linear_conversation("conv-num1", [("user", "u1")])
    c2 = linear_conversation("conv-num2", [("user", "u2")])
    c10 = linear_conversation("conv-num10", [("user", "u10")])

    zip_num = tmp_path / "numeric_sort.zip"
    build_sharded_export(
        zip_num,
        {
            "conversations-10.json": [c10],
            "conversations-2.json": [c2],
            "conversations-1.json": [c1],
        },
    )

    ns_hash = "fixed_test_ns"
    reader = ChatGPTExportReader(zip_num, account_namespace_hash=ns_hash)
    assert reader.conversation_shards() == [
        "conversations-1.json",
        "conversations-2.json",
        "conversations-10.json",
    ]


def test_sharded_duplicate_conversation_identical_dedup(tmp_path) -> None:
    c1 = linear_conversation("conv-dup", [("user", "hello"), ("assistant", "world")])

    zip_dup = build_sharded_export(
        tmp_path / "dup_identical.zip",
        [[c1], [c1]],
    )

    ns_hash = "fixed_test_ns"
    reader = ChatGPTExportReader(zip_dup, account_namespace_hash=ns_hash)
    sessions = reader.list_sessions()
    assert len(sessions) == 1
    assert sessions[0].conversation_id == "conv-dup"
    assert any("duplicate_conversation_deduped:conv-dup" in w for w in reader.schema_warnings)


def test_sharded_duplicate_conversation_conflicting_fail_closed(tmp_path) -> None:
    c1_v1 = linear_conversation("conv-conflict", [("user", "original")])
    c1_v2 = linear_conversation("conv-conflict", [("user", "different")])

    zip_conflict = build_sharded_export(
        tmp_path / "dup_conflict.zip",
        [[c1_v1], [c1_v2]],
    )

    ns_hash = "fixed_test_ns"
    reader = ChatGPTExportReader(zip_conflict, account_namespace_hash=ns_hash)
    with pytest.raises(ChatGPTExportError) as exc_info:
        reader.list_sessions()
    assert exc_info.value.code == "DUPLICATE_CONVERSATION_ID"


def test_sharded_snapshot_hash_independent_of_packaging(tmp_path) -> None:
    conv = linear_conversation("conv-pkg", [("user", "query"), ("assistant", "reply")])

    zip_single = build_export(tmp_path / "single.zip", [conv])
    zip_sharded = build_sharded_export(
        tmp_path / "sharded_pkg.zip",
        {
            "conversations-001.json": [],
            "conversations-002.json": [],
            "conversations-003.json": [conv],
        },
    )

    ns_hash = "fixed_test_ns"
    reader_s = ChatGPTExportReader(zip_single, account_namespace_hash=ns_hash)
    reader_m = ChatGPTExportReader(zip_sharded, account_namespace_hash=ns_hash)

    ref_s = reader_s.list_sessions()[0]
    ref_m = reader_m.list_sessions()[0]

    assert ref_s.source_sha256 == ref_m.source_sha256
    assert ref_s.source_size_bytes == ref_m.source_size_bytes
    assert ref_s.source_entry == ref_m.source_entry
    assert ref_s.import_key == ref_m.import_key
