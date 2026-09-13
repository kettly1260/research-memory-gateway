from __future__ import annotations

import hashlib
import json
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from research_memory_gateway.conversations import (
    AttachmentInventory,
    CodexExportReader,
    ConversationIngestionPipeline,
    ImportManifest,
    ObsidianConversationWriter,
)


def make_export(
    tmp_path: Path,
    *,
    extra_user_text: str = "",
    title: str = "Prototype Conversation",
    extra_tail_records: list[dict] | None = None,
) -> tuple[Path, str]:
    conversation_id = "11111111-2222-3333-4444-555555555555"
    records = [
        {
            "timestamp": "2026-09-10T01:00:00Z",
            "ordinal": 0,
            "type": "session_meta",
            "payload": {
                "type": "session_meta",
                "id": conversation_id,
                "session_id": conversation_id,
                "timestamp": "2026-09-10T01:00:00Z",
                "cwd": "G:\\LLM\\memory",
                "thread_source": "user",
                "base_instructions": {"text": "large repeated system prompt"},
            },
        },
        {
            "timestamp": "2026-09-10T01:00:01Z",
            "ordinal": 1,
            "type": "response_item",
            "payload": {
                "type": "message",
                "id": "msg-injected",
                "role": "user",
                "content": [{"type": "input_text", "text": "<environment_context>noise</environment_context>"}],
            },
        },
        {
            "timestamp": "2026-09-10T01:00:01Z",
            "ordinal": 2,
            "type": "response_item",
            "payload": {
                "type": "message",
                "id": "msg-user",
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": "Keep exact path G:\\LLM\\memory and 324 nm. " + extra_user_text,
                    },
                    {"type": "input_image", "image_url": "data:image/png;base64,AAAA"},
                ],
                "internal_chat_message_metadata_passthrough": {"turn_id": "turn-1"},
            },
        },
        {
            "timestamp": "2026-09-10T01:00:01Z",
            "ordinal": 3,
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "item": {"type": "UserMessage", "content": "duplicate presentation"},
            },
        },
        {
            "timestamp": "2026-09-10T01:00:02Z",
            "ordinal": 4,
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "call_id": "call-1",
                "arguments": '{"cmd":"echo ERROR_NO_SYSTEM_RESOURCES"}',
            },
        },
        {
            "timestamp": "2026-09-10T01:00:03Z",
            "ordinal": 5,
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call-1",
                "output": "ERROR_NO_SYSTEM_RESOURCES 35984",
            },
        },
        {
            "timestamp": "2026-09-10T01:00:04Z",
            "ordinal": 6,
            "type": "response_item",
            "payload": {"type": "reasoning", "encrypted_content": "opaque"},
        },
        {
            "timestamp": "2026-09-10T01:00:05Z",
            "ordinal": 7,
            "type": "compacted",
            "payload": {"type": "compaction", "encrypted_content": "opaque"},
        },
        {
            "timestamp": "2026-09-10T01:00:06Z",
            "ordinal": 8,
            "type": "response_item",
            "payload": {
                "type": "message",
                "id": "msg-assistant",
                "role": "assistant",
                "phase": "final_answer",
                "content": [{"type": "output_text", "text": "Result preserves 60 min."}],
                "internal_chat_message_metadata_passthrough": {"turn_id": "turn-1"},
            },
        },
    ]
    if extra_tail_records:
        records.extend(extra_tail_records)
    raw = b"\n".join(
        json.dumps(record, ensure_ascii=False).encode("utf-8") for record in records
    ) + b"\n"
    entry = f"files/0001-{conversation_id}/rollout.jsonl"
    manifest = {
        "kind": "codex-session-export",
        "packageVersion": 1,
        "exportedAt": "2026-09-10T02:00:00Z",
        "sessions": [
            {
                "sessionId": conversation_id,
                "title": title,
                "cwd": "G:\\LLM\\memory",
                "updatedAt": 1789002006,
                "relativeRolloutPath": "sessions/rollout.jsonl",
                "fileEntry": entry,
                "sizeBytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "sessionIndexEntry": {
                    "id": conversation_id,
                    "thread_name": title,
                    "updated_at": "2026-09-10T01:00:06Z",
                },
                "sourceInstance": "test",
            }
        ],
    }
    archive_path = tmp_path / "export.zip"
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
        archive.writestr(entry, raw)
    return archive_path, conversation_id


