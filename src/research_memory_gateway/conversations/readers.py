"""Platform-agnostic conversation export reader contract (v0.2.4 W1).

The ingestion pipeline must not hardcode any specific platform reader.  Any
importer (Codex today; ChatGPT/Claude/Gemini later) implements this protocol
and yields normalized conversations plus platform-scoped source identity.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, Sequence, runtime_checkable

from .models import ExportSessionRef, NormalizedConversation


@runtime_checkable
class ConversationExportReader(Protocol):
    """Contract every conversation export reader must satisfy.

    ``source_system``, ``parser_version`` and ``schema_version`` are exposed by
    each reader implementation; the pipeline never assumes a global Codex
    parser version.
    """

    @property
    def source_system(self) -> str: ...

    @property
    def parser_version(self) -> str: ...

    @property
    def schema_version(self) -> str: ...

    @property
    def archive_path(self) -> Path: ...

    @property
    def archive_sha256(self) -> str: ...

    def list_sessions(self) -> Sequence[ExportSessionRef]: ...

    def get_session_ref(self, conversation_id: str) -> ExportSessionRef: ...

    def parse(self, conversation_id: str) -> NormalizedConversation: ...
