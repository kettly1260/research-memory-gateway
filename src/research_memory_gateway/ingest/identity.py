"""Deterministic identity derivation shared by the ingest write path.

Everything here is *deterministic and versioned* so that:

* the same logical event replayed by the bridge yields the same ``event_id``;
* a snapshot message and the hook event that captured the same message collapse
  into one archived message;
* the Rust bridge can reproduce byte-identical ids
  (``bridge/src/normalize.rs`` mirrors this module).

The hash inputs always start with a versioned domain separator, so the hash
spaces of different purposes (event id, content identity, session key) can never
collide.
"""

from __future__ import annotations

import hashlib
import unicodedata

EVENT_ID_DOMAIN = "rmg-bridge-event-v1"
CONTENT_IDENTITY_DOMAIN = "rmg-bridge-content-v1"
SESSION_KEY_DOMAIN = "rmg-bridge-session-v1"
EVENT_ID_PREFIX = "rmb1"

#: Bumped only when the derivation inputs change meaning.  Part of the hash
#: input, so a bump can never silently reinterpret old ids.
DERIVATION_VERSION = 1


def sha256_hex(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def normalize_text(value: str) -> str:
    """Conservative, versioned text normalization.

    Deliberately conservative: Unicode NFC, CRLF/CR -> LF, per-line trailing
    whitespace removal, whole-text edge trim.  No case folding, no whitespace
    collapsing, no punctuation stripping -- scientific numbers, formulas and
    molecule names must never become collapse-equal.
    """
    text = unicodedata.normalize("NFC", value or "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    return "\n".join(lines).strip()


def content_identity(content: str) -> str:
    """Stable content identity: hash of the normalized content only."""
    return sha256_hex("\0".join([CONTENT_IDENTITY_DOMAIN, normalize_text(content)]))


def event_content_hash(event_type: str, role: str, content: str) -> str:
    """Fingerprint of a stored event, used to detect ``event_id`` conflicts.

    Two deliveries of one ``event_id`` must carry the same fingerprint.  A
    mismatch means the client re-used an id for different content, which must be
    surfaced as ``event_id_conflict`` rather than silently overwriting history.
    """
    return sha256_hex(
        "\0".join(
            [
                "rmg-bridge-event-content-v1",
                event_type.strip(),
                role.strip().lower(),
                content_identity(content),
            ]
        )
    )


def derive_event_id(
    *,
    schema_version: int,
    source_system: str,
    source_account_namespace: str,
    conversation_id: str,
    thread_id: str,
    branch_id: str,
    message_id: str = "",
    turn_id: str = "",
    event_type: str,
    content: str,
) -> str:
    """Derive the stable ``event_id`` for an event without a provider id.

    Mirrors the algorithm required by the ingest protocol::

        SHA-256(schema_version, source_system, source_account_namespace,
                conversation_id, thread_id, branch_id, message_id|turn_id,
                event_type, stable-content-identity)

    ``message_id`` is preferred; ``turn_id`` is the documented fallback.  If
    both are empty the content identity alone anchors the event, which is still
    stable across replays but collapses two byte-identical consecutive messages
    in the same turn -- hence adapters should always pass a provider id when
    one exists.
    """
    anchor = (message_id or "").strip() or (turn_id or "").strip()
    payload = "\0".join(
        [
            EVENT_ID_DOMAIN,
            str(DERIVATION_VERSION),
            str(int(schema_version)),
            source_system.strip(),
            source_account_namespace.strip(),
            conversation_id.strip(),
            thread_id.strip(),
            branch_id.strip(),
            anchor,
            event_type.strip(),
            content_identity(content),
        ]
    )
    return f"{EVENT_ID_PREFIX}_{sha256_hex(payload)}"


def session_key(
    *,
    source_system: str,
    source_account_namespace: str,
    session_id: str,
    conversation_id: str,
    thread_id: str,
    branch_id: str,
) -> str:
    """Identity of one conversation thread, independent of any single event."""
    payload = "\0".join(
        [
            SESSION_KEY_DOMAIN,
            source_system.strip().lower(),
            source_account_namespace.strip(),
            session_id.strip(),
            conversation_id.strip(),
            thread_id.strip(),
            branch_id.strip(),
        ]
    )
    return sha256_hex(payload)


def safe_path_segment(value: str, *, fallback: str = "unknown") -> str:
    """Filesystem-safe, non-empty, non-reserved path segment."""
    cleaned = "".join(ch if (ch.isalnum() or ch in "-_.") else "_" for ch in value or "")
    cleaned = cleaned.strip("._")
    if not cleaned:
        return fallback
    # Windows reserved device names would make the note unwritable.
    reserved = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
    if cleaned.upper() in reserved:
        return f"_{cleaned}"
    return cleaned[:120]


def note_relative_path(
    *,
    source_system: str,
    conversation_id: str,
    thread_id: str = "",
    branch_id: str = "",
) -> str:
    """Deterministic archive note path, relative to the staging root.

    ``bridge/<source_system>/<conversation>.md`` for the common case.  Branched
    or threaded conversations get a stable digest suffix so two branches of one
    provider conversation can never overwrite each other.
    """
    system = safe_path_segment(source_system.lower(), fallback="unknown")
    stem = safe_path_segment(conversation_id, fallback="unidentified")
    if thread_id or branch_id:
        suffix = sha256_hex("\0".join([thread_id, branch_id]))[:8]
        stem = f"{stem}-{suffix}"
    return f"bridge/{system}/{stem}.md"
