"""Agent surface upload and ingestion control tools.

Provides MCP tools for managing resumable uploads (create, status, abort, commit)
and ingesting turns directly, seamlessly integrating with the Conversation Ingestion Pipeline
and SQLite FTS conversation retrieval.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..conversations.chatgpt_export import (
    ChatGPTExportError,
    ChatGPTExportReader,
    detect_export_format,
    resolve_account_namespace_hash,
)
from ..conversations.cli import _select_import_keys
from ..conversations.manifest import ImportManifest
from ..conversations.pipeline import ConversationIngestionPipeline
from ..conversations.vault_writer import ObsidianConversationWriter
from ..service import ResearchMemoryService

logger = logging.getLogger(__name__)


def conversation_upload_create(
    service: ResearchMemoryService,
    *,
    filename: str,
    size_bytes: int,
    sha256: str,
    content_type: str = "application/zip",
    source_hint: str | None = None,
    expiry_hours: int | None = None,
) -> dict[str, Any]:
    session = service.upload_manager.create_upload(
        filename=filename,
        size_bytes=size_bytes,
        sha256=sha256,
        content_type=content_type,
        source_hint=source_hint,
        expiry_hours=expiry_hours,
    )
    upload_url = f"{service.config.upload.upload_path}/{session.upload_id}"
    return {
        "upload_id": session.upload_id,
        "upload_url": upload_url,
        "tus_version": "1.0.0",
        "chunk_size_recommended": 1048576,
        "filename": session.filename,
        "size_bytes": session.size_bytes,
        "sha256": session.sha256,
        "expires_at": session.expires_at,
        "state": session.state,
    }


def conversation_upload_status(
    service: ResearchMemoryService,
    *,
    upload_id: str,
) -> dict[str, Any]:
    return service.upload_manager.get_upload_status(upload_id)


def conversation_upload_abort(
    service: ResearchMemoryService,
    *,
    upload_id: str,
) -> dict[str, Any]:
    res = service.upload_manager.abort_upload(upload_id)
    return {
        "upload_id": upload_id,
        "aborted": res,
        "state": "aborted" if res else "unknown",
    }


def conversation_upload_commit(
    service: ResearchMemoryService,
    *,
    upload_id: str,
    vault_confirmed: bool = False,
    target_vault: str | None = None,
    format_hint: str = "auto",
    account_namespace: str = "",
    use_default_account_namespace: bool = False,
    dry_run: bool = False,
    resume: bool = True,
    limit: int | None = None,
) -> dict[str, Any]:
    upload_status = service.upload_manager.get_upload_status(upload_id)
    if not upload_status["completed"] and upload_status["state"] != "committed":
        raise ValueError(
            f"Cannot commit upload {upload_id!r}: upload is incomplete "
            f"({upload_status['received_bytes']}/{upload_status['total_bytes']} bytes received)"
        )

    # Verify and finalize into immutable blob store (fails closed on hash mismatch)
    blob_path = service.upload_manager.verify_and_finalize(upload_id)

    # Detect format and construct reader
    original_filename = upload_status.get("filename", "")
    detected_format = format_hint if format_hint != "auto" else detect_export_format(blob_path)
    if detected_format == "ambiguous":
        raise ValueError(
            "Archive matches multiple formats; pass format_hint explicitly ('chatgpt' or 'codex')."
        )
    if detected_format not in ("chatgpt", "codex"):
        raise ValueError(
            f"Unsupported archive format: {detected_format}. Expected 'chatgpt' or 'codex'."
        )

    if detected_format == "codex":
        from ..conversations.readers import CodexExportReader

        reader = CodexExportReader(blob_path)
    else:
        ns_hash, _strategy = resolve_account_namespace_hash(
            blob_path,
            namespace_label=account_namespace,
            use_default=use_default_account_namespace,
        )
        reader = ChatGPTExportReader(blob_path, account_namespace_hash=ns_hash)

    # Resolve output destination
    archive_cfg = service.config.conversation_archive
    if vault_confirmed and archive_cfg.vault_root:
        output_root = archive_cfg.resolve_vault_root(target_vault or "", confirmed=True)
    else:
        output_root = archive_cfg.resolve_staging_dir()

    writer = ObsidianConversationWriter(output_root)
    manifest_path = archive_cfg.resolve_archive_manifest_path(output_root)
    manifest = ImportManifest(manifest_path)

    # Create ingestion pipeline
    allowlist = [entry.path for entry in service.config.sources.allowlist]
    from ..conversations.attachments import AttachmentInventory

    attachment_inventory = AttachmentInventory(
        allowlist_roots=allowlist,
        archive_asset_resolver=getattr(reader, "resolve_asset", None),
    )

    pipeline = ConversationIngestionPipeline(
        reader=reader,
        writer=writer,
        manifest=manifest,
        attachment_inventory=attachment_inventory,
    )

    target_keys = _select_import_keys(reader, [], [], limit)
    results = pipeline.run(target_keys, dry_run=dry_run, resume=resume)

    # Index newly written or updated notes into SQLite FTS
    if not dry_run:
        index_db = service.conversation_retrieval.index_db
        for r in results:
            if r.status in ("written", "hydrated") and r.output_path:
                try:
                    index_db.index_file(r.output_path)
                except Exception as e:
                    logger.warning("Failed to index conversation note %s: %s", r.output_path, e)

    written_count = sum(1 for r in results if r.status == "written")
    skipped_count = sum(1 for r in results if r.status == "skipped")
    conflict_count = sum(1 for r in results if r.status == "conflict")
    failed_count = sum(1 for r in results if r.status == "failed_retryable")
    dry_run_count = sum(1 for r in results if r.status == "dry_run")

    return {
        "upload_id": upload_id,
        "blob_sha256": upload_status.get("final_sha256") or upload_status.get("sha256"),
        "format": detected_format,
        "status": "dry_run" if dry_run else "committed",
        "output_root": str(output_root),
        "total_sessions": len(target_keys),
        "counts": {
            "written": written_count,
            "skipped": skipped_count,
            "conflict": conflict_count,
            "failed": failed_count,
            "dry_run": dry_run_count,
        },
        "results": [
            {
                "conversation_id": r.conversation_id,
                "status": r.status,
                "reason": r.reason,
                "output_path": r.output_path,
                "error": r.error,
            }
            for r in results[:100]
        ],
        "warnings": getattr(reader, "schema_warnings", []),
    }


def conversation_ingest_turn(
    service: ResearchMemoryService,
    *,
    session_id: str,
    role: str,
    content: str,
    title: str | None = None,
    model: str | None = None,
    timestamp: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Ingest a single turn of conversation directly without ZIP upload.

    Appends to or creates a note in the conversation staging directory and updates
    the FTS index for instant recall.
    """
    if role not in ("user", "assistant", "system"):
        raise ValueError(f"Invalid role: {role!r}. Must be 'user', 'assistant', or 'system'.")

    archive_cfg = service.config.conversation_archive
    staging_dir = archive_cfg.resolve_staging_dir()
    staging_dir.mkdir(parents=True, exist_ok=True)

    safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in session_id)
    session_file = staging_dir / f"agent_{safe_id}.md"

    now_iso = timestamp or datetime.now(timezone.utc).isoformat()
    clean_title = title or f"Agent Conversation {safe_id[:8]}"

    if not session_file.exists():
        initial_content = (
            f"---\n"
            f"conversation_id: {session_id}\n"
            f"source_system: agent\n"
            f"title: \"{clean_title}\"\n"
            f"created_at: \"{now_iso}\"\n"
            f"updated_at: \"{now_iso}\"\n"
            f"---\n\n"
            f"# {clean_title}\n\n"
            f"## {role.title()} ({now_iso})\n\n{content.strip()}\n\n"
        )
        session_file.write_text(initial_content, encoding="utf-8")
    else:
        append_content = f"## {role.title()} ({now_iso})\n\n{content.strip()}\n\n"
        with session_file.open("a", encoding="utf-8") as f:
            f.write(append_content)

    # Index into FTS
    index_db = service.conversation_retrieval.index_db
    try:
        index_db.index_file(session_file)
        indexed = True
    except Exception as e:
        logger.warning("Failed to index agent turn note: %s", e)
        indexed = False

    return {
        "status": "ingested",
        "session_id": session_id,
        "file_path": str(session_file),
        "role": role,
        "indexed": indexed,
        "timestamp": now_iso,
    }


