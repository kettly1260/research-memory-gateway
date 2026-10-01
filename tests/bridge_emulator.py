"""A faithful Python mirror of the Rust adapters, for the end-to-end test.

This exists because the pytest job has no compiled ``research-memory-bridge``
binary.  It reproduces *only* the mapping rules documented in
``bridge/src/adapters/*.rs`` -- normalization, redaction and the durable spool
are the Rust side's job and are covered by the Rust suite.

The mapping here must stay in step with the Rust code; if it drifts, the
end-to-end test will disagree with ``bridge/tests/adapters.rs``, which asserts
the same fixtures.

Identity derivation is *not* reimplemented: it calls the shared
``research_memory_gateway.ingest.identity.derive_event_id``, which is the same
algorithm pinned for both languages by ``schemas/event-id-contract-v1.json``.
"""

from __future__ import annotations

import json
from typing import Any

from research_memory_gateway.conversations.identity import account_namespace_hash
from research_memory_gateway.ingest.identity import derive_event_id

CODEX_NAMESPACE = account_namespace_hash("codex", "legacy-default-v1")
CLAUDE_NAMESPACE = account_namespace_hash("claude-code", "default-v1")

CODEX_INJECTION_PREFIXES = (
    "<recommended_plugins>",
    "<app-context>",
    "<environment_context>",
    "<skills_instructions>",
    "<turn_aborted>",
    "# AGENTS.md instructions for",
    "Another language model started to solve this problem",
    "The following is the Codex agent history whose request action you are assessing.",
)

TEXT_CONTENT_TYPES = {"input_text", "output_text", "text"}


def _event(
    *,
    source_system: str,
    namespace: str,
    event_type: str,
    role: str,
    content: str,
    conversation_id: str,
    message_id: str = "",
    turn_id: str = "",
    thread_id: str = "",
    branch_id: str = "",
    timestamp: str = "",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "event_id": derive_event_id(
            schema_version=1,
            source_system=source_system,
            source_account_namespace=namespace,
            conversation_id=conversation_id,
            thread_id=thread_id,
            branch_id=branch_id,
            message_id=message_id,
            turn_id=turn_id,
            event_type=event_type,
            content=content,
        ),
        "schema_version": 1,
        "source_system": source_system,
        "source_account_namespace": namespace,
        "session_id": conversation_id,
        "conversation_id": conversation_id,
        "thread_id": thread_id,
        "branch_id": branch_id,
        "message_id": message_id,
        "turn_id": turn_id,
        "event_type": event_type,
        "role": role,
        "content": content,
        "timestamp": timestamp,
        "metadata": metadata or {},
    }


# ---------------------------------------------------------------------------
# Codex
# ---------------------------------------------------------------------------


