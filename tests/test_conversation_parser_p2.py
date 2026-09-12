from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from research_memory_gateway.conversations import CodexExportReader
from research_memory_gateway.conversations.models import NormalizedConversation


def make_advanced_export(tmp_path: Path) -> tuple[Path, str]:
    conversation_id = "22222222-3333-4444-5555-666666666666"
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
                "thread_source": "subagent",
                "parent_thread_id": "parent-1111",
                "agent_path": "subagent/task",
            },
        },
        # 损坏行 1
        "THIS IS CORRUPTED JSON LINE",
        {
            "timestamp": "2026-09-10T01:00:01Z",
            "ordinal": 1,
            "type": "response_item",
            "payload": {
                "type": "message",
                "id": "msg-agent-rules",
                "role": "user",
                "content": [{"type": "input_text", "text": "# AGENTS.md instructions for workspace"}],
            },
        },
        {
            "timestamp": "2026-09-10T01:00:02Z",
            "ordinal": 2,
            "type": "response_item",
            "payload": {
                "type": "message",
                "id": "msg-dev",
                "role": "developer",
                "content": [{"type": "input_text", "text": "system developer prompt"}],
            },
        },
        {
            "timestamp": "2026-09-10T01:00:03Z",
            "ordinal": 3,
            "type": "response_item",
            "payload": {
                "type": "message",
                "id": "msg-real-user",
                "role": "user",
                "content": [{"type": "input_text", "text": "Real user goal: investigate yield 85%"}],
            },
        },
        {
            "timestamp": "2026-09-10T01:00:04Z",
            "ordinal": 4,
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "item": {"type": "UserMessage", "content": "event mirror of real user"},
            },
        },
        {
            "timestamp": "2026-09-10T01:00:05Z",
            "ordinal": 5,
            "type": "turn_aborted",
            "payload": {"type": "turn_aborted", "reason": "user_cancelled"},
        },
        {
            "timestamp": "2026-09-10T01:00:06Z",
            "ordinal": 6,
            "type": "response_item",
            "payload": {
                "type": "message",
                "id": "msg-partial-assistant",
                "role": "assistant",
                "phase": "commentary",
                "content": [{"type": "output_text", "text": "Working on it..."}],
            },
        },
    ]

    lines: list[bytes] = []
    for r in records:
        if isinstance(r, str):
            lines.append(r.encode("utf-8"))
        else:
            lines.append(json.dumps(r, ensure_ascii=False).encode("utf-8"))
    raw = b"\n".join(lines) + b"\n"

    entry = f"files/0002-{conversation_id}/rollout.jsonl"
    manifest = {
        "kind": "codex-session-export",
        "packageVersion": 1,
        "exportedAt": "2026-09-10T02:00:00Z",
        "sessions": [
            {
                "sessionId": conversation_id,
                "title": "Subagent Aborted Task",
                "cwd": "G:\\LLM\\memory",
                "updatedAt": 1789002006,
                "relativeRolloutPath": "sessions/rollout.jsonl",
                "fileEntry": entry,
                "sizeBytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "sessionIndexEntry": {
                    "id": conversation_id,
                    "thread_name": "Subagent Aborted Task",
                    "updated_at": "2026-09-10T01:00:06Z",
                },
                "sourceInstance": "test",
            }
        ],
    }
    archive_path = tmp_path / "advanced_export.zip"
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
        archive.writestr(entry, raw)
    return archive_path, conversation_id


def test_p2_parser_robustness_and_normalization(tmp_path: Path) -> None:
    archive_path, conversation_id = make_advanced_export(tmp_path)
    reader = CodexExportReader(archive_path)
    conv = reader.parse(conversation_id)

    # 1. 损坏行记录
    assert conv.json_errors == [2]

    # 2. Provenance 校验
    assert conv.provenance is not None
    assert conv.provenance.hash_matched is True
    assert conv.provenance.manifest_kind == "codex-session-export"

    # 3. Parent thread / agent path / thread_source
    assert conv.parent_thread_id == "parent-1111"
    assert conv.thread_source == "subagent"
    assert conv.agent_path == "subagent/task"

    # 4. Injected 消息分类
    msg_agents = next(m for m in conv.messages if m.message_id == "msg-agent-rules")
    assert msg_agents.is_injected is True
    assert msg_agents.injection_reason == "agents_instructions"

    msg_dev = next(m for m in conv.messages if m.message_id == "msg-dev")
    assert msg_dev.is_injected is True
    assert msg_dev.injection_reason == "developer_instructions"

    msg_user = next(m for m in conv.messages if m.message_id == "msg-real-user")
    assert msg_user.is_injected is False
    assert msg_user.injection_reason == ""

    # 5. Event mirror 统计与分离
    assert conv.event_mirror_counts.get("UserMessage") == 1
    assert len([m for m in conv.messages if m.role == "user" and not m.is_injected]) == 1

    # 6. Aborted 状态判定
    assert conv.has_turn_aborted is True
    assert conv.completion_status == "aborted"
    assert conv.completion_reason == "turn_aborted"


