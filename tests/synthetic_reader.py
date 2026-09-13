"""Test-only synthetic conversation export readers for multi-source scenarios.

These fakes implement the platform-agnostic ``ConversationExportReader``
contract without any real provider format.  They exist to prove that the
identity layer separates sources by (system, account namespace, provider id)
and that identical raw ids across platforms never collide.
"""

from __future__ import annotations

import hashlib
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from research_memory_gateway.conversations.identity import account_namespace_hash
from research_memory_gateway.conversations.models import (
    ExportSessionRef,
    NormalizedConversation,
    NormalizedMessage,
)


@dataclass
class SyntheticSession:
    conversation_id: str
    title: str = "Synthetic Conversation"
    messages: list[tuple[str, str]] = field(default_factory=list)
    updated_at: str = "2026-09-13T00:00:00Z"
    created_at: str = "2026-09-13T00:00:00Z"
    thread_id: str = ""
    branch_id: str = ""
    session_meta: dict[str, Any] = field(default_factory=dict)


class SyntheticExportReader:
    """Minimal in-ZIP export reader for a synthetic source system."""

    parser_version = "synthetic-export-v1"
    schema_version = "ai-conversation-v1"

    def __init__(
        self,
        archive_path: str | Path,
        *,
        source_system: str,
        namespace_label: str,
        sessions: list[SyntheticSession] | None = None,
    ) -> None:
        self.archive_path = Path(archive_path)
        self.source_system = source_system
        self._namespace_hash = account_namespace_hash(source_system, namespace_label)
        self._sessions = {s.conversation_id: s for s in (sessions or [])}
        self._archive_sha256: str | None = None
        self._write_archive()

    def _write_archive(self) -> None:
        self.archive_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "kind": f"{self.source_system}-session-export",
            "sessions": [
                {
                    "sessionId": s.conversation_id,
                    "title": s.title,
                    "messages": [list(m) for m in s.messages],
                }
                for s in self._sessions.values()
            ],
        }
        import json

        with zipfile.ZipFile(self.archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps(payload, ensure_ascii=False))

    @property
    def archive_sha256(self) -> str:
        if self._archive_sha256 is None:
            digest = hashlib.sha256()
            with self.archive_path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            self._archive_sha256 = digest.hexdigest()
        return self._archive_sha256

    def list_sessions(self) -> list[ExportSessionRef]:
        return [self._ref(s) for s in self._sessions.values()]

    def get_session_ref(self, conversation_id: str) -> ExportSessionRef:
        session = self._sessions.get(conversation_id)
        if session is None:
            raise KeyError(f"Unknown conversation_id: {conversation_id}")
        return self._ref(session)

    def parse(self, conversation_id: str) -> NormalizedConversation:
        session = self._sessions.get(conversation_id)
        if session is None:
            raise KeyError(f"Unknown conversation_id: {conversation_id}")
        ref = self._ref(session)
        messages = [
            NormalizedMessage(
                ordinal=i,
                timestamp=session.created_at,
                role=role,
                text=text,
                message_id=f"{conversation_id}-{i}",
            )
            for i, (role, text) in enumerate(session.messages)
        ]
        return NormalizedConversation(
            ref=ref,
            archive_path=str(self.archive_path.resolve()),
            archive_sha256=self.archive_sha256,
            created_at=session.created_at,
            session_meta=dict(session.session_meta),
            messages=messages,
        )

    def _ref(self, session: SyntheticSession) -> ExportSessionRef:
        entry = f"files/{session.conversation_id}.json"
        raw = repr(session.messages).encode("utf-8")
        return ExportSessionRef(
            conversation_id=session.conversation_id,
            title=session.title,
            cwd="",
            updated_at=session.updated_at,
            source_entry=entry,
            source_size_bytes=len(raw),
            source_sha256=hashlib.sha256(raw).hexdigest(),
            source_system=self.source_system,
            source_account_namespace_hash=self._namespace_hash,
            source_thread_id=session.thread_id,
            source_branch_id=session.branch_id,
        )
