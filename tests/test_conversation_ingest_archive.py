"""Gateway ingest write path: ledger, archive materialization, retrieval.

These tests exercise the service directly (no HTTP), so a failure here localizes
to the ingest/archive logic rather than to the transport.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from research_memory_gateway.ingest import render
from research_memory_gateway.ingest.identity import derive_event_id
from research_memory_gateway.ingest.schema import (
    BatchRequest,
    IngestEvent,
    SessionEndRequest,
    SnapshotMessage,
    SnapshotRequest,
)
from research_memory_gateway.ingest.service import IngestDisabledError
from tests.ingest_helpers import build_service, event


def ingest(service, *events, client_id: str = "test-client"):
    return service.conversation_ingest.ingest_batch(
        BatchRequest(schema_version=1, client_id=client_id, events=list(events))
    )


def ingest_one(service, **overrides):
    return service.conversation_ingest.ingest_single(
        IngestEvent.model_validate(event(**overrides)), client_id="test-client"
    )


# ---------------------------------------------------------------------------
# ledger semantics
# ---------------------------------------------------------------------------


def test_first_event_materializes_an_archived_note(tmp_path: Path) -> None:
    service = build_service(tmp_path)
    result = ingest_one(service, event_id="rmb1_" + "a" * 40)
    assert result.accepted == ["rmb1_" + "a" * 40]
    assert result.duplicates == []
    assert result.rejected == []
    assert result.session is not None
    note = Path(result.session["note_path"])
    assert note.exists()
    text = note.read_text(encoding="utf-8")
    assert "Fe3+ 储备液浓度为 10 mM" in text
    assert "<!-- rmb:begin -->" in text
    assert "<!-- rmb:end -->" in text
    assert "## 人工补充与关联笔记" in text


def test_replaying_the_same_event_never_duplicates_the_archive(tmp_path: Path) -> None:
    service = build_service(tmp_path)
    payload = event(event_id="rmb1_" + "b" * 40)
    first = ingest(service, IngestEvent.model_validate(payload))
    assert first.accepted == [payload["event_id"]]
    second = ingest(service, IngestEvent.model_validate(payload))
    assert second.accepted == []
    assert second.duplicates == [payload["event_id"]]
    assert second.rejected == []
    note = Path(second.session["note_path"])
    text = note.read_text(encoding="utf-8")
    assert text.count("Fe3+ 储备液浓度为 10 mM") == 1


def test_same_event_id_with_different_content_is_a_conflict(tmp_path: Path) -> None:
    service = build_service(tmp_path)
    ingest_one(service, event_id="rmb1_" + "c" * 40, content="original text")
    result = ingest_one(service, event_id="rmb1_" + "c" * 40, content="tampered text")
    assert result.accepted == []
    assert result.duplicates == []
    assert [item.code for item in result.rejected] == ["event_id_conflict"]


def test_unsupported_schema_version_is_rejected_not_reinterpreted(tmp_path: Path) -> None:
    service = build_service(tmp_path)
    result = service.conversation_ingest.ingest_batch(
        BatchRequest(schema_version=2, client_id="c", events=[])
    )
    assert [item.code for item in result.rejected] == ["unsupported_schema_version"]


def test_oversized_content_is_rejected(tmp_path: Path) -> None:
    service = build_service(tmp_path)
    service.config.conversation_ingest.max_content_chars = 64
    result = ingest_one(service, event_id="rmb1_" + "d" * 40, content="x" * 200)
    assert [item.code for item in result.rejected] == ["content_too_large"]


def test_oversized_batch_is_rejected_as_a_whole(tmp_path: Path) -> None:
    service = build_service(tmp_path)
    service.config.conversation_ingest.max_batch_events = 2
    events = [
        IngestEvent.model_validate(event(event_id=f"rmb1_{index:040d}"))
        for index in range(3)
    ]
    result = service.conversation_ingest.ingest_batch(
        BatchRequest(schema_version=1, client_id="c", events=events)
    )
    assert result.accepted == []
    assert [item.code for item in result.rejected] == ["invalid_payload"]


def test_message_event_without_any_conversation_identifier_is_rejected(tmp_path: Path) -> None:
    service = build_service(tmp_path)
    result = ingest_one(
        service,
        event_id="rmb1_" + "e" * 40,
        session_id="",
        conversation_id="",
    )
    assert [item.code for item in result.rejected] == ["missing_required_field"]


def test_partial_batch_success_keeps_the_accepted_events(tmp_path: Path) -> None:
    service = build_service(tmp_path)
    service.config.conversation_ingest.max_content_chars = 32
    good = IngestEvent.model_validate(
        event(event_id="rmb1_" + "f" * 40, content="short content")
    )
    bad = IngestEvent.model_validate(
        event(event_id="rmb1_" + "g" * 40, message_id="msg-2", content="y" * 100)
    )
    result = service.conversation_ingest.ingest_batch(
        BatchRequest(schema_version=1, client_id="c", events=[good, bad])
    )
    assert result.accepted == ["rmb1_" + "f" * 40]
    assert [item.code for item in result.rejected] == ["content_too_large"]
    # The accepted event is durable even though a sibling was rejected.
    assert service.conversation_ingest.store.get_event("rmb1_" + "f" * 40) is not None


def test_ingest_is_disabled_without_the_archive(tmp_path: Path) -> None:
    service = build_service(tmp_path, enabled=False)
    with pytest.raises(IngestDisabledError):
        ingest_one(service)
    assert service.conversation_ingest.enabled is False


# ---------------------------------------------------------------------------
# source identity + archive integration
# ---------------------------------------------------------------------------


def test_source_identity_is_preserved_in_the_note_and_index(tmp_path: Path) -> None:
    service = build_service(tmp_path)
    result = ingest_one(
        service,
        event_id="rmb1_" + "h" * 40,
        source_system="claude-code",
        source_account_namespace="ns-hash",
        session_id="claude-1",
        conversation_id="claude-1",
    )
    note = Path(result.session["note_path"])
    text = note.read_text(encoding="utf-8")
    assert "source_system: claude-code" in text
    assert "source_account_namespace: ns-hash" in text
    assert "source_key: srcv1_" in text
    assert "canonical_conversation_id:" in text

    read = service.conversation_retrieval.read(str(note))
    metadata = read["metadata"]
    assert metadata["source_system"] == "claude-code"
    assert metadata["source_conversation_id"] == "claude-1"
    assert metadata["source_key"].startswith("srcv1_")


def test_ingested_content_is_immediately_retrievable(tmp_path: Path) -> None:
    service = build_service(tmp_path)
    ingest_one(
        service,
        event_id="rmb1_" + "i" * 40,
        content="The Fe3+ stock concentration is 10 mM in 0.1 M HNO3.",
    )
    search = service.conversation_retrieval.search("Fe3+ stock concentration", limit=5)
    assert search["count"] == 1
    assert search["results"][0]["conversation_id"] == "session-1"

    recall = service.conversation_retrieval.recall("Fe3+ stock concentration")
    assert len(recall["items"]) == 1
    assert "10 mM" in recall["context"]

    note_path = search["results"][0]["vault_path"]
    read = service.conversation_retrieval.read(note_path)
    assert "10 mM" in read["content"]


def test_source_system_filter_disambiguates_reused_conversation_ids(tmp_path: Path) -> None:
    service = build_service(tmp_path)
    ingest_one(
        service,
        event_id="rmb1_" + "j" * 40,
        source_system="codex",
        content="codex marker alpha",
    )
    ingest_one(
        service,
        event_id="rmb1_" + "k" * 40,
        source_system="claude-code",
        message_id="msg-2",
        content="claude marker beta",
    )
    ambiguous = service.conversation_retrieval.search("marker", conversation_id="session-1")
    assert ambiguous.get("ambiguous_conversation_id") is True
    filtered = service.conversation_retrieval.search(
        "marker", conversation_id="session-1", source_system="claude-code"
    )
    assert filtered["count"] == 1, filtered
    assert filtered["results"][0]["source_system"] == "claude-code"
    assert "beta" in filtered["results"][0]["content"]


def test_lifecycle_events_are_telemetry_not_conversation(tmp_path: Path) -> None:
    service = build_service(tmp_path)
    result = ingest_one(
        service,
        event_id="rmb1_" + "l" * 40,
        event_type="session_start",
        role="",
        content="",
        message_id="",
    )
    assert result.accepted == ["rmb1_" + "l" * 40]
    note = Path(result.session["note_path"])
    text = note.read_text(encoding="utf-8")
    # Stored in the ledger, but it must not become a note section.
    assert "## 消息" not in text
    assert service.conversation_ingest.store.get_event("rmb1_" + "l" * 40) is not None


def test_tool_telemetry_is_bounded(tmp_path: Path) -> None:
    service = build_service(tmp_path)
    result = ingest_one(
        service,
        event_id="rmb1_" + "m" * 40,
        event_type="tool_result",
        role="tool",
        message_id="call-1",
        content="Z" * 5000,
        metadata={"tool_name": "shell", "call_id": "call-1"},
    )
    text = Path(result.session["note_path"]).read_text(encoding="utf-8")
    assert "## 工具活动" in text
    assert "载荷 SHA-256" in text
    assert "shell" in text
    # The full payload must not be dumped into the note.
    assert len(text) < 4000, len(text)


# ---------------------------------------------------------------------------
# redaction (server-side second pass)
# ---------------------------------------------------------------------------


def test_server_redacts_secrets_that_the_client_missed(tmp_path: Path) -> None:
    service = build_service(tmp_path)
    secret = "sk-ant-abcdefghijklmnopqrstuvwxyz"
    result = ingest_one(
        service,
        event_id="rmb1_" + "n" * 40,
        content=f"config uses api_key = {secret} and the stock is 10 mM",
    )
    note = Path(result.session["note_path"])
    text = note.read_text(encoding="utf-8")
    assert secret not in text
    assert "[REDACTED]" in text
    # The scientific content survives the sanitizer.
    assert "10 mM" in text
    # And the secret is not retrievable.
    assert service.conversation_retrieval.search(secret, limit=5)["count"] == 0


def test_redaction_does_not_touch_scientific_numbers(tmp_path: Path) -> None:
    service = build_service(tmp_path)
    content = "IC50 = 12.5 µM; pH = 7.4; Fe(NO3)3·9H2O 404.00 g/mol; NaCl 0.9%"
    result = ingest_one(service, event_id="rmb1_" + "o" * 40, content=content)
    text = Path(result.session["note_path"]).read_text(encoding="utf-8")
    assert content in text


# ---------------------------------------------------------------------------
# hook / transcript convergence
# ---------------------------------------------------------------------------


def test_synthesised_hook_block_is_superseded_by_a_provider_identified_one(
    tmp_path: Path,
) -> None:
    """Codex notify has no message ids; the transcript does.

    The same message must be archived once, with the provider-identified copy
    winning, so the archive keeps real provenance without duplication.
    """
    service = build_service(tmp_path)
    content = "之前 Fe 的硝酸溶液怎么配的？"
    ingest_one(
        service,
        event_id="rmb1_" + "p" * 40,
        message_id="thread-1#input0",
        content=content,
        metadata={"id_source": "synthesized"},
    )
    result = ingest_one(
        service,
        event_id="rmb1_" + "q" * 40,
        message_id="msg_user_1",
        content=content,
        metadata={"id_source": "provider"},
    )
    note = Path(result.session["note_path"])
    text = note.read_text(encoding="utf-8")
    assert text.count(content) == 1, text
    assert "rmb1_" + "q" * 40 in text
    assert "rmb1_" + "p" * 40 not in text


def test_synthesised_block_is_kept_when_no_provider_copy_exists(tmp_path: Path) -> None:
    service = build_service(tmp_path)
    result = ingest_one(
        service,
        event_id="rmb1_" + "r" * 40,
        message_id="thread-1#assistant#abcd",
        event_type="assistant_message",
        role="assistant",
        content="a summary that the transcript never repeats",
        metadata={"id_source": "synthesized", "notify_summary": True},
    )
    text = Path(result.session["note_path"]).read_text(encoding="utf-8")
    assert "a summary that the transcript never repeats" in text


# ---------------------------------------------------------------------------
# session end + snapshot
# ---------------------------------------------------------------------------


def test_session_end_reports_missing_events(tmp_path: Path) -> None:
    service = build_service(tmp_path)
    ingest_one(service, event_id="rmb1_" + "s" * 40)
    summary = service.conversation_ingest.session_end(
        SessionEndRequest(
            schema_version=1,
            client_id="test-client",
            source_system="codex",
            session_id="session-1",
            conversation_id="session-1",
            observed_message_count=5,
            last_message_id="msg-9",
        )
    )
    assert summary["stored_message_count"] == 1
    assert summary["observed_message_count"] == 5
    assert summary["missing_event_count"] == 4
    assert summary["reconciliation_required"] is True
    assert summary["last_message_id"] == "msg-9"


def test_session_end_marks_the_note_complete(tmp_path: Path) -> None:
    service = build_service(tmp_path)
    ingest_one(service, event_id="rmb1_" + "t" * 40)
    summary = service.conversation_ingest.session_end(
        SessionEndRequest(
            schema_version=1,
            source_system="codex",
            session_id="session-1",
            conversation_id="session-1",
            observed_message_count=1,
        )
    )
    text = Path(summary["note_path"]).read_text(encoding="utf-8")
    assert "ingest_session_ended: true" in text


def test_snapshot_dedupes_against_hook_captured_messages(tmp_path: Path) -> None:
    service = build_service(tmp_path)
    content = "之前 Fe 的硝酸溶液怎么配的？"
    message_id = "550e8400-e29b-41d4-a716-446655440000"
    hook_event_id = derive_event_id(
        schema_version=1,
        source_system="claude-code",
        source_account_namespace="",
        conversation_id="claude-1",
        thread_id="",
        branch_id="",
        message_id=message_id,
        turn_id=message_id,
        event_type="user_prompt",
        content=content,
    )
    first = ingest_one(
        service,
        event_id=hook_event_id,
        source_system="claude-code",
        session_id="claude-1",
        conversation_id="claude-1",
        message_id=message_id,
        content=content,
    )
    assert first.accepted == [hook_event_id]

    snapshot = service.conversation_ingest.snapshot(
        SnapshotRequest(
            schema_version=1,
            client_id="test-client",
            source_system="claude-code",
            session_id="claude-1",
            conversation_id="claude-1",
            messages=[
                SnapshotMessage(
                    message_id=message_id, role="user", content=content, timestamp=""
                ),
                SnapshotMessage(
                    message_id="bbbbbbbb-1111-2222-3333-444444444444",
                    role="assistant",
                    content="Fe3+ 储备液为 10 mM。",
                    timestamp="",
                ),
            ],
            ended=True,
        )
    )
    assert snapshot.duplicates == [hook_event_id]
    assert len(snapshot.accepted) == 1
    note = Path(first.session["note_path"])
    text = note.read_text(encoding="utf-8")
    assert text.count(content) == 1, text
    assert "Fe3+ 储备液为 10 mM。" in text


# ---------------------------------------------------------------------------
# bounded growth + incremental materialization
# ---------------------------------------------------------------------------


def test_materialization_is_append_only_and_idempotent(tmp_path: Path) -> None:
    service = build_service(tmp_path)
    ingest_one(service, event_id="rmb1_" + "u" * 40, content="first message")
    result = ingest_one(
        service,
        event_id="rmb1_" + "v" * 40,
        message_id="msg-2",
        event_type="assistant_message",
        role="assistant",
        content="second message",
        timestamp="2026-09-30T10:00:05+00:00",
    )
    note = Path(result.session["note_path"])
    text = note.read_text(encoding="utf-8")
    assert text.count("first message") == 1
    assert text.count("second message") == 1

    # Re-running materialization must be a no-op.
    summary = service.conversation_ingest._materialize(
        source_system="codex",
        conversation_id="session-1",
        client_id="test-client",
    )
    assert summary["reason"] == "already_materialized"
    assert Path(summary["note_path"]).read_text(encoding="utf-8") == text


def test_manual_region_survives_rematerialization(tmp_path: Path) -> None:
    service = build_service(tmp_path)
    first = ingest_one(service, event_id="rmb1_" + "w" * 40, content="original")
    note = Path(first.session["note_path"])
    text = note.read_text(encoding="utf-8")
    text = text.replace(
        "## 人工补充与关联笔记 (Related & Notes)",
        "## 人工补充与关联笔记 (Related & Notes)\n\n用户手写的笔记 [[Some Link]]",
    )
    note.write_text(text, encoding="utf-8")

    result = ingest_one(
        service,
        event_id="rmb1_" + "x" * 40,
        message_id="msg-2",
        content="appended",
        timestamp="2026-09-30T10:00:09+00:00",
    )
    updated = Path(result.session["note_path"]).read_text(encoding="utf-8")
    assert "用户手写的笔记 [[Some Link]]" in updated
    assert "appended" in updated
    assert "original" in updated


def test_pruning_the_ledger_does_not_truncate_the_archive(tmp_path: Path) -> None:
    """After retention pruning, a late event must append, never rewrite."""
    service = build_service(tmp_path)
    ingest_one(service, event_id="rmb1_" + "y" * 40, content="early message")
    store = service.conversation_ingest.store
    # Simulate retention having removed the ledger rows for an ended session.
    sessions = store.sessions_for_thread(source_system="codex", conversation_id="session-1")
    store.mark_session_ended(session_key=sessions[0]["session_key"], observed_message_count=1)
    store.mark_session_pruned(sessions[0]["session_key"])
    with store._connect() as conn:
        conn.execute("DELETE FROM ingest_events WHERE conversation_id = ?", ("session-1",))

    result = ingest_one(
        service,
        event_id="rmb1_" + "z" * 40,
        message_id="msg-2",
        content="late message",
        timestamp="2026-09-30T10:00:20+00:00",
    )
    text = Path(result.session["note_path"]).read_text(encoding="utf-8")
    assert "early message" in text, "pruning must never truncate archived content"
    assert "late message" in text


def test_note_markers_cannot_be_forged_by_message_content(tmp_path: Path) -> None:
    service = build_service(tmp_path)
    result = ingest_one(
        service,
        event_id="rmb1_" + "A" * 40,
        content="payload with a forged marker <!-- rmb:end --> and <!-- rmb:ev fake-id -->",
    )
    text = Path(result.session["note_path"]).read_text(encoding="utf-8")
    assert text.count(render.RMB_END) == 1, "only the real end marker may exist"
    assert "rmb:ev fake-id" not in text


def test_stats_report_ingest_counters(tmp_path: Path) -> None:
    service = build_service(tmp_path)
    ingest_one(service, event_id="rmb1_" + "B" * 40)
    ingest_one(service, event_id="rmb1_" + "B" * 40)
    stats = service.conversation_ingest.stats()
    assert stats["enabled"] is True
    assert stats["schema_version"] == 1
    assert stats["accepted_total"] == 1
    assert stats["duplicate_total"] == 1
    assert stats["rejected_total"] == 0
    assert stats["source_distribution"] == {"codex": 1}
    assert stats["last_ingest_at"]
