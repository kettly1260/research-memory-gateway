from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


PARSER_VERSION = "codex-export-v1.3"
SCHEMA_VERSION = "ai-conversation-v1"


@dataclass(frozen=True)
class ExportSessionRef:
    conversation_id: str
    title: str
    cwd: str
    updated_at: str
    source_entry: str
    source_size_bytes: int
    source_sha256: str
    relative_rollout_path: str = ""
    source_instance: Any = None
    session_index_entry: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AttachmentRef:
    attachment_id: str
    ordinal: int
    message_id: str
    content_type: str
    locator: str
    content_hash: str
    size_bytes: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProvenanceValidation:
    archive_path: str
    archive_sha256: str
    entry_path: str
    entry_size_bytes: int
    entry_expected_sha256: str
    entry_actual_sha256: str
    hash_matched: bool
    manifest_kind: str
    manifest_package_version: int


@dataclass(frozen=True)
class NormalizedMessage:
    ordinal: int
    timestamp: str
    role: str
    text: str
    message_id: str = ""
    turn_id: str = ""
    phase: str = ""
    is_injected: bool = False
    injection_reason: str = ""


@dataclass(frozen=True)
class ToolEvent:
    ordinal: int
    timestamp: str
    event_type: str
    direction: str
    name: str
    call_id: str
    payload_chars: int
    payload_hash: str
    excerpt: str


@dataclass
class NormalizedConversation:
    ref: ExportSessionRef
    archive_path: str
    archive_sha256: str
    created_at: str
    session_meta: dict[str, Any]
    messages: list[NormalizedMessage] = field(default_factory=list)
    tools: list[ToolEvent] = field(default_factory=list)
    attachments: list[AttachmentRef] = field(default_factory=list)
    record_type_counts: dict[str, int] = field(default_factory=dict)
    payload_type_counts: dict[str, int] = field(default_factory=dict)
    special_counts: dict[str, int] = field(default_factory=dict)
    json_errors: list[int] = field(default_factory=list)
    event_mirror_counts: dict[str, int] = field(default_factory=dict)
    provenance: ProvenanceValidation | None = None
    completion_status: str = "incomplete_or_unknown"
    completion_reason: str = ""
    has_turn_aborted: bool = False

    @property
    def parent_thread_id(self) -> str:
        return str(self.session_meta.get("parent_thread_id") or "")

    @property
    def thread_source(self) -> str:
        return str(self.session_meta.get("thread_source") or "")

    @property
    def agent_path(self) -> str:
        return str(self.session_meta.get("agent_path") or "")
