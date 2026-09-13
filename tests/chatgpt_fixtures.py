"""Synthetic ChatGPT Data Export ZIP builder for tests (v0.2.6).

Builds export-shaped archives (conversations.json + mapping graphs + optional
user.json / asset entries) without any real account data.  The shapes mirror
the documented ChatGPT Data Export structure:

* conversation object: ``conversation_id``/``title``/``create_time``/
  ``update_time``/``current_node``/``mapping``;
* mapping node: ``id``/``message``/``parent``/``children``;
* message: ``id``/``author.role``/``content.content_type``/``content.parts``/
  ``create_time``/``recipient``/``metadata``.

Node and message ids are derived from content positions (like the provider's
stable server-side ids) so two export versions of the same conversation keep
the same ids for the shared prefix.  ZIP entries carry fixed timestamps so
identical content produces byte-identical archives.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path
from typing import Any

TEST_ACCOUNT_GUID = "11111111-2222-3333-4444-555555555555"

_EPOCH_A = 1757800000.0  # fixed synthetic time (2025-09-13-ish UTC)


def _stable_id(*parts: Any, prefix: str) -> str:
    payload = "\0".join(str(part) for part in parts)
    return f"{prefix}-{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:16]}"


def make_message(
    role: str,
    text: str,
    *,
    create_time: float | None = None,
    model_slug: str | None = None,
    hidden: bool = False,
    recipient: str = "all",
    message_id: str | None = None,
    content: dict[str, Any] | None = None,
    extra_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if content is None:
        content = {"content_type": "text", "parts": [text]}
    metadata: dict[str, Any] = {}
    if model_slug:
        metadata["model_slug"] = model_slug
    if hidden:
        metadata["is_visually_hidden_from_conversation"] = True
    if extra_metadata:
        metadata.update(extra_metadata)
    return {
        "id": message_id or "",  # filled by GraphBuilder when empty
        "author": {"role": role, "name": None, "metadata": {}},
        "create_time": create_time,
        "content": content,
        "recipient": recipient,
        "status": "finished_successfully",
        "weight": 1.0,
        "metadata": metadata,
        "end_turn": None,
    }


class GraphBuilder:
    """Assemble a mapping graph with content-stable ids from ordered turns.

    Node/message ids derive from (conversation, parent, role, text, sibling
    index), so re-building the same prefix yields the same ids -- mirroring
    the provider's stable node ids across successive exports.
    """

    def __init__(self, conversation_id: str) -> None:
        self.conversation_id = conversation_id
        self.root_id = _stable_id(conversation_id, "root", prefix="root")
        self.nodes: dict[str, dict[str, Any]] = {
            self.root_id: {"id": self.root_id, "message": None, "parent": None, "children": []}
        }
        self.tail = self.root_id
        self._clock = _EPOCH_A

    def _add_node(self, parent: str, message: dict[str, Any], *, prefix: str) -> str:
        node_id = _stable_id(
            self.conversation_id,
            parent,
            message["author"]["role"],
            json.dumps(message.get("content"), ensure_ascii=False, sort_keys=True),
            len(self.nodes[parent]["children"]),
            prefix=prefix,
        )
        if not message.get("id"):
            message["id"] = _stable_id(
                self.conversation_id,
                parent,
                message["author"]["role"],
                json.dumps(message.get("content"), ensure_ascii=False, sort_keys=True),
                len(self.nodes[parent]["children"]),
                prefix="msg",
            )
        self._clock += 60.0
        if "create_time" not in message:
            message["create_time"] = self._clock
        self.nodes[node_id] = {
            "id": node_id,
            "message": message,
            "parent": parent,
            "children": [],
        }
        self.nodes[parent]["children"].append(node_id)
        return node_id

    def append(self, message: dict[str, Any]) -> str:
        node_id = self._add_node(self.tail, message, prefix="node")
        self.tail = node_id
        return node_id

    def attach_branch_under(self, parent_node_id: str, message: dict[str, Any]) -> str:
        """Add an alternate child below an existing node (regenerate/edit)."""
        return self._add_node(parent_node_id, message, prefix="alt")

    def extend_under(self, parent_node_id: str, messages: list[dict[str, Any]]) -> str:
        current = parent_node_id
        for message in messages:
            current = self._add_node(current, message, prefix="alt")
        return current

    def to_conversation(
        self,
        *,
        title: str = "Synthetic ChatGPT Conversation",
        create_time: float = _EPOCH_A,
        update_time: float = _EPOCH_A + 3600.0,
        current_node: str | None = None,
        conversation_id_field: str = "conversation_id",
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        conversation: dict[str, Any] = {
            conversation_id_field: self.conversation_id,
            "title": title,
            "create_time": create_time,
            "update_time": update_time,
            "current_node": current_node or self.tail,
            "mapping": self.nodes,
        }
        if extra:
            conversation.update(extra)
        return conversation


def linear_conversation(
    conversation_id: str,
    turns: list[tuple[str, str]],
    *,
    title: str = "Synthetic ChatGPT Conversation",
    create_time: float = _EPOCH_A,
    update_time: float = _EPOCH_A + 3600.0,
    model_slug: str | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    builder = GraphBuilder(conversation_id)
    clock = create_time + 60.0
    for index, (role, text) in enumerate(turns):
        clock += 60.0
        message = make_message(
            role,
            text,
            create_time=clock,
            model_slug=model_slug if role == "assistant" else None,
        )
        builder.append(message)
    return builder.to_conversation(title=title, create_time=create_time, update_time=update_time, **kwargs)


def image_part(pointer: str, *, size_bytes: int = 1000) -> dict[str, Any]:
    return {
        "content_type": "image_asset_pointer",
        "asset_pointer": pointer,
        "size_bytes": size_bytes,
        "width": 1024,
        "height": 1024,
    }


def multimodal_message(
    role: str,
    text: str,
    pointer: str,
    *,
    message_id: str | None = None,
    create_time: float | None = None,
) -> dict[str, Any]:
    return make_message(
        role,
        text,
        message_id=message_id,
        create_time=create_time,
        content={
            "content_type": "multimodal_text",
            "parts": [text, image_part(pointer)],
        },
    )


def _fixed_writestr(archive: zipfile.ZipFile, name: str, data: str) -> None:
    info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    archive.writestr(info, data)


def build_export(
    path: Path,
    conversations: list[dict[str, Any]],
    *,
    account_id: str | None = TEST_ACCOUNT_GUID,
    assets: dict[str, bytes] | None = None,
    reverse_order: bool = False,
    extra_entries: dict[str, str] | None = None,
) -> Path:
    """Write a ChatGPT-export-shaped ZIP.  Deterministic for equal input."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = list(reversed(conversations)) if reverse_order else list(conversations)
    with zipfile.ZipFile(path, "w") as archive:
        _fixed_writestr(
            archive,
            "conversations.json",
            json.dumps(ordered, ensure_ascii=False, indent=1),
        )
        if account_id is not None:
            _fixed_writestr(
                archive,
                "user.json",
                json.dumps(
                    {"id": account_id, "name": "Synthetic User", "email": "redacted@example.com"}
                ),
            )
        for name, payload in (assets or {}).items():
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, payload)
        for name, payload in (extra_entries or {}).items():
            _fixed_writestr(archive, name, payload)
    return path
