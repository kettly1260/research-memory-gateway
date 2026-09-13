"""ChatGPT Data Export reader (v0.2.6).

Reads the structured conversation data of a ChatGPT Data Export ZIP (primary
source: ``conversations.json``) without ever modifying the archive.  The
reader implements the platform-agnostic ``ConversationExportReader``
contract and isolates every ChatGPT-specific behaviour:

* full-export re-import (the whole ZIP is imported again every time);
* provider conversation + branch two-layer identity: each importable branch
  gets a unique ``import_key`` while ``source_conversation_id`` keeps the
  bare provider conversation id;
* regenerate / edited-message branches are extracted from the mapping graph
  and never silently dropped;
* per-branch snapshot provenance: ``source_sha256`` hashes a canonical
  serialization of the branch snapshot, not the whole ``conversations.json``,
  so one new conversation can never churn every previously imported one;
* account namespace: only a hash of a stable namespace signal is ever
  persisted; raw emails/tokens are neither read into identity nor logged;
* assets are only ever read from inside the ZIP -- missing assets become
  unresolved records and no network fetch is ever attempted.
"""

from __future__ import annotations

import hashlib
import json
import re
import zipfile
from collections import Counter
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterable

from .identity import (
    account_namespace_hash,
    normalize_message_text_v1,
)
from .models import (
    CHATGPT_PARSER_VERSION,
    CHATGPT_SCHEMA_VERSION,
    IMPORT_KEY_BRANCH_SEPARATOR,
    AttachmentRef,
    ExportSessionRef,
    NormalizedConversation,
    NormalizedMessage,
    ToolEvent,
)

CONVERSATIONS_ENTRY = "conversations.json"
USER_ENTRY = "user.json"

BRANCH_KIND_PRIMARY = "primary"
BRANCH_KIND_ALTERNATE = "alternate"

VISIBLE_ROLES = frozenset({"user", "assistant"})
NON_VISIBLE_ROLE_INJECTED = frozenset({"system", "developer"})

# Asset-pointer content types seen in ChatGPT multimodal content parts.
ASSET_POINTER_CONTENT_TYPES = frozenset(
    {
        "image_asset_pointer",
        "audio_asset_pointer",
        "video_container_asset_pointer",
        "real_time_user_audio_video_asset_pointer",
    }
)

DEFAULT_NAMESPACE_LABEL = "chatgpt-default-v1"

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_ASSET_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
BRANCH_ID_HASH_DOMAIN = "chatgpt-branch-v1"


