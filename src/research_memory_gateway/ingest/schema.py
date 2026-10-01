"""Wire schema for the conversation ingest protocol (``schema_version = 1``).

The same contract is mirrored in three places, which MUST stay in sync:

1. this module (server-side validation),
2. ``schemas/conversation-ingest-v1.json`` (language-neutral JSON Schema),
3. ``bridge/src/event.rs`` (Rust client structs).

``tests/test_conversation_ingest_schema_contract.py`` fails if they diverge.

Design rules encoded here:

* Every payload carries an explicit ``schema_version``.  Known versions are
  accepted, unknown/future versions are rejected with a stable machine-readable
  code -- never silently reinterpreted.
* ``event_id`` is the end-to-end idempotency key and is produced by the client.
* ``content`` is the only free-text field; everything else is bounded.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

INGEST_SCHEMA_VERSION = 1
SUPPORTED_SCHEMA_VERSIONS: frozenset[int] = frozenset({1})

BATCH_MAX_EVENTS_DEFAULT = 500
MAX_CONTENT_CHARS_DEFAULT = 400_000
MAX_FIELD_CHARS = 512
MAX_METADATA_BYTES = 65_536

#: Stable, machine-readable rejection reasons.  Clients must switch on these
#: strings, never on the human-readable ``message``.
REJECTION_CODES: frozenset[str] = frozenset(
    {
        "unsupported_schema_version",
        "invalid_payload",
        "missing_required_field",
        "invalid_event_type",
        "invalid_role",
        "content_too_large",
        "metadata_too_large",
        "event_id_conflict",
        "session_id_mismatch",
        "ingest_disabled",
        "archive_write_failed",
        "internal_error",
    }
)

#: Canonical normalized event types.  Adapters map provider-native events onto
#: these; the gateway never sees provider vocabulary.
EVENT_TYPES: frozenset[str] = frozenset(
    {
        "session_start",
        "user_prompt",
        "assistant_message",
        "system_message",
        "tool_call",
        "tool_result",
        "turn_end",
        "session_end",
    }
)

#: Roles that become part of the human-visible conversation transcript.
TRANSCRIPT_ROLES: frozenset[str] = frozenset({"user", "assistant"})

ROLES: frozenset[str] = frozenset({"user", "assistant", "system", "tool"})

#: Event types that carry a real message and therefore participate in
#: session-end reconciliation counting.
COUNTED_EVENT_TYPES: frozenset[str] = frozenset({"user_prompt", "assistant_message"})


def _blank_to_empty(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return value


class IngestEvent(BaseModel):
    """One normalized agent event."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    event_id: str = Field(min_length=8, max_length=256)
    schema_version: int = INGEST_SCHEMA_VERSION
    source_system: str = Field(min_length=1, max_length=64)
    source_account_namespace: str = Field(default="", max_length=MAX_FIELD_CHARS)
    session_id: str = Field(default="", max_length=MAX_FIELD_CHARS)
    conversation_id: str = Field(default="", max_length=MAX_FIELD_CHARS)
    thread_id: str = Field(default="", max_length=MAX_FIELD_CHARS)
    branch_id: str = Field(default="", max_length=MAX_FIELD_CHARS)
    message_id: str = Field(default="", max_length=MAX_FIELD_CHARS)
    turn_id: str = Field(default="", max_length=MAX_FIELD_CHARS)
    event_type: str = Field(min_length=1, max_length=64)
    role: str = Field(default="", max_length=32)
    content: str = ""
    timestamp: str = Field(default="", max_length=64)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("event_type")
    @classmethod
    def _check_event_type(cls, value: str) -> str:
        if value not in EVENT_TYPES:
            raise ValueError(f"unsupported event_type: {value}")
        return value

    @field_validator("role", mode="before")
    @classmethod
    def _normalize_role(cls, value: Any) -> Any:
        value = _blank_to_empty(value)
        if value == "":
            return ""
        if not isinstance(value, str) or value not in ROLES:
            raise ValueError(f"unsupported role: {value!r}")
        return value

    @field_validator(
        "source_account_namespace",
        "session_id",
        "conversation_id",
        "thread_id",
        "branch_id",
        "message_id",
        "turn_id",
        mode="before",
    )
    @classmethod
    def _normalize_optional_ids(cls, value: Any) -> Any:
        return _blank_to_empty(value)