def test_codex_reader_recovers_messages_tools_attachments_and_provenance(tmp_path: Path) -> None:
    archive_path, conversation_id = make_export(tmp_path)
    conversation = CodexExportReader(archive_path).parse(conversation_id)

    assert [message.role for message in conversation.messages] == ["user", "user", "assistant"]
    assert [message.ordinal for message in conversation.messages] == [1, 2, 8]
    assert conversation.messages[0].is_injected is True
    assert conversation.messages[1].turn_id == "turn-1"
    assert len(conversation.tools) == 2
    assert conversation.tools[1].excerpt == "ERROR_NO_SYSTEM_RESOURCES 35984"
    assert len(conversation.attachments) == 1
    assert conversation.attachments[0].locator == "embedded-data"
    assert conversation.special_counts["event_item:UserMessage"] == 1
    assert conversation.special_counts["encrypted_compactions"] == 1
    assert conversation.special_counts["encrypted_reasoning_records"] == 1
    assert conversation.special_counts["injected_user_messages"] == 1
    assert conversation.json_errors == []


def test_writer_is_readable_traceable_and_refuses_unmanaged_collision(tmp_path: Path) -> None:
    archive_path, conversation_id = make_export(tmp_path)
    conversation = CodexExportReader(archive_path).parse(conversation_id)
    writer = ObsidianConversationWriter(tmp_path / "vault")
    output = writer.write(conversation)
    text = output.read_text(encoding="utf-8")

    assert "type: ai-conversation" in text
    assert "completion_status: complete" in text
    assert f"conversation_id: {conversation_id}" in text
    assert "G:\\LLM\\memory" in text
    assert "324 nm" in text
    assert "60 min" in text
    assert "ERROR_NO_SYSTEM_RESOURCES 35984" in text
    assert "data:image/" not in text
    assert ";base64," not in text
    assert "<environment_context>noise" not in text
    assert "source ordinal=2 message_id=msg-user turn_id=turn-1" in text
    assert conversation.ref.source_entry in text
    assert conversation.ref.source_sha256 in text

    output.write_text("human note", encoding="utf-8")
    with pytest.raises(FileExistsError, match="non-managed"):
        writer.write(conversation)


def test_manifest_supports_incremental_skip_change_and_conflict(tmp_path: Path) -> None:
    archive_path, conversation_id = make_export(tmp_path)
    reader = CodexExportReader(archive_path)
    conversation = reader.parse(conversation_id)
    ref = conversation.ref
    output = ObsidianConversationWriter(tmp_path / "vault").write(conversation)
    manifest = ImportManifest(tmp_path / "manifest.sqlite")

    assert manifest.decide(ref, archive_sha256=reader.archive_sha256).reason == "new_conversation"
    manifest.record(
        ref,
        archive_path=str(archive_path),
        archive_sha256=reader.archive_sha256,
        output_path=output,
    )
    assert manifest.decide(ref, archive_sha256=reader.archive_sha256).action == "skip"
    assert manifest.decide(ref, archive_sha256="new-archive-hash").reason == "same_conversation_in_new_archive"

    # 手工区域编辑：保持 skip，不触发 conflict（R3/R4 核心设计）
    original_text = output.read_text(encoding="utf-8")
    output.write_text(original_text + "\nmanual edit in notes section\n", encoding="utf-8")
    decision_manual = manifest.decide(ref, archive_sha256=reader.archive_sha256)
    assert decision_manual.action == "skip"

    # 机器托管区域篡改：触发 conflict 与 managed_output_modified
    tampered_text = original_text.replace("324 nm", "325 nm")
    output.write_text(tampered_text, encoding="utf-8")
    decision = manifest.decide(ref, archive_sha256=reader.archive_sha256)
    assert decision.action == "conflict"
    assert decision.reason == "managed_output_modified"


def test_pipeline_is_idempotent_and_exports_human_readable_manifest(tmp_path: Path) -> None:
    archive_path, conversation_id = make_export(tmp_path)
    output_root = tmp_path / "staging"
    manifest = ImportManifest(output_root / ".ai-memory" / "manifest.sqlite")
    pipeline = ConversationIngestionPipeline(
        CodexExportReader(archive_path),
        ObsidianConversationWriter(output_root),
        manifest,
    )

    first = pipeline.run([conversation_id])
    second = pipeline.run([conversation_id])
    csv_path = manifest.export_csv(output_root / ".ai-memory" / "manifest.csv")

    assert first[0].status == "written"
    assert second[0].status == "skipped"
    csv_text = csv_path.read_text(encoding="utf-8-sig")
    assert "source_entry_sha256" in csv_text
    assert conversation_id in csv_text