class ChatGPTExportError(ValueError):
    """Fail-closed export error carrying a stable machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def make_import_key(provider_conversation_id: str, branch_id: str) -> str:
    """Branch-scoped reader lookup key.

    This key is pipeline scoping only: source identity always keeps the bare
    provider conversation id plus a separate ``source_branch_id``.
    """
    return f"{provider_conversation_id}{IMPORT_KEY_BRANCH_SEPARATOR}{branch_id}"


def provider_id_of(conversation: dict[str, Any]) -> str:
    """Provider conversation id; exports use 'conversation_id' or 'id'."""
    for key in ("conversation_id", "id"):
        value = conversation.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


# ---------------------------------------------------------------------------
# Timestamps: ChatGPT exports use Unix seconds (int or float) or null.
# ---------------------------------------------------------------------------


def _timestamp_from_epoch(value: Any) -> str:
    """Unix seconds/float -> UTC ISO-8601.  null -> ''; invalid -> ''."""
    if value in (None, ""):
        return ""
    try:
        from datetime import datetime, timezone

        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return ""


def _timestamp_shape(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "invalid"
    if isinstance(value, int):
        return "epoch_seconds_int"
    if isinstance(value, float):
        return "epoch_seconds_float"
    if isinstance(value, str):
        return "iso_string" if re.match(r"\d{4}-\d{2}-\d{2}", value) else "invalid"
    return "invalid"


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _stable_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )


# ---------------------------------------------------------------------------
# Format detection (content-based, never filename-based).
# ---------------------------------------------------------------------------


def detect_export_format(archive_path: str | Path) -> str:
    """Classify an export ZIP by its content: codex | chatgpt | ambiguous | unknown."""
    path = Path(archive_path)
    if not path.exists() or not zipfile.is_zipfile(path):
        return "unknown"
    codex_kind = False
    chatgpt_shape = False
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        if "manifest.json" in names:
            try:
                manifest = json.loads(archive.read("manifest.json"))
                codex_kind = (
                    isinstance(manifest, dict)
                    and manifest.get("kind") == "codex-session-export"
                )
            except (json.JSONDecodeError, OSError, zipfile.BadZipFile):
                codex_kind = False
        if CONVERSATIONS_ENTRY in names:
            chatgpt_shape = _conversations_json_shape_ok(archive)
    if codex_kind and chatgpt_shape:
        return "ambiguous"
    if codex_kind:
        return "codex"
    if chatgpt_shape:
        return "chatgpt"
    return "unknown"


def _conversations_json_shape_ok(archive: zipfile.ZipFile) -> bool:
    try:
        data = json.loads(archive.read(CONVERSATIONS_ENTRY))
    except (json.JSONDecodeError, OSError, zipfile.BadZipFile, MemoryError):
        return False
    if not isinstance(data, list):
        return False
    # Structural probe: a conversation object carries a mapping graph (or at
    # least title+create/update_time when the mapping is empty), never
    # session-JSONL packaging.
    for element in data[:20]:
        if not isinstance(element, dict):
            continue
        if "mapping" in element:
            return True
        if "title" in element and ("create_time" in element or "update_time" in element):
            return True
    return len(data) == 0


# ---------------------------------------------------------------------------
# Account namespace (C6).  Raw labels/GUIDs/emails never reach storage.
# ---------------------------------------------------------------------------


def detect_account_guid(archive_path: str | Path) -> str:
    """Return a stable provider account GUID from the export, or ''.

    Only a strict UUID is accepted, and anything email-shaped is rejected;
    the raw value is used solely to derive a namespace hash and must never be
    persisted, logged or reported.
    """
    path = Path(archive_path)
    if not path.exists() or not zipfile.is_zipfile(path):
        return ""
    with zipfile.ZipFile(path) as archive:
        if USER_ENTRY not in set(archive.namelist()):
            return ""
        try:
            user = json.loads(archive.read(USER_ENTRY))
        except (json.JSONDecodeError, OSError, zipfile.BadZipFile, UnicodeDecodeError):
            return ""
    if not isinstance(user, dict):
        return ""
    value = user.get("id")
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if "@" in value or not _UUID_RE.match(value):
        return ""
    return value


def resolve_account_namespace_hash(
    archive_path: str | Path,
    *,
    namespace_label: str = "",
    use_default: bool = False,
) -> tuple[str, str]:
    """Resolve the account namespace hash following the v0.2.6 priority.

    1. a stable provider account GUID inside the export (validated as a UUID,
       never email/secret shaped) -> hash it;
    2. an explicit stable user-chosen label -> ``account_namespace_hash``;
    3. only with explicit opt-in, the documented default namespace.

    Returns ``(namespace_hash, strategy)``.  Raises ``ChatGPTExportError``
    with code ``ACCOUNT_NAMESPACE_REQUIRED`` when nothing usable is present.
    """
    guid = detect_account_guid(archive_path)
    if guid:
        return account_namespace_hash("chatgpt", guid), "export_account_guid"
    label = (namespace_label or "").strip()
    if label:
        return account_namespace_hash("chatgpt", label), "explicit_label"
    if use_default:
        return account_namespace_hash("chatgpt", DEFAULT_NAMESPACE_LABEL), "default_opt_in"
    raise ChatGPTExportError(
        "ACCOUNT_NAMESPACE_REQUIRED",
        "No stable account namespace: pass --account-namespace <label> or "
        "--use-default-account-namespace explicitly.",
    )


# ---------------------------------------------------------------------------
# Mapping graph validation + branch extraction (C3).
# ---------------------------------------------------------------------------


class ChatGPTGraph:
    """Validated view of one conversation mapping graph.

    Deterministic: parent chains are unique per node, children sets are only
    iterated in sorted order, and JSON object insertion order never matters.
    """

    def __init__(
        self,
        mapping: dict[str, dict[str, Any]],
        *,
        orphan_node_count: int = 0,
        unknown_child_ref_count: int = 0,
        non_dict_node_count: int = 0,
        cycle_node_count: int = 0,
    ) -> None:
        self.mapping = mapping
        self.orphan_node_count = orphan_node_count
        self.unknown_child_ref_count = unknown_child_ref_count
        self.non_dict_node_count = non_dict_node_count
        self.cycle_node_count = cycle_node_count

    @classmethod
    def build(cls, conversation: dict[str, Any], *, fail_closed: bool = True) -> "ChatGPTGraph":
        raw_mapping = conversation.get("mapping")
        if not isinstance(raw_mapping, dict):
            raise ChatGPTExportError(
                "MALFORMED_EXPORT", "conversation.mapping must be an object"
            )
        mapping: dict[str, dict[str, Any]] = {}
        orphans = 0
        unknown_children = 0
        non_dict_nodes = 0
        for node_id, node in raw_mapping.items():
            if not isinstance(node, dict):
                non_dict_nodes += 1
                continue
            key = str(node_id)
            mapping[key] = dict(node)
            mapping[key].setdefault("id", key)
        for node_id in sorted(mapping):
            node = mapping[node_id]
            parent = node.get("parent")
            if parent is None:
                continue
            if str(parent) not in mapping:
                orphans += 1
            children = node.get("children") or []
            if not isinstance(children, list):
                continue
            for child in children:
                if str(child) not in mapping:
                    unknown_children += 1
        graph = cls(
            mapping,
            orphan_node_count=orphans,
            unknown_child_ref_count=unknown_children,
            non_dict_node_count=non_dict_nodes,
        )
        cycles = graph._detect_cycles()
        if cycles and fail_closed:
            raise ChatGPTExportError(
                "GRAPH_CYCLE",
                "conversation mapping contains a parent cycle (refusing to traverse)",
            )
        return graph

    def _detect_cycles(self) -> list[str]:
        """Walk every parent chain; revisiting a node of the same chain is a cycle."""
        cyclic: list[str] = []
        for node_id in sorted(self.mapping):
            seen: set[str] = set()
            current: str | None = node_id
            while current is not None and current not in seen:
                seen.add(current)
                node = self.mapping.get(current)
                parent = node.get("parent") if node else None
                current = str(parent) if parent is not None else None
            if current is not None:
                # The chain stopped because it revisited one of its own nodes.
                cyclic.append(node_id)
        self.cycle_node_count = len({n for n in cyclic})
        return sorted(set(cyclic))

    def parent_of(self, node_id: str) -> str | None:
        node = self.mapping.get(node_id)
        if node is None:
            return None
        parent = node.get("parent")
        if parent is None:
            return None
        parent_id = str(parent)
        return parent_id if parent_id in self.mapping else None

    def path_to_leaf(self, leaf_id: str) -> list[str]:
        """Ordered node ids from a root down to (and including) the leaf."""
        path: list[str] = []
        current: str | None = leaf_id
        seen: set[str] = set()
        while current is not None and current not in seen:
            seen.add(current)
            path.append(current)
            current = self.parent_of(current)
        path.reverse()
        return path

    def children_of(self, node_id: str) -> list[str]:
        node = self.mapping.get(node_id)
        if node is None:
            return []
        children = node.get("children") or []
        if not isinstance(children, list):
            return []
        return sorted(str(child) for child in children if str(child) in self.mapping)

    def leaf_ids(self) -> list[str]:
        """Nodes without resolvable children, in deterministic node-id order."""
        return [
            node_id
            for node_id in sorted(self.mapping)
            if not self.children_of(node_id)
        ]


class BranchPlan:
    """One importable branch of one provider conversation."""

    def __init__(
        self,
        *,
        conversation_id: str,
        branch_id: str,
        branch_kind: str,
        path: list[str],
        snapshot_sha256: str,
        snapshot_size_bytes: int,
    ) -> None:
        self.conversation_id = conversation_id
        self.branch_id = branch_id
        self.branch_kind = branch_kind
        self.path = path
        self.snapshot_sha256 = snapshot_sha256
        self.snapshot_size_bytes = snapshot_size_bytes

    @property
    def import_key(self) -> str:
        return make_import_key(self.conversation_id, self.branch_id)


def branch_id_for_path(
    graph: ChatGPTGraph, path: list[str], visible_sequence: list[tuple[str, str]]
) -> str:
    """Stable branch id following the v0.2.6 priority order.

    1. provider terminal leaf node id;
    2. provider branch/thread id carried by the leaf message (if provided);
    3. hash of the ordered provider node/message ids along the path;
    4. hash of the normalized visible message sequence (last resort).

    Titles, update times and ZIP ordering are never used.
    """
    if not path:
        return "branch-empty"
    leaf_id = path[-1]
    if leaf_id.strip():
        return leaf_id
    leaf_node = graph.mapping.get(leaf_id) or {}
    message = leaf_node.get("message")
    metadata = message.get("metadata") if isinstance(message, dict) else None
    if isinstance(metadata, dict):
        for key in ("branch_id", "thread_id"):
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                return f"provider-{value.strip()}"
    parts: list[str] = []
    for node in path:
        node_dict = graph.mapping.get(node) or {}
        message = node_dict.get("message")
        message_id = (
            message.get("id") if isinstance(message, dict) and isinstance(message.get("id"), str) else ""
        )
        parts.append(message_id.strip() or node)
    if any(part for part in parts):
        return "path-" + _sha256_text(BRANCH_ID_HASH_DOMAIN + "\0" + "\n".join(parts))[:32]
    sequence = "\n".join(
        f"{role}\0{normalize_message_text_v1(text)}" for role, text in visible_sequence
    )
    return "content-" + _sha256_text(BRANCH_ID_HASH_DOMAIN + "\0" + sequence)[:32]


def _message_visible_text(message: dict[str, Any] | None) -> str:
    """Human-visible text of one mapping message ('' when not visible)."""
    if not isinstance(message, dict):
        return ""
    author = message.get("author") or {}
    role = str(author.get("role") or "") if isinstance(author, dict) else ""
    if role not in VISIBLE_ROLES:
        return ""
    recipient = str(message.get("recipient") or "")
    if recipient not in ("", "all"):
        # assistant->tool traffic is never part of the human-visible transcript
        return ""
    metadata = message.get("metadata") or {}
    if isinstance(metadata, dict) and metadata.get("is_visually_hidden_from_conversation"):
        return ""
    text, _, _ = _extract_content(message)
    return text


def _visible_sequence(graph: ChatGPTGraph, path: list[str]) -> list[tuple[str, str]]:
    sequence: list[tuple[str, str]] = []
    for node_id in path:
        node = graph.mapping.get(node_id) or {}
        message = node.get("message")
        text = _message_visible_text(message if isinstance(message, dict) else None)
        if not text.strip():
            continue
        author = (message or {}).get("author") or {}
        role = str(author.get("role") or "")
        sequence.append((role, text))
    return sequence


def plan_branches(
    conversation: dict[str, Any], *, fail_closed: bool = True
) -> tuple[ChatGPTGraph, list[BranchPlan]]:
    """Validate the graph and derive deterministic importable branch plans.

    The primary branch (from ``current_node``) always imports; every other
    terminal leaf with human-visible user/assistant content becomes an
    alternate branch.  System-only or metadata-only alternate leaves never
    create empty branch records.  Branch ordering is deterministic:
    primary first, then alternates ordered by terminal leaf node id.
    """
    graph = ChatGPTGraph.build(conversation, fail_closed=fail_closed)
    provider_id = provider_id_of(conversation)
    current_node = conversation.get("current_node")
    current_node_id = str(current_node) if isinstance(current_node, str) and current_node else ""

    candidates: list[tuple[str, list[str], list[tuple[str, str]]]] = []
    if current_node_id and current_node_id in graph.mapping:
        primary_path = graph.path_to_leaf(current_node_id)
        candidates.append((current_node_id, primary_path, _visible_sequence(graph, primary_path)))
    for leaf_id in graph.leaf_ids():
        if leaf_id == current_node_id:
            continue
        path = graph.path_to_leaf(leaf_id)
        visible = _visible_sequence(graph, path)
        if not visible:
            # system-only / metadata-only leaf: never an empty branch record
            continue
        candidates.append((leaf_id, path, visible))

    plans: list[BranchPlan] = []
    seen_branch_ids: set[str] = set()
    primary_seen = False
    for index, (leaf_id, path, visible) in enumerate(candidates):
        branch_id = branch_id_for_path(graph, path, visible)
        if branch_id in seen_branch_ids:
            continue
        seen_branch_ids.add(branch_id)
        kind = BRANCH_KIND_PRIMARY if (index == 0 and current_node_id) else BRANCH_KIND_ALTERNATE
        primary_seen = primary_seen or kind == BRANCH_KIND_PRIMARY
        snapshot = canonical_branch_snapshot(conversation, branch_id, path, graph)
        plans.append(
            BranchPlan(
                conversation_id=provider_id,
                branch_id=branch_id,
                branch_kind=kind,
                path=path,
                snapshot_sha256=_sha256_bytes(snapshot),
                snapshot_size_bytes=len(snapshot),
            )
        )
    if plans and not primary_seen:
        # No usable current_node: the first deterministic candidate acts as
        # the primary so exactly one branch is always marked primary.
        plans[0].branch_kind = BRANCH_KIND_PRIMARY
    return graph, plans


# ---------------------------------------------------------------------------
# Content extraction (C4.4).
# ---------------------------------------------------------------------------


def _extract_content(
    message: dict[str, Any],
) -> tuple[str, list[AttachmentRef], dict[str, int]]:
    """Extract stable text plus attachment refs from one mapping message.

    Returns ``(text, attachment_refs, counters)``.  Non-text parts never
    disappear silently: asset pointers become ``[attachment: ...]``
    placeholders plus AttachmentRef records; unknown shapes become counted
    ``[unsupported-content: ...]`` placeholders.  Python dict reprs are never
    written into the transcript.
    """
    counters: dict[str, int] = {"unsupported_content": 0, "attachment_parts": 0, "empty_parts": 0}
    refs: list[AttachmentRef] = []
    content = message.get("content")
    if not isinstance(content, dict):
        if content in (None, ""):
            counters["empty_parts"] += 1
            return "", refs, counters
        counters["unsupported_content"] += 1
        return f"[unsupported-content: {type(content).__name__}]", refs, counters

    texts: list[str] = []
    parts = content.get("parts")
    if isinstance(parts, list):
        for part in parts:
            if isinstance(part, str):
                if part:
                    texts.append(part)
                else:
                    counters["empty_parts"] += 1
                continue
            if not isinstance(part, dict):
                counters["unsupported_content"] += 1
                texts.append(f"[unsupported-content: {type(part).__name__}]")
                continue
            part_type = str(part.get("content_type") or "unknown")
            if part_type in ASSET_POINTER_CONTENT_TYPES:
                ref = _attachment_ref_from_pointer(message, part, part_type)
                if ref is not None:
                    refs.append(ref)
                    counters["attachment_parts"] += 1
                    texts.append(f"[attachment: {ref.locator}]")
                else:
                    counters["unsupported_content"] += 1
                    texts.append(f"[unsupported-content: {part_type}]")
                continue
            text_value = part.get("text") or part.get("audio_transcription")
            if isinstance(text_value, str) and text_value.strip():
                texts.append(text_value)
            elif isinstance(text_value, str):
                counters["empty_parts"] += 1
            else:
                counters["unsupported_content"] += 1
                texts.append(f"[unsupported-content: {part_type}]")
        return "\n".join(texts).strip(), refs, counters

    text_value = content.get("text")
    if isinstance(text_value, str):
        return text_value.strip(), refs, counters
    title_value = content.get("title")
    if isinstance(title_value, str) and title_value.strip():
        return title_value.strip(), refs, counters
    counters["unsupported_content"] += 1
    return f"[unsupported-content: {content.get('content_type') or 'unknown'}]", refs, counters


def _attachment_ref_from_pointer(
    message: dict[str, Any], part: dict[str, Any], part_type: str
) -> AttachmentRef | None:
    locator_value = part.get("asset_pointer") or part.get("pointer")
    if not isinstance(locator_value, str) or not locator_value.strip():
        return None
    locator = locator_value.strip()
    content_hash = _sha256_text(_stable_json(locator))
    metadata = {
        key: value
        for key, value in part.items()
        if key in {"width", "height"} and isinstance(value, (int, float))
    }
    size_value = part.get("size_bytes")
    return AttachmentRef(
        attachment_id=f"att_{content_hash[:16]}",
        ordinal=0,  # finalized with the message ordinal by the caller
        message_id=str(message.get("id") or ""),
        content_type=part_type,
        locator=locator,
        content_hash=content_hash,
        size_bytes=int(size_value) if isinstance(size_value, (int, float)) and not isinstance(size_value, bool) else None,
        metadata=metadata,
    )


# ---------------------------------------------------------------------------
# Per-branch snapshot provenance (C5).
# ---------------------------------------------------------------------------

_SNAPSHOT_FIELDS = ("id", "author", "content", "create_time", "end_turn", "recipient", "weight")


def _canonical_message(message: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(message, dict):
        return None
    canonical: dict[str, Any] = {}
    for field in _SNAPSHOT_FIELDS:
        if field in message:
            canonical[field] = message[field]
    metadata = message.get("metadata")
    if isinstance(metadata, dict) and "is_visually_hidden_from_conversation" in metadata:
        canonical["hidden"] = bool(metadata.get("is_visually_hidden_from_conversation"))
    return canonical


def canonical_branch_snapshot(
    conversation: dict[str, Any],
    branch_id: str,
    path: list[str],
    graph: ChatGPTGraph,
) -> bytes:
    """Deterministic per-branch snapshot serialization.

    Includes only the nodes on this branch's path plus the provider
    conversation id and branch id.  Object keys are sorted, packaging-only
    ordering is ignored, and parser/schema versions are deliberately absent
    so a parser upgrade can never forge a source payload change.

    Sibling branch structure (the ``children`` lists) is deliberately
    excluded: adding a regenerated sibling must never churn the existing
    branch's snapshot hash.
    """
    nodes = []
    for node_id in path:
        node = graph.mapping.get(node_id) or {}
        nodes.append(
            {
                "node_id": str(node.get("id") or node_id),
                "parent": str(node.get("parent")) if node.get("parent") is not None else "",
                "message": _canonical_message(node.get("message")),
            }
        )
    snapshot = {
        "provider_conversation_id": provider_id_of(conversation),
        "branch_id": branch_id,
        "nodes": nodes,
    }
    return _stable_json(snapshot).encode("utf-8")


def branch_source_entry(provider_conversation_id: str, branch_id: str) -> str:
    return f"{CONVERSATIONS_ENTRY}#conversation={provider_conversation_id}#branch={branch_id}"


# ---------------------------------------------------------------------------
# The reader.
# ---------------------------------------------------------------------------


class ChatGPTExportReader:
    """Reader for one ChatGPT Data Export ZIP.  The archive is read-only."""

    source_system = "chatgpt"
    parser_version = CHATGPT_PARSER_VERSION
    schema_version = CHATGPT_SCHEMA_VERSION

    def __init__(self, archive_path: str | Path, *, account_namespace_hash: str) -> None:
        self.archive_path = Path(archive_path)
        if not self.archive_path.exists():
            raise FileNotFoundError(f"Archive not found: {self.archive_path}")
        if not zipfile.is_zipfile(self.archive_path):
            raise ChatGPTExportError("UNSUPPORTED_EXPORT_FORMAT", "not a ZIP archive")
        self._namespace_hash = (account_namespace_hash or "").strip()
        if not self._namespace_hash:
            raise ChatGPTExportError(
                "ACCOUNT_NAMESPACE_REQUIRED",
                "ChatGPTExportReader requires an account namespace hash",
            )
        self._archive_sha256: str | None = None
        self._conversations: list[dict[str, Any]] | None = None
        self._refs_by_import_key: dict[str, ExportSessionRef] | None = None
        self._refs_in_order: list[ExportSessionRef] | None = None
        self.schema_warnings: list[str] = []

    # -- archive plumbing ------------------------------------------------------

    @property
    def archive_sha256(self) -> str:
        if self._archive_sha256 is None:
            digest = hashlib.sha256()
            with self.archive_path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            self._archive_sha256 = digest.hexdigest()
        return self._archive_sha256

    def _open_archive(self) -> zipfile.ZipFile:
        # Read-only by construction: mode "r" only, anywhere in this module.
        return zipfile.ZipFile(self.archive_path, "r")

    def _load_conversations(self) -> list[dict[str, Any]]:
        if self._conversations is not None:
            return self._conversations
        with self._open_archive() as archive:
            if CONVERSATIONS_ENTRY not in set(archive.namelist()):
                raise ChatGPTExportError(
                    "UNSUPPORTED_EXPORT_FORMAT",
                    "ZIP has no structured conversations.json (HTML-only exports are not supported)",
                )
            raw = archive.read(CONVERSATIONS_ENTRY)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ChatGPTExportError(
                "MALFORMED_EXPORT", f"conversations.json is not valid JSON: {exc}"
            ) from exc
        if not isinstance(data, list):
            raise ChatGPTExportError(
                "MALFORMED_EXPORT",
                f"conversations.json top level must be a list, got {type(data).__name__}",
            )
        conversations: list[dict[str, Any]] = []
        for index, element in enumerate(data):
            if isinstance(element, dict):
                conversations.append(element)
            else:
                self.schema_warnings.append(f"non_object_conversation:{index}")
        self._conversations = conversations
        return conversations

    # -- sessions ----------------------------------------------------------------

    @staticmethod
    def _conversation_sort_key(conversation: dict[str, Any]) -> tuple[float, str]:
        create_time = conversation.get("create_time")
        if isinstance(create_time, (int, float)) and not isinstance(create_time, bool):
            return (float(create_time), provider_id_of(conversation))
        return (0.0, provider_id_of(conversation))

    @staticmethod
    def _conversation_title(conversation: dict[str, Any]) -> str:
        title = conversation.get("title")
        if isinstance(title, str) and title.strip():
            return title
        return provider_id_of(conversation) or "Untitled"

    def _build_refs(self) -> dict[str, ExportSessionRef]:
        if self._refs_by_import_key is not None:
            return self._refs_by_import_key
        refs: dict[str, ExportSessionRef] = {}
        for conversation in sorted(self._load_conversations(), key=self._conversation_sort_key):
            provider_id = provider_id_of(conversation)
            if not provider_id:
                self.schema_warnings.append("conversation_missing_id")
                continue
            if not isinstance(conversation.get("mapping"), dict):
                # Individually malformed conversations are reported and
                # skipped; the rest of the export still imports.
                self.schema_warnings.append(f"malformed_mapping:{provider_id}")
                continue
            try:
                _, plans = plan_branches(conversation, fail_closed=True)
            except ChatGPTExportError as exc:
                # Cycles and other graph-level corruption fail closed loudly.
                raise ChatGPTExportError(
                    exc.code, f"conversation {provider_id}: {exc.args[0]}"
                ) from exc
            updated_at = _timestamp_from_epoch(conversation.get("update_time"))
            for plan in plans:
                refs[plan.import_key] = ExportSessionRef(
                    conversation_id=provider_id,
                    title=self._conversation_title(conversation),
                    cwd="",
                    updated_at=updated_at,
                    source_entry=branch_source_entry(provider_id, plan.branch_id),
                    source_size_bytes=plan.snapshot_size_bytes,
                    source_sha256=plan.snapshot_sha256,
                    source_system=self.source_system,
                    source_account_namespace_hash=self._namespace_hash,
                    source_branch_id=plan.branch_id,
                    import_key=plan.import_key,
                )
        self._refs_by_import_key = refs
        self._refs_in_order = list(refs.values())
        return refs

    def list_sessions(self) -> list[ExportSessionRef]:
        """All importable refs in deterministic order.

        Conversations are ordered by (create_time, provider id); within a
        conversation the primary branch comes first, then alternates by
        terminal leaf node id.  JSON object order never influences this.
        """
        if self._refs_in_order is None:
            self._build_refs()
        return list(self._refs_in_order or [])

    def get_session_ref(self, import_key: str) -> ExportSessionRef:
        refs = self._build_refs()
        try:
            return refs[import_key]
        except KeyError:
            raise KeyError(f"Unknown import_key: {import_key}") from None

    # -- parsing -------------------------------------------------------------------

    def parse(self, import_key: str) -> NormalizedConversation:
        refs = self._build_refs()
        ref = refs.get(import_key)
        if ref is None:
            raise KeyError(f"Unknown import_key: {import_key}")
        conversation = self._conversation_by_id(ref.conversation_id)
        graph, plans = plan_branches(conversation, fail_closed=True)
        plan = next((p for p in plans if p.branch_id == ref.source_branch_id), None)
        if plan is None:
            raise ChatGPTExportError(
                "MALFORMED_EXPORT",
                f"branch {ref.source_branch_id} vanished from conversation {ref.conversation_id}",
            )
        return self._parse_branch(conversation, ref, graph, plan)

    def _conversation_by_id(self, provider_id: str) -> dict[str, Any]:
        for conversation in self._load_conversations():
            if provider_id_of(conversation) == provider_id:
                return conversation
        raise KeyError(f"Unknown conversation: {provider_id}")

    def _parse_branch(
        self,
        conversation: dict[str, Any],
        ref: ExportSessionRef,
        graph: ChatGPTGraph,
        plan: BranchPlan,
    ) -> NormalizedConversation:
        messages: list[NormalizedMessage] = []
        tools: list[ToolEvent] = []
        attachments: list[AttachmentRef] = []
        model_histogram: Counter[str] = Counter()
        model_order: list[str] = []
        content_type_histogram: Counter[str] = Counter()
        special: Counter[str] = Counter()
        hidden_count = 0
        unsupported_count = 0
        cache: dict[str, tuple[str, int] | None] = {}

        ordinal = 0
        for node_id in plan.path:
            node = graph.mapping.get(node_id) or {}
            message = node.get("message")
            if not isinstance(message, dict):
                special["nodes_without_message"] += 1
                continue
            author = message.get("author") or {}
            role = str(author.get("role") or "unknown") if isinstance(author, dict) else "unknown"
            metadata = message.get("metadata") or {}
            is_hidden = isinstance(metadata, dict) and bool(
                metadata.get("is_visually_hidden_from_conversation")
            )
            timestamp = _timestamp_from_epoch(message.get("create_time"))
            if message.get("create_time") not in (None, "") and not timestamp:
                self.schema_warnings.append(f"invalid_timestamp:{node_id}")

            recipient = str(message.get("recipient") or "")
            content = message.get("content") or {}
            content_type = str(content.get("content_type") or "") if isinstance(content, dict) else ""
            if content_type:
                content_type_histogram[content_type] += 1
            model_slug = (
                str(metadata.get("model_slug") or "")
                if isinstance(metadata, dict) and metadata.get("model_slug")
                else ""
            )
            if model_slug:
                model_histogram[model_slug] += 1
                if model_slug not in model_order:
                    model_order.append(model_slug)

            if role == "tool" or (role == "assistant" and recipient not in ("", "all")):
                # Tool traffic stays a tool event; it never poses as a
                # visible user/assistant transcript turn.
                text, refs, counters = _extract_content(message)
                special["tool_traffic_messages"] += 1
                tools.append(
                    ToolEvent(
                        ordinal=ordinal,
                        timestamp=timestamp,
                        event_type="tool_call" if role == "assistant" else "tool_message",
                        direction="call" if role == "assistant" else "output",
                        name=str(author.get("name") or "") if isinstance(author, dict) else "",
                        call_id=str(message.get("id") or ""),
                        payload_chars=len(text),
                        payload_hash=_sha256_text(text),
                        excerpt=text[:500],
                    )
                )
                attachments.extend(
                    self._resolve_ref_content(replace(r, ordinal=ordinal), cache)
                    for r in refs
                )
                special["unsupported_content"] += counters.get("unsupported_content", 0)
                unsupported_count += counters.get("unsupported_content", 0)
                if is_hidden:
                    hidden_count += 1
                ordinal += 1
                continue

            text, refs, counters = _extract_content(message)
            unsupported = counters.get("unsupported_content", 0)
            unsupported_count += unsupported
            special["unsupported_content"] += unsupported
            attachments.extend(
                self._resolve_ref_content(replace(r, ordinal=ordinal), cache)
                for r in refs
            )

            if role in NON_VISIBLE_ROLE_INJECTED:
                special[f"{role}_role_messages"] += 1
                messages.append(
                    NormalizedMessage(
                        ordinal=ordinal,
                        timestamp=timestamp,
                        role=role,
                        text=text,
                        message_id=str(message.get("id") or ""),
                        is_injected=True,
                        injection_reason=f"{role}_role",
                    )
                )
            elif role in VISIBLE_ROLES:
                if is_hidden:
                    hidden_count += 1
                    special["hidden_visible_role_messages"] += 1
                messages.append(
                    NormalizedMessage(
                        ordinal=ordinal,
                        timestamp=timestamp,
                        role=role,
                        text=text,
                        message_id=str(message.get("id") or ""),
                        is_injected=is_hidden,
                        injection_reason="chatgpt_hidden" if is_hidden else "",
                    )
                )
            else:
                special[f"role:{role}"] += 1
                messages.append(
                    NormalizedMessage(
                        ordinal=ordinal,
                        timestamp=timestamp,
                        role=role,
                        text=text,
                        message_id=str(message.get("id") or ""),
                        is_injected=True,
                        injection_reason="non_visible_role",
                    )
                )
            ordinal += 1

        model_name = ""
        if model_histogram:
            best_count = max(model_histogram.values())
            candidates = [slug for slug, count in model_histogram.items() if count == best_count]
            # dominant slug; tie-break by latest occurrence for determinism
            model_name = next(slug for slug in reversed(model_order) if slug in candidates)

        session_meta: dict[str, Any] = {
            "source_system": self.source_system,
            "source_originator": "ChatGPT Data Export",
            "source_surface": "chatgpt_export",
            "source_version": "",
            "provider_conversation_id": ref.conversation_id,
            "branch_id": plan.branch_id,
            "branch_kind": plan.branch_kind,
            "primary_current_node": str(conversation.get("current_node") or ""),
            "model_histogram": dict(sorted(model_histogram.items())),
            "content_type_histogram": dict(sorted(content_type_histogram.items())),
            "hidden_message_count": hidden_count,
            "unsupported_content_count": unsupported_count,
        }
        if model_histogram:
            # Export evidence (model_slug) supports attributing the provider.
            session_meta["model_provider"] = "openai"
            session_meta["model_name"] = model_name
        assistants = [m for m in messages if m.role == "assistant" and not m.is_injected]
        completion_reason = "no_final_answer" if assistants else "no_assistant_message"

        return NormalizedConversation(
            ref=ref,
            archive_path=str(self.archive_path.resolve()),
            archive_sha256=self.archive_sha256,
            created_at=_timestamp_from_epoch(conversation.get("create_time")),
            session_meta=session_meta,
            messages=messages,
            tools=tools,
            attachments=_dedupe_attachments(attachments),
            record_type_counts={"mapping_node": len(plan.path)},
            payload_type_counts=dict(sorted(content_type_histogram.items())),
            special_counts=dict(sorted(special.items())),
            completion_status="incomplete_or_unknown",
            completion_reason=completion_reason,
        )

    # -- ZIP-internal assets (C7) -------------------------------------------------

    def _asset_entry_index(self) -> dict[str, str]:
        """basename (and extension-less stem) -> zip entry name for assets.

        Structured document entries (the JSON/HTML exports themselves) are
        excluded.  Entry names are validated against path traversal before
        they are ever opened.  Asset pointers reference extension-less file
        ids, so both ``file-ABC.png`` and ``file-ABC`` resolve to the same
        entry (exact basename wins over stem).
        """
        exact: dict[str, str] = {}
        stems: dict[str, str] = {}
        excluded = {CONVERSATIONS_ENTRY, USER_ENTRY, "chat.html", "manifest.json"}
        with self._open_archive() as archive:
            for name in archive.namelist():
                if not _safe_zip_entry_name(name):
                    continue
                base = name.replace("\\", "/").rsplit("/", 1)[-1]
                if not base or base in excluded:
                    continue
                if base in exact:
                    # deterministic: keep the lexicographically smallest entry
                    exact[base] = min(exact[base], name)
                else:
                    exact[base] = name
                stem = base.rsplit(".", 1)[0] if "." in base else base
                if stem and stem not in stems:
                    stems[stem] = exact[base]
                elif stem and stem in stems and exact[base] < stems[stem]:
                    stems[stem] = exact[base]
        merged = dict(stems)
        merged.update(exact)
        return merged

    def resolve_asset(self, locator: str) -> tuple[bytes | None, str | None]:
        """Resolve one asset pointer strictly from inside the ZIP.

        Returns ``(content, zip_entry_name)`` or ``(None, None)`` when the
        asset is not present in the export.  Never touches the network.
        Entry names with traversal segments are rejected outright.
        """
        asset_id = _asset_id_from_locator(locator)
        if not asset_id:
            return None, None
        index = self._asset_entry_index()
        entry = index.get(asset_id)
        if entry is None or not _safe_zip_entry_name(entry):
            return None, None
        with self._open_archive() as archive:
            return archive.read(entry), entry

    def _resolve_ref_content(
        self, ref: AttachmentRef, cache: dict[str, tuple[str, int] | None]
    ) -> AttachmentRef:
        """Upgrade an attachment ref with the resolved asset's content hash.

        When the pointer resolves inside the ZIP, ``content_hash`` becomes
        the SHA-256 of the actual asset bytes (and ``size_bytes`` the true
        byte length).  Unresolvable pointers keep the pointer-derived hash
        and stay unresolved records -- the network is never consulted.
        """
        if ref.locator in cache:
            resolved = cache[ref.locator]
        else:
            content, _entry = self.resolve_asset(ref.locator)
            resolved = (
                (_sha256_bytes(content), len(content)) if content is not None else None
            )
            cache[ref.locator] = resolved
        if resolved is None:
            return ref
        return replace(ref, content_hash=resolved[0], size_bytes=resolved[1])

    # -- schema audit (C1) ----------------------------------------------------------

    def schema_audit(self) -> dict[str, Any]:
        """Privacy-safe structural audit of the export.

        Emits inventory/shape/count/hash information only -- never
        transcripts, account identifiers or raw attachment payloads.
        """
        conversations = self._load_conversations()
        with self._open_archive() as archive:
            file_inventory = sorted(
                ({"name": info.filename, "size_bytes": info.file_size} for info in archive.infolist()),
                key=lambda item: item["name"],
            )

        conversation_fields: Counter[str] = Counter()
        node_fields: Counter[str] = Counter()
        message_fields: Counter[str] = Counter()
        content_types: Counter[str] = Counter()
        roles: Counter[str] = Counter()
        metadata_keys: Counter[str] = Counter()
        locator_schemes: Counter[str] = Counter()
        timestamp_shapes: Counter[str] = Counter()
        current_node_present = 0
        empty_mapping = 0
        graph_orphans = 0
        graph_unknown_children = 0
        graph_cycles = 0
        non_dict_nodes = 0
        leaf_total = 0
        alternate_total = 0
        malformed_objects = 0
        branch_counts: Counter[str] = Counter()

        for conversation in conversations:
            for field in conversation:
                conversation_fields[field] += 1
            if not isinstance(conversation.get("mapping"), dict):
                malformed_objects += 1
                continue
            if not conversation.get("mapping"):
                empty_mapping += 1
            if conversation.get("current_node"):
                current_node_present += 1
            try:
                graph, plans = plan_branches(conversation, fail_closed=False)
            except ChatGPTExportError:
                malformed_objects += 1
                continue
            graph_orphans += graph.orphan_node_count
            graph_unknown_children += graph.unknown_child_ref_count
            graph_cycles += 1 if graph.cycle_node_count else 0
            non_dict_nodes += graph.non_dict_node_count
            leaf_total += len(graph.leaf_ids())
            for plan in plans:
                branch_counts[plan.branch_kind] += 1
            alternate_total += sum(1 for plan in plans if plan.branch_kind == BRANCH_KIND_ALTERNATE)
            for node_id in sorted(graph.mapping):
                node = graph.mapping[node_id]
                for field in node:
                    node_fields[field] += 1
                message = node.get("message")
                if not isinstance(message, dict):
                    continue
                for field in message:
                    message_fields[field] += 1
                author = message.get("author") or {}
                if isinstance(author, dict):
                    roles[str(author.get("role") or "<missing>")] += 1
                message_metadata = message.get("metadata")
                if isinstance(message_metadata, dict):
                    for key in message_metadata:
                        metadata_keys[str(key)] += 1
                content = message.get("content")
                if isinstance(content, dict):
                    content_types[str(content.get("content_type") or "<missing>")] += 1
                timestamp_shapes[_timestamp_shape(message.get("create_time"))] += 1
                _count_locator_schemes(message, locator_schemes)

        return {
            "archive_path_resolved": str(self.archive_path.resolve()),
            "archive_sha256": self.archive_sha256,
            "archive_file_inventory": file_inventory,
            "conversations_json_present": True,
            "conversations_top_level_type": "list",
            "conversation_count": len(conversations),
            "conversation_object_fields": dict(sorted(conversation_fields.items())),
            "mapping_node_fields": dict(sorted(node_fields.items())),
            "message_fields": dict(sorted(message_fields.items())),
            "content_type_inventory": dict(sorted(content_types.items())),
            "role_inventory": dict(sorted(roles.items())),
            "metadata_key_inventory": dict(sorted(metadata_keys.items())),
            "asset_locator_schemes": dict(sorted(locator_schemes.items())),
            "timestamp_shapes": dict(sorted(timestamp_shapes.items())),
            "branch_shape": {
                "current_node_present": current_node_present,
                "empty_mapping_conversations": empty_mapping,
                "leaf_nodes_total": leaf_total,
                "primary_branches": branch_counts.get(BRANCH_KIND_PRIMARY, 0),
                "alternate_branches": alternate_total,
            },
            "graph_validation": {
                "orphan_nodes": graph_orphans,
                "unknown_child_refs": graph_unknown_children,
                "non_dict_nodes": non_dict_nodes,
                "cycle_conversations": graph_cycles,
            },
            "malformed": {
                "malformed_conversation_objects": malformed_objects,
                "non_object_conversations": len(self.schema_warnings),
            },
        }


def _count_locator_schemes(message: dict[str, Any], schemes: Counter[str]) -> None:
    content = message.get("content")
    if not isinstance(content, dict):
        return
    parts = content.get("parts")
    if not isinstance(parts, list):
        return
    for part in parts:
        if isinstance(part, dict):
            pointer = part.get("asset_pointer") or part.get("pointer")
            if isinstance(pointer, str) and pointer.strip():
                match = _ASSET_SCHEME_RE.match(pointer.strip())
                schemes[match.group(0)[:-3] if match else "bare"] += 1


def _asset_id_from_locator(locator: str) -> str:
    """``file-service://file-ABC`` -> ``file-ABC``; ``sediment://file_x`` -> ``file_x``."""
    value = (locator or "").strip()
    if not value:
        return ""
    if "://" in value:
        value = value.split("://", 1)[1]
    value = value.split("?", 1)[0].split("#", 1)[0]
    return value.replace("\\", "/").rsplit("/", 1)[-1].strip()


