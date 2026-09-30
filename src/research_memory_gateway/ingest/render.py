"""Render the archived Markdown note for bridge-ingested conversations.

The note is a *materialized view* of the ingest ledger.  Rendering is split into
self-contained blocks so materialization can be incremental (append-only) after
retention pruning has removed the older ledger rows, without ever truncating
content that is already archived.

The note layout is deliberately close to ``ObsidianConversationWriter`` output so
the existing FTS indexer, retrieval service and MCP tools need no special cases::

    ---
    <machine frontmatter, incl. source_key / canonical_conversation_id>
    ---

    # <title>

    <!-- rmb:begin -->
    <!-- source ordinal=0 message_id=... turn_id=... -->
    ## 用户 (2026-09-30T...)

    text

    <!-- rmb:end -->

    ## 人工补充与关联笔记 (Related & Notes)

    <user-owned region, never overwritten>

Only ``user`` / ``assistant`` content is treated as the conversation transcript.
Tool events are archived as bounded telemetry blocks so they cannot drown normal
conversation recall.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import yaml

from ..conversations.identity import (
    ConversationSourceIdentity,
    canonical_conversation_id_for,
)
from .identity import safe_path_segment

RMB_BEGIN = "<!-- rmb:begin -->"
RMB_END = "<!-- rmb:end -->"
RMB_EVENT_PREFIX = "<!-- rmb:ev "
RMB_EVENT_SUFFIX = " -->"
MANUAL_SECTION = (
    "## 人工补充与关联笔记 (Related & Notes)\n\n"
    "<!-- 在此添加自定义笔记、研究决策或 Obsidian 反向链接 [[...]]；重新物化时会自动保留 -->\n"
)

PARSER_VERSION = "bridge-hook-v1"
NOTE_SCHEMA_VERSION = "conversation-ingest-v1"
SOURCE_ORIGINATOR = "research-memory-bridge"

MAX_TOOL_EXCERPT_CHARS = 240
MAX_TOOL_BLOCK_CHARS = 400
DATA_URI_RE = re.compile(r"data:[^;,\s]+(?:;[^,\s]+)*;base64,[A-Za-z0-9+/=\r\n]+", re.IGNORECASE)

ROLE_LABELS = {"user": "用户", "assistant": "助手", "system": "系统"}

TRANSCRIPT_EVENT_TYPES = {"user_prompt": "user", "assistant_message": "assistant", "system_message": "system"}
TOOL_EVENT_TYPES = {"tool_call": "调用", "tool_result": "结果"}

#: Event types that become a block in the archived note.  Lifecycle markers
#: (`session_start`, `turn_end`, `session_end`) stay in the ledger as telemetry
#: and never become note sections, so they cannot dilute conversation recall.
RENDERABLE_EVENT_TYPES = frozenset(
    {"user_prompt", "assistant_message", "system_message", "tool_call", "tool_result"}
)

#: Metadata key distinguishing a real provider id from an id the adapter had to
#: synthesise.  Only provider-identified blocks may supersede another block.
ID_SOURCE_KEY = "id_source"
ID_SOURCE_PROVIDER = "provider"


def is_renderable(event: dict[str, Any]) -> bool:
    return str(event.get("event_type")) in RENDERABLE_EVENT_TYPES


def id_source(event: dict[str, Any]) -> str:
    metadata = event.get("metadata")
    if isinstance(metadata, dict):
        value = str(metadata.get(ID_SOURCE_KEY) or "")
        if value:
            return value
    return ID_SOURCE_PROVIDER if str(event.get("message_id") or "").strip() else "content"

_SOURCE_ANCHOR_SAFE_RE = re.compile(r"<!--\s*source\s+", re.IGNORECASE)
_RMB_EVENT_RE = re.compile(r"<!--\s*rmb:ev\s+(?P<event_id>\S+)\s*-->")


def event_marker(event_id: str) -> str:
    """Per-block marker making note materialization idempotent.

    Kept inside the block body (after the message text) so the chunker attaches
    it to that message's section instead of emitting a stray one-line chunk.
    """
    return f"{RMB_EVENT_PREFIX}{event_id}{RMB_EVENT_SUFFIX}"


def scan_event_ids(note_text: str) -> set[str]:
    """All event ids already present in a note's managed region."""
    _, managed, _ = parse_note(note_text)
    return {match.group("event_id") for match in _RMB_EVENT_RE.finditer(managed)}


def _date_part(value: str) -> str:
    return value[:10] if value and re.match(r"\d{4}-\d{2}-\d{2}", value) else ""