def test_pipeline_continued_conversation_reuses_original_path_when_title_changes(tmp_path: Path) -> None:
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()

    archive_v1, conversation_id = make_export(first_dir, title="Original Title")
    output_root = tmp_path / "staging_continued"
    manifest = ImportManifest(output_root / ".ai-memory" / "manifest.sqlite")
    writer = ObsidianConversationWriter(output_root)

    first_pipeline = ConversationIngestionPipeline(
        CodexExportReader(archive_v1),
        writer,
        manifest,
    )
    first = first_pipeline.run([conversation_id])
    assert first[0].status == "written"
    original_path = Path(first[0].output_path)
    original_text = original_path.read_text(encoding="utf-8")
    original_path.write_text(original_text + "\nmanual continuity note\n", encoding="utf-8")

    archive_v2, _ = make_export(
        second_dir,
        extra_tail_records=[
            {
                "timestamp": "2026-09-10T01:10:00Z",
                "ordinal": 9,
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "id": "msg-user-2",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "Follow-up: why did the first run fail?"}
                    ],
                    "internal_chat_message_metadata_passthrough": {"turn_id": "turn-2"},
                },
            },
            {
                "timestamp": "2026-09-10T01:10:05Z",
                "ordinal": 10,
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "id": "msg-assistant-2",
                    "role": "assistant",
                    "phase": "final_answer",
                    "content": [{"type": "output_text", "text": "Continuation answer."}],
                    "internal_chat_message_metadata_passthrough": {"turn_id": "turn-2"},
                },
            },
        ],
        title="Renamed After Continuation",
    )
    second_pipeline = ConversationIngestionPipeline(
        CodexExportReader(archive_v2),
        writer,
        manifest,
    )
    second = second_pipeline.run([conversation_id])

    assert second[0].status == "written"
    assert second[0].reason == "source_continued"
    assert Path(second[0].output_path) == original_path.resolve()
    assert original_path.exists()
    updated_text = original_path.read_text(encoding="utf-8")
    assert "Follow-up: why did the first run fail?" in updated_text
    assert "manual continuity note" in updated_text
    assert len(list(output_root.rglob("*.md"))) == 1
    record = manifest.get_record(conversation_id)
    assert record is not None
    assert Path(record["output_path"]) == original_path.resolve()


def test_pipeline_attachment_allowlist_content_change_and_missing(tmp_path: Path) -> None:
    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()
    attachment = allowed_root / "evidence.png"
    attachment.write_bytes(b"attachment-A")
    archive_path, conversation_id = make_export(
        tmp_path,
        extra_user_text=f"Attachment: {attachment}",
    )
    output_root = tmp_path / "staging_attachment"
    manifest = ImportManifest(output_root / ".ai-memory" / "manifest.sqlite")
    pipeline = ConversationIngestionPipeline(
        CodexExportReader(archive_path),
        ObsidianConversationWriter(output_root),
        manifest,
        attachment_inventory=AttachmentInventory([allowed_root]),
    )

    first = pipeline.run([conversation_id])
    first_record = manifest.get_record(conversation_id)
    assert first[0].status == "written"
    assert first_record is not None
    hash_a = first_record["attachment_inventory_hash"]
    assert hash_a

    attachment.write_bytes(b"attachment-B-is-different")
    second = pipeline.run([conversation_id])
    second_record = manifest.get_record(conversation_id)
    assert second[0].status == "written"
    assert second[0].reason == "attachment_changed"
    assert second_record is not None
    hash_b = second_record["attachment_inventory_hash"]
    assert hash_b and hash_b != hash_a

    attachment.unlink()
    third = pipeline.run([conversation_id])
    third_record = manifest.get_record(conversation_id)
    assert third[0].status == "written"
    assert third[0].reason == "attachment_changed"
    assert third_record is not None
    assert third_record["attachment_inventory_hash"] not in {hash_a, hash_b}


def test_pipeline_failure_recovery_and_resume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    archive_path, conversation_id = make_export(tmp_path)
    output_root = tmp_path / "staging_fail"
    manifest = ImportManifest(output_root / ".ai-memory" / "manifest.sqlite")
    reader = CodexExportReader(archive_path)
    writer = ObsidianConversationWriter(output_root)
    pipeline = ConversationIngestionPipeline(reader, writer, manifest)

    # 1. 模拟 reader.parse() 在首次运行时抛出异常
    real_parse = reader.parse

    def failing_parse(cid: str):
        if cid == conversation_id:
            raise RuntimeError("Simulated parse failure during ingestion")
        return real_parse(cid)

    monkeypatch.setattr(reader, "parse", failing_parse)

    # 执行 pipeline：必须捕获异常，绝不能二次崩溃，并记录 failed_retryable
    results = pipeline.run([conversation_id])
    assert len(results) == 1
    assert results[0].status == "failed_retryable"
    assert "Simulated parse failure" in results[0].error

    # 验证 manifest ledger 记录成功
    dec = manifest.decide(reader.get_session_ref(conversation_id), archive_sha256=reader.archive_sha256)
    assert dec.status == "failed_retryable"
    assert dec.action == "write"

    # 2. 恢复正常 parse 并再次执行 pipeline (resume)
    monkeypatch.setattr(reader, "parse", real_parse)
    retry_results = pipeline.run([conversation_id])
    assert len(retry_results) == 1
    assert retry_results[0].status == "written"
    assert Path(retry_results[0].output_path).exists()

    # 3. 第三次运行：应进入 skipped (unchanged)
    third_results = pipeline.run([conversation_id])
    assert len(third_results) == 1
    assert third_results[0].status == "skipped"

