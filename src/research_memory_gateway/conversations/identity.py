"""Platform-agnostic conversation source identity and deterministic fingerprints.

v0.2.4 introduces a source identity layer that is independent of any single
export platform.  Two concepts are strictly separated:

Source Conversation Record
    The provenance record of one conversation as exported by one platform
    (system, account namespace, provider conversation/thread/branch id).
Canonical Conversation
    The Gateway-internal logical group that links source records that have
    been confirmed to belong to the same logical conversation.

Fingerprints describe *content*; they never replace source identity.
"""

from __future__ import annotations

import hashlib
import unicodedata
import uuid
from dataclasses import dataclass
from typing import Any, Sequence

# ---------------------------------------------------------------------------
# Versioned domain separators.  Every hash input starts with a versioned
# domain string so hash spaces can never collide across purposes or algorithm
# generations.  Python's runtime hash() is never used.
# ---------------------------------------------------------------------------

FINGERPRINT_VERSION = 1

# Migrated-but-unhydrated marker: legacy records carry no per-message
# fingerprints yet.  Version 0 must never be treated as a valid fingerprint
# generation -- it explicitly means "fingerprints unknown".
FINGERPRINT_VERSION_UNHYDRATED = 0

SOURCE_KEY_DOMAIN = "rmg-source-v1"
SOURCE_KEY_PREFIX = "srcv1"
ACCOUNT_NAMESPACE_DOMAIN = "rmg-account-ns-v1"
MESSAGE_FINGERPRINT_DOMAIN = "rmg-message-fingerprint-v1"
TRANSCRIPT_FINGERPRINT_DOMAIN = "rmg-normalized-transcript-v1"
ORDERED_MESSAGES_DOMAIN = "rmg-ordered-messages-v1"
MESSAGE_SET_DOMAIN = "rmg-message-set-v1"
TOOL_EVENT_SET_DOMAIN = "rmg-tool-event-set-v1"
ATTACHMENT_INVENTORY_DOMAIN = "rmg-attachment-inventory-v1"

# Fixed namespace for deterministic canonical conversation UUIDv5 values.
CANONICAL_NAMESPACE_V1 = uuid.uuid5(
    uuid.NAMESPACE_URL, "urn:research-memory-gateway:canonical-conversation:v1"
)

# Stable legacy namespace for existing Codex imports (322-session library).
LEGACY_CODEX_SOURCE_SYSTEM = "codex"
LEGACY_CODEX_NAMESPACE_LABEL = "legacy-default-v1"

VISIBLE_ROLES = frozenset({"user", "assistant"})


def _sha256_hex(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def normalize_component(value: Any) -> str:
    """Normalize one identity component to a stable string (never None)."""
    if value is None:
        return ""
    return str(value).strip()


def account_namespace_hash(source_system: str, namespace_label: str) -> str:
    """Deterministic hash of an account namespace label, scoped per system.

    Raw account emails, tokens or secrets must never be passed here: callers
    pass either a stable provider account GUID (normalized) or a stable
    user-chosen namespace label.  Only the hash is ever persisted.
    """
    system = normalize_component(source_system).lower()
    label = normalize_component(namespace_label)
    payload = "\0".join([ACCOUNT_NAMESPACE_DOMAIN, system, label])
    return _sha256_hex(payload)


def codex_legacy_namespace_hash() -> str:
    return account_namespace_hash(LEGACY_CODEX_SOURCE_SYSTEM, LEGACY_CODEX_NAMESPACE_LABEL)


@dataclass(frozen=True)
class ConversationSourceIdentity:
    """Identity of one conversation as exported by one platform/account."""

    source_system: str
    source_account_namespace_hash: str
    source_conversation_id: str
    source_thread_id: str = ""
    source_branch_id: str = ""

    @property
    def source_key(self) -> str:
        """Deterministic, namespace-safe source key: ``srcv1_<sha256>``."""
        payload = "\0".join(
            [
                SOURCE_KEY_DOMAIN,
                normalize_component(self.source_system).lower(),
                normalize_component(self.source_account_namespace_hash),
                normalize_component(self.source_conversation_id),
                normalize_component(self.source_thread_id),
                normalize_component(self.source_branch_id),
            ]
        )
        return f"{SOURCE_KEY_PREFIX}_{_sha256_hex(payload)}"


def canonical_conversation_id_for(source_key: str) -> str:
    """Deterministic canonical id for the first source record of a conversation."""
    return str(uuid.uuid5(CANONICAL_NAMESPACE_V1, normalize_component(source_key)))


# ---------------------------------------------------------------------------
# Versioned message normalization + fingerprints (algorithm version 1).
# Deliberately conservative normalization: Unicode NFC, CRLF -> LF, per-line
# trailing whitespace removal and whole-text edge trimming.  No case folding,
# no whitespace collapsing, no punctuation removal: code, formulas and numeric
# content must not be collapse-equal.
# ---------------------------------------------------------------------------


def normalize_message_text_v1(text: str) -> str:
    value = unicodedata.normalize("NFC", text or "")
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in value.split("\n")]
    return "\n".join(lines).strip()