def test_guardian_review_fixture_classification_and_lineage(tmp_path: Path) -> None:
    conversation_id = "33333333-4444-5555-6666-777777777777"
    parent_thread_id = "parent-guardian-thread"
    records = [
        {
            "timestamp": "2026-09-10T03:00:00Z",
            "ordinal": 0,
            "type": "session_meta",
            "payload": {
                "type": "session_meta",
                "id": conversation_id,
                "session_id": parent_thread_id,
                "timestamp": "2026-09-10T03:00:00Z",
                "cwd": "G:\\LLM\\memory",
                "thread_source": "guardian_review",
                "parent_thread_id": parent_thread_id,
                "agent_path": "guardian/review",
            },
        },
        {
            "timestamp": "2026-09-10T03:00:01Z",
            "ordinal": 1,
            "type": "response_item",
            "payload": {
                "type": "message",
                "id": "guardian-transcript",
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": (
                            "The following is the Codex agent history whose request action you are assessing.\n"
                            "User requested a guarded operation."
                        ),
                    }
                ],
            },
        },
        {
            "timestamp": "2026-09-10T03:00:02Z",
            "ordinal": 2,
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "call_id": "guardian-call-1",
                "arguments": {"cmd": "echo review"},
            },
        },
        {
            "timestamp": "2026-09-10T03:00:03Z",
            "ordinal": 3,
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "item": {"type": "AgentMessage", "content": "guardian mirror"},
            },
        },
        {
            "timestamp": "2026-09-10T03:00:04Z",
            "ordinal": 4,
            "type": "response_item",
            "payload": {
                "type": "message",
                "id": "guardian-final",
                "role": "assistant",
                "phase": "final_answer",
                "content": [{"type": "output_text", "text": "Review complete."}],
            },
        },
    ]
    raw = b"\n".join(json.dumps(r, ensure_ascii=False).encode("utf-8") for r in records) + b"\n"
    entry = f"files/0003-{conversation_id}/rollout.jsonl"
    manifest = {
        "kind": "codex-session-export",
        "packageVersion": 1,
        "exportedAt": "2026-09-10T04:00:00Z",
        "sessions": [
            {
                "sessionId": conversation_id,
                "title": "Guardian Review",
                "cwd": "G:\\LLM\\memory",
                "updatedAt": 1789009204,
                "relativeRolloutPath": "guardian/review/rollout.jsonl",
                "fileEntry": entry,
                "sizeBytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "sessionIndexEntry": {"id": conversation_id, "thread_name": "Guardian Review"},
                "sourceInstance": "guardian",
            }
        ],
    }
    archive_path = tmp_path / "guardian_export.zip"
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
        archive.writestr(entry, raw)

    conv = CodexExportReader(archive_path).parse(conversation_id)
    assert conv.thread_source == "guardian_review"
    assert conv.parent_thread_id == parent_thread_id
    assert conv.agent_path == "guardian/review"
    guardian_msg = next(m for m in conv.messages if m.message_id == "guardian-transcript")
    assert guardian_msg.is_injected is True
    assert guardian_msg.injection_reason == "guardian_transcript"
    assert len([m for m in conv.messages if m.role == "user" and not m.is_injected]) == 0
    assert len(conv.tools) == 1
    assert conv.tools[0].call_id == "guardian-call-1"
    assert conv.event_mirror_counts.get("AgentMessage") == 1
    assert conv.completion_status == "complete"
    assert conv.completion_reason == "final_answer_present"
