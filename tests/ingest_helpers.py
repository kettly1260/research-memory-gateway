"""Shared helpers for the conversation-ingest test suites.

Not a test module (no ``test_`` prefix) so pytest does not collect it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from research_memory_gateway.backends import build_backend
from research_memory_gateway.config import AppConfig
from research_memory_gateway.service import ResearchMemoryService


def build_config(tmp_path: Path, *, enabled: bool = True) -> AppConfig:
    """An isolated gateway config with the conversation archive switched on."""
    config = AppConfig()
    config.backend.sqlite_path = str(tmp_path / "research-memory.db")
    config.conversation_archive.enabled = enabled
    config.conversation_archive.staging_dir = str(tmp_path / "conversation-staging")
    config.conversation_archive.index_path = str(tmp_path / "conversation-index.sqlite")
    config.conversation_archive.manifest_path = str(tmp_path / "conversation-imports.sqlite")
    config.conversation_ingest.enabled = enabled
    config.conversation_ingest.state_path = str(tmp_path / "conversation-ingest.sqlite")
    config.media_index.enabled = False
    return config


def build_service(tmp_path: Path, *, enabled: bool = True) -> ResearchMemoryService:
    config = build_config(tmp_path, enabled=enabled)
    return ResearchMemoryService(config, build_backend(config))


def event(**overrides: Any) -> dict[str, Any]:
    """A valid ingest event with sensible defaults."""
    payload: dict[str, Any] = {
        "event_id": "rmb1_" + "0" * 40,
        "schema_version": 1,
        "source_system": "codex",
        "source_account_namespace": "",
        "session_id": "session-1",
        "conversation_id": "session-1",
        "thread_id": "",
        "branch_id": "",
        "message_id": "msg-1",
        "turn_id": "",
        "event_type": "user_prompt",
        "role": "user",
        "content": "Fe3+ 储备液浓度为 10 mM，介质为 0.1 M HNO3。",
        "timestamp": "2026-09-30T10:00:00+00:00",
        "metadata": {},
    }
    payload.update(overrides)
    return payload


def codex_notify_payload(*, thread_id: str = "thread-1") -> dict[str, Any]:
    """The verified Codex ``notify`` payload shape."""
    return {
        "type": "agent-turn-complete",
        "thread-id": thread_id,
        "last-assistant-message": "Fe3+ 储备液为 10 mM，介质为 0.1 M HNO3。",
        "input-messages": ["之前 Fe 的硝酸溶液怎么配的？"],
    }


def claude_hook_payload(*, session_id: str = "claude-session-1") -> dict[str, Any]:
    """The verified Claude Code ``UserPromptSubmit`` hook payload shape."""
    return {
        "session_id": session_id,
        "prompt_id": "550e8400-e29b-41d4-a716-446655440000",
        "transcript_path": "/home/user/.claude/projects/slug/session.jsonl",
        "cwd": "/home/user/project",
        "permission_mode": "default",
        "hook_event_name": "UserPromptSubmit",
        "prompt": "之前 Fe 的硝酸溶液怎么配的？",
    }
