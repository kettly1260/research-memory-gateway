"""Conversation archive ingestion and Obsidian staging support."""

from .codex_export import CodexExportReader
from .manifest import ImportManifest
from .pipeline import ConversationIngestionPipeline
from .vault_writer import ObsidianConversationWriter
from .attachments import AttachmentInventory, AttachmentInventoryRecord
from .chunking import ConversationChunk, HeadingChunker
from .identity import (
    ConversationSourceIdentity,
    TranscriptFingerprints,
    compute_transcript_fingerprints,
)
from .identity_store import ConversationIdentityStore
from .index import ConversationIndexDatabase, SearchResult
from .retrieval import ConversationRetrievalService

__all__ = [
    "AttachmentInventory",
    "AttachmentInventoryRecord",
    "ConversationChunk",
    "ConversationIndexDatabase",
    "ConversationIdentityStore",
    "CodexExportReader",
    "ConversationIngestionPipeline",
    "ConversationRetrievalService",
    "ConversationSourceIdentity",
    "HeadingChunker",
    "ImportManifest",
    "ObsidianConversationWriter",
    "SearchResult",
    "TranscriptFingerprints",
    "compute_transcript_fingerprints",
]