def _timeline_part(value: str) -> list[str]:
    date = _date_part(value)
    return [date[:7]] if len(date) >= 7 else []


def _sanitize_body(text: str) -> str:
    """Neutralize anything that could break the note's managed-region markers.

    A message body containing the literal markers or a forged ``source`` anchor
    could otherwise confuse the chunker or a later append.
    """
    if not text:
        return ""
    cleaned = text.replace(RMB_BEGIN, "[rmb:begin]").replace(RMB_END, "[rmb:end]")
    cleaned = cleaned.replace(RMB_EVENT_PREFIX, "<!-- rmb:ev&#32;")
    cleaned = _SOURCE_ANCHOR_SAFE_RE.sub("<!-- source&#32;", cleaned)
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    return cleaned.strip()


def _short_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"…(+{len(text) - limit} chars)"


def note_title(conversation_id: str, *, title: str = "", metadata: dict[str, Any] | None = None) -> str:
    for candidate in (title, str((metadata or {}).get("title") or "")):
        if candidate and candidate.strip():
            return candidate.strip()
    return f"Agent Conversation {conversation_id[:8] or 'unknown'}"


def build_frontmatter(
    *,
    source_system: str,
    source_account_namespace: str,
    conversation_id: str,
    thread_id: str = "",
    branch_id: str = "",
    client_id: str = "",
    title: str = "",
    model: str = "",
    created_at: str = "",
    updated_at: str = "",
    event_count: int = 0,
    message_count: int = 0,
    completion_status: str = "incomplete_or_unknown",
    metadata: dict[str, Any] | None = None,
    schema_version: int = 1,
) -> dict[str, Any]:
    metadata = metadata or {}
    identity = ConversationSourceIdentity(
        source_system=source_system,
        source_account_namespace_hash=source_account_namespace,
        source_conversation_id=conversation_id,
        source_thread_id=thread_id,
        source_branch_id=branch_id,
    )
    projects = metadata.get("projects")
    if not isinstance(projects, list):
        projects = []
    return {
        "type": "ai-conversation",
        "source": source_system,
        "source_system": source_system,
        "source_originator": SOURCE_ORIGINATOR,
        "source_surface": "lifecycle-hook",
        "source_version": str(metadata.get("agent_version") or "") or None,
        "model_provider": str(metadata.get("model_provider") or "") or None,
        "model_name": model or None,
        "conversation_id": conversation_id,
        "canonical_conversation_id": canonical_conversation_id_for(identity.source_key),
        "source_key": identity.source_key,
        "source_conversation_id": conversation_id,
        "source_thread_id": thread_id or None,
        "source_branch_id": branch_id or None,
        "source_account_namespace": source_account_namespace,
        "ingest_client_id": client_id,
        "created": _date_part(created_at),
        "updated": _date_part(updated_at or created_at),
        "projects": [str(item) for item in projects],
        "timeline": _timeline_part(created_at),
        "topics": [],
        "parser_version": PARSER_VERSION,
        "schema_version": NOTE_SCHEMA_VERSION,
        "ingest_schema_version": int(schema_version),
        "ingest_event_count": int(event_count),
        "ingest_message_count": int(message_count),
        "ingest_source": SOURCE_ORIGINATOR,
        "completion_status": completion_status,
        "title": title,
    }


def render_message_block(event: dict[str, Any], ordinal: int) -> str:
    """One transcript message as a self-contained Markdown block."""
    role = str(event.get("role") or "")
    event_type = str(event.get("event_type") or "")
    role = role or TRANSCRIPT_EVENT_TYPES.get(event_type, "user")
    label = ROLE_LABELS.get(role, role or "消息")
    timestamp = str(event.get("timestamp") or "")
    message_id = str(event.get("message_id") or "")
    turn_id = str(event.get("turn_id") or "")
    content = _sanitize_body(str(event.get("content") or ""))
    lines = [
        f"<!-- source ordinal={ordinal} message_id={message_id} turn_id={turn_id} -->",
        f"## {label} ({timestamp})",
        "",
        content,
        "",
        event_marker(str(event.get("event_id") or "")),
        "",
    ]
    return "\n".join(lines)