class BatchRequest(BaseModel):
    """``POST /api/conversations/events/batch`` request body.

    ``schema_version`` is required (no default) so a client can never silently
    get a version it did not ask for.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: int
    client_id: str = Field(default="", max_length=MAX_FIELD_CHARS)
    events: list[IngestEvent]


class RejectedEvent(BaseModel):
    """One rejected event with a stable machine-readable reason."""

    model_config = ConfigDict(extra="forbid")

    event_id: str = ""
    code: str
    message: str = ""

    @field_validator("code")
    @classmethod
    def _check_code(cls, value: str) -> str:
        if value not in REJECTION_CODES:
            raise ValueError(f"unknown rejection code: {value}")
        return value


class BatchResponse(BaseModel):
    """Response for both single-event and batch ingest.

    ``accepted`` and ``duplicates`` carry event ids only, so a retry after a
    lost ACK is cheap to reconcile.  ``rejected`` must never be treated as a
    successful ACK.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: int = INGEST_SCHEMA_VERSION
    accepted: list[str] = Field(default_factory=list)
    duplicates: list[str] = Field(default_factory=list)
    rejected: list[RejectedEvent] = Field(default_factory=list)
    session: dict[str, Any] | None = None

    @property
    def accepted_count(self) -> int:
        return len(self.accepted)

    @property
    def duplicate_count(self) -> int:
        return len(self.duplicates)

    @property
    def rejected_count(self) -> int:
        return len(self.rejected)


class SessionEndRequest(BaseModel):
    """``POST /api/conversations/session-end`` request body.

    Declares the client's view of a finished session so the gateway can detect
    obviously missing events.  This is the *eventual consistency* half of the
    pipeline; the realtime hooks remain the low-latency path.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: int
    client_id: str = Field(default="", max_length=MAX_FIELD_CHARS)
    source_system: str = Field(min_length=1, max_length=64)
    source_account_namespace: str = Field(default="", max_length=MAX_FIELD_CHARS)
    session_id: str = Field(default="", max_length=MAX_FIELD_CHARS)
    conversation_id: str = Field(default="", max_length=MAX_FIELD_CHARS)
    thread_id: str = Field(default="", max_length=MAX_FIELD_CHARS)
    branch_id: str = Field(default="", max_length=MAX_FIELD_CHARS)
    last_message_id: str = Field(default="", max_length=MAX_FIELD_CHARS)
    observed_message_count: int = Field(default=0, ge=0)
    ended_at: str = Field(default="", max_length=64)


class SnapshotMessage(BaseModel):
    """One message inside a snapshot reconciliation payload."""

    model_config = ConfigDict(extra="forbid")

    message_id: str = Field(default="", max_length=MAX_FIELD_CHARS)
    turn_id: str = Field(default="", max_length=MAX_FIELD_CHARS)
    # Required: silently defaulting a snapshot message's role to "user" would
    # misattribute assistant text.
    role: str = Field(max_length=32)
    content: str = ""
    timestamp: str = Field(default="", max_length=64)
    event_id: str = Field(default="", max_length=256)

    @field_validator("role")
    @classmethod
    def _check_role(cls, value: str) -> str:
        if value not in ROLES:
            raise ValueError(f"unsupported role: {value!r}")
        return value


class SnapshotRequest(BaseModel):
    """``POST /api/conversations/snapshot`` request body.

    Snapshot ingestion is the final-consistency fallback for sessions where
    some realtime hook events were lost.  It uses exactly the same idempotency
    key derivation as the realtime path, so hook-captured and snapshot-captured
    copies of one message collapse into a single archived message.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: int
    client_id: str = Field(default="", max_length=MAX_FIELD_CHARS)
    source_system: str = Field(min_length=1, max_length=64)
    source_account_namespace: str = Field(default="", max_length=MAX_FIELD_CHARS)
    session_id: str = Field(default="", max_length=MAX_FIELD_CHARS)
    conversation_id: str = Field(default="", max_length=MAX_FIELD_CHARS)
    thread_id: str = Field(default="", max_length=MAX_FIELD_CHARS)
    branch_id: str = Field(default="", max_length=MAX_FIELD_CHARS)
    title: str = Field(default="", max_length=MAX_FIELD_CHARS)
    model: str = Field(default="", max_length=MAX_FIELD_CHARS)
    messages: list[SnapshotMessage]
    metadata: dict[str, Any] = Field(default_factory=dict)
    ended: bool = False