def _safe_zip_entry_name(name: str) -> bool:
    """Reject path-traversal / absolute / drive-letter ZIP entry names."""
    if not name:
        return False
    normalized = name.replace("\\", "/")
    if re.match(r"^[A-Za-z]:", normalized):
        return False
    if normalized.startswith("/"):
        return False
    return ".." not in normalized.split("/")


def _dedupe_attachments(items: list[AttachmentRef]) -> list[AttachmentRef]:
    """Collapse repeated references to the same asset pointer.

    Identity is the pointer-derived ``attachment_id`` (never the content
    hash): two distinct pointers with identical bytes stay two inventory
    records that merely share a content hash.
    """
    deduped: list[AttachmentRef] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        key = (item.content_type, item.attachment_id)
        if key not in seen:
            seen.add(key)
            deduped.append(item)
    return deduped


__all__ = [
    "BRANCH_KIND_ALTERNATE",
    "BRANCH_KIND_PRIMARY",
    "ASSET_POINTER_CONTENT_TYPES",
    "ChatGPTExportError",
    "ChatGPTExportReader",
    "ChatGPTGraph",
    "BranchPlan",
    "canonical_branch_snapshot",
    "detect_account_guid",
    "detect_export_format",
    "make_import_key",
    "plan_branches",
    "resolve_account_namespace_hash",
]