def codex_notify_events(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """``agent-turn-complete`` -> the turn's messages (no provider ids exist)."""
    if payload.get("type") != "agent-turn-complete":
        return []
    thread = str(payload.get("thread-id") or payload.get("thread_id") or "")
    if not thread:
        return []
    inputs = [str(item) for item in payload.get("input-messages") or []]
    assistant = str(payload.get("last-assistant-message") or "")

    import hashlib

    turn_anchor = hashlib.sha256(
        "\0".join([thread, "\x1f".join(inputs), assistant]).encode("utf-8")
    ).hexdigest()[:16]

    events: list[dict[str, Any]] = []
    for index, text in enumerate(inputs):
        stripped = text.strip()
        if not stripped or stripped.startswith(CODEX_INJECTION_PREFIXES):
            continue
        events.append(
            _event(
                source_system="codex",
                namespace=CODEX_NAMESPACE,
                event_type="user_prompt",
                role="user",
                content=stripped,
                conversation_id=thread,
                thread_id=thread,
                message_id=f"{thread}#input{index}",
                turn_id=f"notify-{turn_anchor}",
                metadata={"adapter": "codex-notify", "id_source": "synthesized"},
            )
        )
    if assistant.strip():
        events.append(
            _event(
                source_system="codex",
                namespace=CODEX_NAMESPACE,
                event_type="assistant_message",
                role="assistant",
                content=assistant.strip(),
                conversation_id=thread,
                thread_id=thread,
                message_id=f"{thread}#assistant#{turn_anchor}",
                turn_id=f"notify-{turn_anchor}",
                metadata={
                    "adapter": "codex-notify",
                    "id_source": "synthesized",
                    "notify_summary": True,
                },
            )
        )
    return events


def codex_rollout_events(text: str) -> list[dict[str, Any]]:
    """Parse a Codex rollout JSONL transcript (the reconciliation path)."""
    events: list[dict[str, Any]] = []
    thread_id = ""
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        outer = record.get("type") or ""
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        timestamp = str(record.get("timestamp") or "")
        if outer == "session_meta":
            thread_id = str(payload.get("id") or payload.get("session_id") or "")
            events.append(
                _event(
                    source_system="codex",
                    namespace=CODEX_NAMESPACE,
                    event_type="session_start",
                    role="",
                    content="",
                    conversation_id=thread_id,
                    thread_id=thread_id,
                    timestamp=timestamp,
                    metadata={"adapter": "codex-rollout"},
                )
            )
            continue
        if outer != "response_item" or payload.get("type") != "message":
            continue
        role = str(payload.get("role") or "")
        if role == "developer":
            continue
        texts = [
            str(block.get("text")).rstrip()
            for block in payload.get("content") or []
            if isinstance(block, dict)
            and block.get("type") in TEXT_CONTENT_TYPES
            and str(block.get("text") or "").strip()
        ]
        if not texts:
            continue
        content = "\n\n".join(texts)
        if role == "user":
            if content.lstrip().startswith(CODEX_INJECTION_PREFIXES):
                continue
            event_type = "user_prompt"
        elif role == "assistant":
            event_type = "assistant_message"
        else:
            event_type = "system_message"
        message_id = str(payload.get("id") or "")
        turn_id = str(
            (payload.get("internal_chat_message_metadata_passthrough") or {}).get("turn_id") or ""
        )
        events.append(
            _event(
                source_system="codex",
                namespace=CODEX_NAMESPACE,
                event_type=event_type,
                role=role,
                content=content,
                conversation_id=thread_id,
                thread_id=thread_id,
                message_id=message_id,
                turn_id=turn_id,
                timestamp=timestamp,
                metadata={
                    "adapter": "codex-rollout",
                    "id_source": "provider" if message_id else "content",
                },
            )
        )
    return events


# ---------------------------------------------------------------------------
# Claude Code
# ---------------------------------------------------------------------------


def claude_hook_events(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Map a Claude Code hook payload (stdin JSON)."""
    hook = str(payload.get("hook_event_name") or "")
    session_id = str(payload.get("session_id") or "")
    if not session_id:
        return []
    if hook == "UserPromptSubmit":
        prompt = str(payload.get("prompt") or "").strip()
        if not prompt:
            return []
        prompt_id = str(payload.get("prompt_id") or "")
        return [
            _event(
                source_system="claude-code",
                namespace=CLAUDE_NAMESPACE,
                event_type="user_prompt",
                role="user",
                content=prompt,
                conversation_id=session_id,
                message_id=prompt_id,
                turn_id=prompt_id,
                metadata={
                    "adapter": "claude-code-hook",
                    "hook_event_name": hook,
                    "id_source": "provider" if prompt_id else "synthesized",
                },
            )
        ]
    if hook == "SessionEnd":
        return [
            _event(
                source_system="claude-code",
                namespace=CLAUDE_NAMESPACE,
                event_type="session_end",
                role="",
                content="",
                conversation_id=session_id,
                metadata={
                    "adapter": "claude-code-hook",
                    "hook_event_name": hook,
                    "reason": str(payload.get("reason") or ""),
                },
            )
        ]
    if hook == "SessionStart":
        return [
            _event(
                source_system="claude-code",
                namespace=CLAUDE_NAMESPACE,
                event_type="session_start",
                role="",
                content="",
                conversation_id=session_id,
                metadata={"adapter": "claude-code-hook", "hook_event_name": hook},
            )
        ]
    if hook in {"Stop", "StopFailure"}:
        # A turn boundary -- deliberately NOT a session end.
        return [
            _event(
                source_system="claude-code",
                namespace=CLAUDE_NAMESPACE,
                event_type="turn_end",
                role="",
                content="",
                conversation_id=session_id,
                turn_id=str(payload.get("prompt_id") or ""),
                metadata={"adapter": "claude-code-hook", "hook_event_name": hook},
            )
        ]
    return []


def claude_transcript_events(text: str) -> list[dict[str, Any]]:
    """Parse a Claude Code project transcript (the reconciliation path)."""
    events: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    session_id = ""
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        record_type = str(record.get("type") or "")
        if record_type not in {"user", "assistant"}:
            continue
        if record.get("isMeta") or record.get("isApiErrorMessage"):
            continue
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        session_id = session_id or str(record.get("sessionId") or "")
        uuid = str(record.get("uuid") or "")
        prompt_id = str(record.get("promptId") or "")
        content = message.get("content")
        texts: list[str] = []
        if isinstance(content, str):
            if content.strip():
                texts.append(content)
        elif isinstance(content, list):
            for block in content:
                if (
                    isinstance(block, dict)
                    and block.get("type") == "text"
                    and str(block.get("text") or "").strip()
                ):
                    texts.append(str(block["text"]))
        if not texts:
            # Tool-only records are telemetry, never conversation transcript.
            continue
        role = str(message.get("role") or record_type)
        event_type = {
            "user": "user_prompt",
            "assistant": "assistant_message",
        }.get(role, "system_message")
        # User prompts anchor on promptId so the hook copy and the transcript
        # copy collapse; assistant messages exist only here, so they use uuid.
        anchor = prompt_id if event_type == "user_prompt" and prompt_id else uuid
        if not anchor or (event_type, anchor) in seen:
            continue
        seen.add((event_type, anchor))
        events.append(
            _event(
                source_system="claude-code",
                namespace=CLAUDE_NAMESPACE,
                event_type=event_type,
                role=role,
                content="\n\n".join(texts),
                conversation_id=session_id,
                message_id=anchor,
                turn_id=prompt_id,
                timestamp=str(record.get("timestamp") or ""),
                metadata={"adapter": "claude-code-transcript", "id_source": "provider"},
            )
        )
    return events