def normalize_message_text(text: str, *, version: int = FINGERPRINT_VERSION) -> str:
    if version != 1:
        raise ValueError(f"Unsupported message normalization version: {version}")
    return normalize_message_text_v1(text)


def message_fingerprint_v1(role: str, text: str) -> str:
    payload = "\0".join(
        [
            MESSAGE_FINGERPRINT_DOMAIN,
            normalize_component(role).lower(),
            normalize_message_text_v1(text),
        ]
    )
    return _sha256_hex(payload)


def message_fingerprint(role: str, text: str, *, version: int = FINGERPRINT_VERSION) -> str:
    if version != 1:
        raise ValueError(f"Unsupported message fingerprint version: {version}")
    return message_fingerprint_v1(role, text)


def visible_messages(conversation: Any) -> list[Any]:
    """Messages that form the human-visible transcript (mirrors note content).

    Injected/system messages are excluded so exporter packaging noise cannot
    manufacture false divergences, and so fingerprint sequences align with the
    visible content stored in the managed Markdown notes.
    """
    return [
        message
        for message in (conversation.messages or [])
        if message.role in VISIBLE_ROLES and not message.is_injected
    ]


def _tool_event_set_payload(tools: Sequence[Any]) -> str:
    parts = [
        "\x1f".join(
            [
                normalize_component(tool.event_type),
                normalize_component(tool.direction),
                normalize_component(tool.name),
                normalize_component(tool.call_id),
                normalize_component(tool.payload_hash),
            ]
        )
        for tool in tools
    ]
    return "\n".join(sorted(parts))


def _attachment_inventory_payload(attachments: Sequence[Any]) -> str:
    parts = [
        "\x1f".join(
            [
                normalize_component(att.content_type),
                normalize_component(att.content_hash),
            ]
        )
        for att in attachments
    ]
    return "\n".join(sorted(parts))


@dataclass(frozen=True)
class TranscriptFingerprints:
    """Content fingerprints for one normalized conversation snapshot."""

    fingerprint_version: int
    ordered_message_hash: str
    message_set_hash: str
    normalized_transcript_sha256: str
    message_count: int
    tool_event_set_hash: str
    attachment_inventory_hash: str
    message_fingerprints: tuple[str, ...]
    message_roles: tuple[str, ...]


def compute_transcript_fingerprints(conversation: Any) -> TranscriptFingerprints:
    messages = visible_messages(conversation)
    fingerprints = tuple(message_fingerprint_v1(m.role, m.text) for m in messages)
    roles = tuple(normalize_component(m.role).lower() for m in messages)

    ordered_payload = "\n".join(f"{index}\0{fp}" for index, fp in enumerate(fingerprints))
    ordered_hash = _sha256_hex("\0".join([ORDERED_MESSAGES_DOMAIN, ordered_payload]))

    set_payload = "\n".join(sorted(fingerprints))
    set_hash = _sha256_hex("\0".join([MESSAGE_SET_DOMAIN, set_payload]))

    transcript_parts = [
        "\n".join([f"### role: {role}", text, "<<<message-end>>>"])
        for role, text in (
            (normalize_component(m.role).lower(), normalize_message_text_v1(m.text))
            for m in messages
        )
    ]
    transcript_hash = _sha256_hex(
        "\0".join([TRANSCRIPT_FINGERPRINT_DOMAIN, "\n".join(transcript_parts)])
    )

    tool_hash = _sha256_hex(
        "\0".join([TOOL_EVENT_SET_DOMAIN, _tool_event_set_payload(conversation.tools or [])])
    )
    attachment_hash = _sha256_hex(
        "\0".join(
            [ATTACHMENT_INVENTORY_DOMAIN, _attachment_inventory_payload(conversation.attachments or [])]
        )
    )

    return TranscriptFingerprints(
        fingerprint_version=FINGERPRINT_VERSION,
        ordered_message_hash=ordered_hash,
        message_set_hash=set_hash,
        normalized_transcript_sha256=transcript_hash,
        message_count=len(messages),
        tool_event_set_hash=tool_hash,
        attachment_inventory_hash=attachment_hash,
        message_fingerprints=fingerprints,
        message_roles=roles,
    )


# ---------------------------------------------------------------------------
# Sequence relations used by the import decision state machine.
# ---------------------------------------------------------------------------


def sequence_relation(
    left: Sequence[str], right: Sequence[str]
) -> str:
    """Classify two fingerprint sequences.

    Returns one of:
      equal                 -- sequences are identical
      left_strict_prefix    -- left is a proper prefix of right (right continues left)
      right_strict_prefix   -- right is a proper prefix of left (left is newer)
      diverged              -- they disagree before either sequence ends
    """
    if list(left) == list(right):
        return "equal"
    common = min(len(left), len(right))
    if list(left[:common]) == list(right[:common]):
        return "left_strict_prefix" if len(left) < len(right) else "right_strict_prefix"
    return "diverged"


def message_overlap_ratio(left: Sequence[str], right: Sequence[str]) -> float:
    left_set = set(left)
    right_set = set(right)
    if not left_set and not right_set:
        return 0.0
    union = left_set | right_set
    if not union:
        return 0.0
    return len(left_set & right_set) / len(union)