def conversation_ingest_snapshot(
    service: ResearchMemoryService,
    *,
    session_id: str,
    messages: list[dict[str, Any]],
    title: str | None = None,
    model: str | None = None,
    metadata: dict[str, Any] | None = None,
    overwrite: bool = True,
) -> dict[str, Any]:
    """Ingest a complete conversation snapshot directly into staging and index into FTS."""
    if not isinstance(messages, list) or not messages:
        raise ValueError("messages must be a non-empty list of message dicts.")

    archive_cfg = service.config.conversation_archive
    staging_dir = archive_cfg.resolve_staging_dir()
    staging_dir.mkdir(parents=True, exist_ok=True)

    safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in session_id)
    session_file = staging_dir / f"agent_{safe_id}.md"

    now_iso = datetime.now(timezone.utc).isoformat()
    clean_title = title or f"Agent Conversation {safe_id[:8]}"

    lines = [
        "---",
        f"conversation_id: {session_id}",
        "source_system: agent",
        f"title: \"{clean_title}\"",
    ]
    if model:
        lines.append(f"model: \"{model}\"")
    lines.extend([
        f"created_at: \"{now_iso}\"",
        f"updated_at: \"{now_iso}\"",
        "---",
        "",
        f"# {clean_title}",
        "",
    ])

    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        ts = msg.get("timestamp") or now_iso
        lines.append(f"## {role.title()} ({ts})\n\n{str(content).strip()}\n")

    session_file.write_text("\n".join(lines), encoding="utf-8")

    index_db = service.conversation_retrieval.index_db
    try:
        index_db.index_file(session_file)
        indexed = True
    except Exception as e:
        logger.warning("Failed to index agent snapshot note: %s", e)
        indexed = False

    return {
        "status": "ingested",
        "session_id": session_id,
        "message_count": len(messages),
        "file_path": str(session_file),
        "indexed": indexed,
        "timestamp": now_iso,
    }
