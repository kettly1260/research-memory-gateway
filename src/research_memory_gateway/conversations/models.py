from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .identity import ConversationSourceIdentity


PARSER_VERSION = "codex-export-v1.3"
SCHEMA_VERSION = "ai-conversation-v1"

# v0.2.4: the global constants above describe the Codex exporter.  Each reader
# exposes its own parser/schema version; PARSER_VERSION stays as a
# compatibility alias for existing callers and tests.
CODEX_PARSER_VERSION = PARSER_VERSION
CODEX_SCHEMA_VERSION = SCHEMA_VERSION

# v0.2.6: ChatGPT Data Export reader versions.
CHATGPT_PARSER_VERSION = "chatgpt-export-v1.0"
CHATGPT_SCHEMA_VERSION = "chatgpt-conversations-v1"

# Import-item key separator.  The provider conversation id is NEVER stored in
# this combined form: import_key is a lookup/pipeline-scoping key only, while
# source identity keeps the bare provider id plus a separate branch id.
IMPORT_KEY_BRANCH_SEPARATOR = "#branch="


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
    # v0.2.4 source identity.  ``conversation_id`` remains the legacy bare
    # provider id for compatibility; new identity lookups must use
    # source_conversation_id + source_system + account namespace instead.
    source_system: str = "codex"
    source_account_namespace_hash: str = ""
    source_thread_id: str = ""
    source_branch_id: str = ""
    # v0.2.6: pipeline-scoped lookup key for one importable item.  Codex keeps
    # this empty so it stays identical to ``conversation_id``; branched sources
    # (ChatGPT) give every branch its own unique import_key while
    # ``conversation_id`` keeps the bare provider conversation id.
    import_key: str = ""

    @property
    def source_conversation_id(self) -> str:
        return self.conversation_id

    @property
    def effective_import_key(self) -> str:
        """The key pipelines use to look this item up via the reader."""
        return self.import_key or self.conversation_id

    def source_identity(self) -> "ConversationSourceIdentity":
        from .identity import ConversationSourceIdentity

        return ConversationSourceIdentity(
            source_system=self.source_system or "codex",
            source_account_namespace_hash=self.source_account_namespace_hash,
            source_conversation_id=self.source_conversation_id,
            source_thread_id=self.source_thread_id,
            source_branch_id=self.source_branch_id,
        )


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
        pid = self.session_meta.get("parent_thread_id")
        if pid:
            return str(pid)
        src = self.session_meta.get("source")
        if isinstance(src, dict):
            spawn = src.get("subagent", {}).get("thread_spawn", {})
            if isinstance(spawn, dict) and spawn.get("parent_thread_id"):
                return str(spawn["parent_thread_id"])
        return str(self.session_meta.get("parent_thread_id") or "")

    @property
    def thread_source(self) -> str:
        return str(self.session_meta.get("thread_source") or "")

    @property
    def agent_path(self) -> str:
        ap = self.session_meta.get("agent_path")
        if ap:
            return str(ap)
        src = self.session_meta.get("source")
        if isinstance(src, dict):
            spawn = src.get("subagent", {}).get("thread_spawn", {})
            if isinstance(spawn, dict) and spawn.get("agent_path"):
                return str(spawn["agent_path"])
        return str(self.session_meta.get("agent_path") or "")

    @property
    def source_system(self) -> str:
        explicit = self.session_meta.get("source_system")
        if explicit:
            return str(explicit)
        return "codex"

    @property
    def source_originator(self) -> str:
        return str(self.session_meta.get("originator") or self.session_meta.get("source_originator") or "")

    @property
    def source_surface(self) -> str:
        explicit = self.session_meta.get("source_surface")
        if explicit:
            return str(explicit)
        src = self.session_meta.get("source")
        if isinstance(src, str):
            return src
        if isinstance(src, dict):
            orig = str(self.session_meta.get("originator") or "").lower()
            if "desktop" in orig:
                return "desktop"
            return "subagent"
        return ""

    @property
    def source_version(self) -> str:
        return str(self.session_meta.get("cli_version") or self.session_meta.get("source_version") or "")

    @property
    def model_provider(self) -> str:
        return str(self.session_meta.get("model_provider") or "")

    @property
    def model_name(self) -> str:
        explicit = self.session_meta.get("model_name") or self.session_meta.get("model")
        if explicit:
            return str(explicit)
        base_inst = self.session_meta.get("base_instructions")
        if isinstance(base_inst, dict):
            prov = base_inst.get("provenance")
            if isinstance(prov, dict) and prov.get("model"):
                return str(prov["model"])
        return ""