def render_tool_block(event: dict[str, Any], ordinal: int) -> str:
    """Bounded telemetry block for one tool call/result.

    Deliberately compact: tool payloads are archived as hash + short excerpt so
    that a chatty tool cannot dominate conversation recall.
    """
    event_type = str(event.get("event_type") or "")
    direction = TOOL_EVENT_TYPES.get(event_type, event_type)
    metadata = event.get("metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}
    name = str(metadata.get("tool_name") or metadata.get("name") or "")
    call_id = str(metadata.get("call_id") or event.get("message_id") or "")
    raw = str(event.get("content") or "")
    excerpt = DATA_URI_RE.sub(
        lambda match: f"<embedded-data:{len(match.group(0))} chars>", raw
    )
    excerpt = truncate(excerpt, MAX_TOOL_EXCERPT_CHARS).replace("\n", " ")
    block_body = truncate(_sanitize_body(raw), MAX_TOOL_BLOCK_CHARS).replace("\n", " ")
    lines = [
        f"<!-- source ordinal={ordinal} message_id={call_id} turn_id={event.get('turn_id') or ''} -->",
        f"## 工具活动 · {direction} `{name or 'unknown'}`",
        "",
        f"- call_id: `{call_id}`",
        f"- 载荷字符数: {len(raw)}",
        f"- 载荷 SHA-256: `{_short_hash(raw)}`",
        f"- 摘要: `{excerpt}`",
        "",
        block_body,
        "",
        event_marker(str(event.get("event_id") or "")),
        "",
    ]
    return "\n".join(lines)


def render_block(event: dict[str, Any], ordinal: int) -> str:
    if str(event.get("event_type")) in TOOL_EVENT_TYPES:
        return render_tool_block(event, ordinal)
    return render_message_block(event, ordinal)


def render_blocks(events: Sequence[dict[str, Any]]) -> str:
    blocks = [render_block(event, index) for index, event in enumerate(events)]
    return "\n".join(blocks).rstrip() + "\n"


def render_note(
    *,
    frontmatter: dict[str, Any],
    title: str,
    blocks: str,
    manual: str = "",
) -> str:
    fm_text = yaml.safe_dump(
        {key: value for key, value in frontmatter.items() if value is not None},
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    ).strip()
    body = "\n".join(
        [
            "---",
            fm_text,
            "---",
            "",
            f"# {title}",
            "",
            RMB_BEGIN,
            "",
            blocks.rstrip(),
            "",
            RMB_END,
            "",
        ]
    )
    manual_text = manual.strip() or MANUAL_SECTION.strip()
    return f"{body}\n{manual_text}\n"


def parse_note(text: str) -> tuple[dict[str, Any], str, str]:
    """Split a bridge note into (frontmatter, managed region, manual region)."""
    frontmatter: dict[str, Any] = {}
    body = text
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            try:
                loaded = yaml.safe_load(parts[1]) or {}
                if isinstance(loaded, dict):
                    frontmatter = loaded
            except Exception:
                frontmatter = {}
            body = parts[2]
    managed = ""
    manual = ""
    if RMB_END in body:
        before, manual = body.split(RMB_END, 1)
        manual = manual.lstrip("\r\n")
        if RMB_BEGIN in before:
            _, managed = before.split(RMB_BEGIN, 1)
        else:
            managed = before
    elif RMB_BEGIN in body:
        _, managed = body.split(RMB_BEGIN, 1)
    else:
        managed = ""
        manual = body
    return frontmatter, managed.strip(), manual.strip()


def append_to_note(
    *,
    existing_text: str,
    new_blocks: str,
    frontmatter_updates: dict[str, Any],
) -> str:
    """Append blocks to an existing note without touching its manual region."""
    frontmatter, managed, manual = parse_note(existing_text)
    merged = {**frontmatter, **frontmatter_updates}
    combined_blocks = "\n".join(part for part in (managed, new_blocks.strip()) if part).rstrip()
    title = str(merged.get("title") or "").strip()
    if not title:
        title_match = re.search(r"^#\s+(.*)$", existing_text, flags=re.MULTILINE)
        title = title_match.group(1).strip() if title_match else "Agent Conversation"
    return render_note(frontmatter=merged, title=title, blocks=combined_blocks, manual=manual)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def note_path_for(
    staging_dir: Any,
    *,
    source_system: str,
    conversation_id: str,
    thread_id: str = "",
    branch_id: str = "",
) -> Any:
    from pathlib import Path

    from .identity import note_relative_path

    relative = note_relative_path(
        source_system=source_system,
        conversation_id=conversation_id,
        thread_id=thread_id,
        branch_id=branch_id,
    )
    root = Path(staging_dir).resolve()
    target = (root / relative).resolve()
    target.relative_to(root)  # fail closed if the relative path ever escapes
    return target


def safe_segment(value: str) -> str:
    return safe_path_segment(value)
