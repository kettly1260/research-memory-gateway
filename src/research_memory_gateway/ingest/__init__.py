"""Non-MCP HTTP conversation ingest surface.

This package implements the *write* path of the Research Memory Gateway:

    Agent -> research-memory-bridge -> HTTP ingest API -> gateway archive + FTS

It is deliberately separate from the MCP surface, which stays the *read* path
(``conversation_search`` / ``conversation_recall`` / ``conversation_read``).
"""

from .schema import (
    BATCH_MAX_EVENTS_DEFAULT,
    INGEST_SCHEMA_VERSION,
    REJECTION_CODES,
    BatchRequest,
    BatchResponse,
    IngestEvent,
    RejectedEvent,
    SessionEndRequest,
    SnapshotRequest,
)

__all__ = [
    "BATCH_MAX_EVENTS_DEFAULT",
    "INGEST_SCHEMA_VERSION",
    "REJECTION_CODES",
    "BatchRequest",
    "BatchResponse",
    "IngestEvent",
    "RejectedEvent",
    "SessionEndRequest",
    "SnapshotRequest",
]
