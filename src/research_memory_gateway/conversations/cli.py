from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from research_memory_gateway.config import AppConfig, load_config
from research_memory_gateway.retrieval import EmbeddingClient
from .attachments import AttachmentInventory
from .codex_export import CodexExportReader
from .identity_store import ConversationIdentityStore, load_legacy_import_rows
from .index import ConversationIndexDatabase
from .manifest import ImportManifest
from .pipeline import ConversationIngestionPipeline
from .retrieval import ConversationRetrievalService
from .vault_writer import ObsidianConversationWriter

DEFAULT_EMBEDDING_VERSION = "v1"


def _resolve_explicit_staging(cfg: AppConfig, value: str) -> Path:
    configured_root = cfg.conversation_archive.resolve_staging_dir()
    target = Path(value).resolve()
    try:
        target.relative_to(configured_root)
    except ValueError as exc:
        raise PermissionError(
            f"--staging-dir must stay within configured staging root: {configured_root}"
        ) from exc
    if cfg.conversation_archive.vault_root:
        try:
            vault = cfg.conversation_archive.resolve_vault_root(confirmed=True)
            target.relative_to(vault)
        except ValueError:
            pass
        else:
            raise PermissionError("--staging-dir cannot target canonical vault; use --vault --confirm-vault")
    return target


def _record_manifest_indexing(
    cfg: AppConfig,
    target_dir: Path,
    idx: ConversationIndexDatabase,
    path: Path,
) -> None:
    manifest_path = cfg.conversation_archive.resolve_archive_manifest_path(target_dir)
    if not manifest_path.exists():
        return
    metadata = idx.index_metadata_for_file(path)
    if not metadata:
        return
    # The legacy ledger is keyed by the bare provider conversation id; the
    # documents.id may be a v0.2.4 source key for multi-source notes.
    record_key = metadata.get("legacy_conversation_id") or metadata["conversation_id"]
    ImportManifest(manifest_path).record_indexing(
        record_key,
        index_source_hash=metadata["index_source_hash"],
        last_indexed_at=metadata["last_indexed_at"],
        embedding_model=metadata["embedding_model"],
        embedding_version=metadata["embedding_version"],
        embedding_dimension=metadata["embedding_dimension"],
        content_section_hashes=metadata["content_section_hashes"],
    )


def _manifest_index_is_stale(
    cfg: AppConfig,
    target_dir: Path,
    idx: ConversationIndexDatabase,
    path: Path,
) -> bool:
    manifest_path = cfg.conversation_archive.resolve_archive_manifest_path(target_dir)
    if not manifest_path.exists():
        return False
    metadata = idx.index_metadata_for_file(path)
    if not metadata:
        return False
    record_key = metadata.get("legacy_conversation_id") or metadata["conversation_id"]
    record = ImportManifest(manifest_path).get_record(record_key)
    if not record:
        return False
    return bool(
        record.get("status") == "index_stale"
        or not record.get("last_indexed_at")
        or not record.get("index_source_hash")
        or record.get("index_source_hash") != metadata["index_source_hash"]
        or not record.get("content_section_hashes")
    )


def _estimate_missing_embeddings(
    md_files: list[Path],
    db_path: Path,
    *,
    model: str,
    version: str = DEFAULT_EMBEDDING_VERSION,
) -> tuple[int, int]:
    """Estimate unique vector identities without mutating the DB or calling the embedding service."""
    from .chunking import HeadingChunker

    chunker = HeadingChunker()
    identities: set[str] = set()
    for path in md_files:
        _, chunks = chunker.chunk_file(
            path,
            embedding_model=model,
            embedding_version=version,
        )
        identities.update(chunk.embedding_identity for chunk in chunks if chunk.embedding_identity)

    if not identities or not db_path.exists():
        return len(identities), len(identities)

    cached: set[str] = set()
    try:
        uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as conn:
            placeholders = ",".join("?" for _ in identities)
            rows = conn.execute(
                f"""
                SELECT embedding_identity FROM conversation_embeddings
                WHERE model = ? AND version = ?
                  AND embedding_identity IN ({placeholders})
                """,
                [model, version, *sorted(identities)],
            ).fetchall()
            cached = {str(row[0]) for row in rows}
    except sqlite3.Error:
        cached = set()
    return len(identities), len(identities - cached)


def _load_app_config(config_path: str | None) -> AppConfig:
    if config_path and Path(config_path).exists():
        return load_config(config_path)
    if Path("config.yaml").exists():
        return load_config("config.yaml")
    return AppConfig()


def cmd_audit_export(args: argparse.Namespace) -> int:
    archive_path = Path(args.archive)
    if not archive_path.exists():
        print(f"Error: Archive not found: {archive_path}", file=sys.stderr)
        return 1

    reader = CodexExportReader(archive_path)
    sessions = reader.sessions()
    manifest_meta = reader.manifest()

    subagents = sum(1 for s in sessions if "subagent" in s.relative_rollout_path or s.source_instance == "subagent")
    report = {
        "archive_path": str(archive_path.resolve()),
        "archive_sha256": reader.archive_sha256,
        "package_version": manifest_meta.get("packageVersion"),
        "exported_at": manifest_meta.get("exportedAt"),
        "total_sessions": len(sessions),
        "estimated_subagents": subagents,
    }

    if args.json_report:
        Path(args.json_report).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    cfg = _load_app_config(args.config)
    archive_path = Path(args.archive)
    if not archive_path.exists():
        print(f"Error: Archive not found: {archive_path}", file=sys.stderr)
        return 1

    if getattr(args, "staging_dir", None):
        try:
            output_root = _resolve_explicit_staging(cfg, args.staging_dir)
        except (PermissionError, FileNotFoundError, ValueError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
    elif args.vault:
        if not args.confirm_vault:
            print("Error: Writing to canonical vault requires --confirm-vault", file=sys.stderr)
            return 1
        output_root = cfg.conversation_archive.resolve_canonical_root(confirmed=True)
    else:
        output_root = cfg.conversation_archive.resolve_staging_dir()

    output_root.mkdir(parents=True, exist_ok=True)
    manifest_file = cfg.conversation_archive.resolve_archive_manifest_path(output_root)
    csv_file = output_root / ".ai-memory" / "manifest.csv"

    reader = CodexExportReader(archive_path)
    writer = ObsidianConversationWriter(output_root)
    manifest = ImportManifest(manifest_file)
    attachment_inventory = AttachmentInventory(
        allowlist_roots=[entry.path for entry in cfg.sources.allowlist]
    )
    pipeline = ConversationIngestionPipeline(
        reader,
        writer,
        manifest,
        attachment_inventory=attachment_inventory,
    )

    all_refs = reader.sessions()
    if args.session_id:
        target_ids = [sid for sid in args.session_id]
    else:
        target_ids = [s.conversation_id for s in all_refs]

    if args.limit:
        target_ids = target_ids[: args.limit]

    results = pipeline.run(target_ids, dry_run=args.dry_run, resume=args.resume)
    if not args.dry_run:
        manifest.export_csv(csv_file)

    counts = {
        "written": sum(1 for r in results if r.status == "written"),
        "skipped": sum(1 for r in results if r.status == "skipped"),
        "index_stale": sum(1 for r in results if r.status == "index_stale"),
        "conflict": sum(1 for r in results if r.status == "conflict"),
        "index_stale": sum(1 for r in results if r.status == "index_stale"),
        "failed_retryable": sum(1 for r in results if r.status == "failed_retryable"),
        "dry_run": sum(1 for r in results if r.status == "dry_run"),
    }

    summary = {
        "total_requested": len(target_ids),
        "output_root": str(output_root),
        "dry_run": args.dry_run,
        "resume": getattr(args, "resume", False),
        "counts": counts,
        "results": [r.__dict__ for r in results],
    }

    if args.json_report:
        Path(args.json_report).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def cmd_inventory_attachments(args: argparse.Namespace) -> int:
    cfg = _load_app_config(args.config)
    archive_path = Path(args.archive)
    if not archive_path.exists():
        print(f"Error: Archive not found: {archive_path}", file=sys.stderr)
        return 1

    reader = CodexExportReader(archive_path)
    all_refs = reader.sessions()
    if args.session_id:
        target_ids = [sid for sid in args.session_id]
    else:
        target_ids = [s.conversation_id for s in all_refs]
    if args.limit:
        target_ids = target_ids[: args.limit]

    allowlist = [entry.path for entry in cfg.sources.allowlist]

    inventory = AttachmentInventory(allowlist_roots=allowlist)
    convs = [reader.parse(sid) for sid in target_ids]
    records = inventory.scan_many(convs)

    if args.missing_only:
        records = inventory.missing_records(records)

    if args.output_json:
        inventory.export_json(records, args.output_json)
    if args.output_csv:
        inventory.export_csv(records, args.output_csv)

    print(f"Scanned {len(target_ids)} conversations, found {len(records)} attachment records.")
    return 0


def cmd_rebuild_index(args: argparse.Namespace) -> int:
    if not args.dry_run and not args.confirm_rebuild:
        print("Error: Rebuilding index will clear existing tables. Use --confirm-rebuild to proceed.", file=sys.stderr)
        return 1

    cfg = _load_app_config(args.config)
    target_dir = Path(args.dir) if args.dir else cfg.conversation_archive.resolve_staging_dir()
    try:
        db_path = cfg.conversation_archive.resolve_index_path()
    except (PermissionError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    md_files = sorted(list(target_dir.rglob("*.md")))
    if getattr(args, "session_id", None):
        target_sids = set(args.session_id)
        md_files = [f for f in md_files if any(sid[:8] in f.name for sid in target_sids)]
    if getattr(args, "limit", None):
        md_files = md_files[: args.limit]

    if args.dry_run:
        from .chunking import HeadingChunker

        chunker = HeadingChunker()
        estimated_chunks = sum(len(chunker.chunk_file(path)[1]) for path in md_files)
        embedding_client = EmbeddingClient(cfg.retrieval.embedding)
        embedding_effective = bool(embedding_client.enabled and embedding_client.model)
        estimated_identities = 0
        estimated_embeddings = 0
        if embedding_effective:
            estimated_identities, estimated_embeddings = _estimate_missing_embeddings(
                md_files,
                db_path,
                model=embedding_client.model,
            )
        summary = {
            "markdown_files_found": len(md_files),
            "estimated_chunks": estimated_chunks,
            "estimated_embedding_identities": estimated_identities,
            "estimated_embeddings": estimated_embeddings,
            "dry_run": True,
            "index_db_path": str(db_path.resolve()),
            "embedding_enabled": embedding_effective,
        }
        if args.json_report:
            Path(args.json_report).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    embedding_client = None
    if cfg.retrieval.embedding.enabled:
        embedding_client = EmbeddingClient(cfg.retrieval.embedding)

    idx = ConversationIndexDatabase(db_path, embedding_client=embedding_client)
    count = idx.rebuild_from_markdown(md_files)
    for f in md_files:
        _record_manifest_indexing(cfg, target_dir, idx, f)

    idx.record_index_run(
        run_at=datetime.now(timezone.utc).isoformat(),
        indexed_files=len(md_files),
        skipped_files=0,
        total_chunks=count,
        embedded_chunks=idx.stats_new_embeddings if (embedding_client and embedding_client.enabled) else 0,
        cache_reused_chunks=idx.stats_cache_reused,
        failed_chunks=idx.stats_failures,
        model=embedding_client.model if (embedding_client and embedding_client.enabled) else "none",
        status="success",
    )

    summary = {
        "markdown_files_found": len(md_files),
        "total_chunks_indexed": count,
        "index_db_path": str(db_path.resolve()),
        "embedding_enabled": bool(embedding_client and embedding_client.enabled),
        "new_embedding_requests": idx.stats_new_embeddings,
        "cache_reused_chunks": idx.stats_cache_reused,
        "embedding_failures": idx.stats_failures,
        "dimension_mismatches": idx.stats_dimension_mismatches,
    }
    if args.json_report:
        Path(args.json_report).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def cmd_index(args: argparse.Namespace) -> int:
    cfg = _load_app_config(args.config)
    target_dir = Path(args.dir) if args.dir else cfg.conversation_archive.resolve_staging_dir()
    try:
        db_path = cfg.conversation_archive.resolve_index_path()
    except (PermissionError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    md_files = sorted(list(target_dir.rglob("*.md")))
    if getattr(args, "session_id", None):
        target_sids = set(args.session_id)
        md_files = [f for f in md_files if any(sid[:8] in f.name for sid in target_sids)]
    if getattr(args, "limit", None):
        md_files = md_files[: args.limit]

    embedding_client = None
    if cfg.retrieval.embedding.enabled:
        embedding_client = EmbeddingClient(cfg.retrieval.embedding)

    idx = ConversationIndexDatabase(db_path, embedding_client=embedding_client)

    files_scanned = len(md_files)
    indexed_files = 0
    skipped_unchanged = 0
    total_chunks = 0
    change_reasons: dict[str, int] = {}

    for f in md_files:
        reason = idx.check_changed_reason(f)
        if reason == "unchanged" and _manifest_index_is_stale(cfg, target_dir, idx, f):
            reason = "index_stale"
        change_reasons[reason] = change_reasons.get(reason, 0) + 1
        if args.changed_only and reason == "unchanged":
            skipped_unchanged += 1
            continue
        chunks_count = idx.index_file(f, dry_run=args.dry_run)
        total_chunks += chunks_count
        indexed_files += 1
        if not args.dry_run:
            _record_manifest_indexing(cfg, target_dir, idx, f)

    summary = {
        "files_scanned": files_scanned,
        "indexed_files": indexed_files,
        "skipped_unchanged": skipped_unchanged,
        "chunks_indexed": total_chunks,
        "change_reasons": change_reasons,
        "dry_run": args.dry_run,
        "index_db_path": str(db_path.resolve()),
        "embedding_enabled": bool(embedding_client and embedding_client.enabled),
        "new_embedding_requests": idx.stats_new_embeddings,
        "cache_reused_chunks": idx.stats_cache_reused,
        "embedding_failures": idx.stats_failures,
        "dimension_mismatches": idx.stats_dimension_mismatches,
    }

    if not args.dry_run:
        idx.record_index_run(
            run_at=datetime.now(timezone.utc).isoformat(),
            indexed_files=indexed_files,
            skipped_files=skipped_unchanged,
            total_chunks=total_chunks,
            embedded_chunks=idx.stats_new_embeddings if (embedding_client and embedding_client.enabled) else 0,
            cache_reused_chunks=idx.stats_cache_reused,
            failed_chunks=idx.stats_failures,
            model=embedding_client.model if (embedding_client and embedding_client.enabled) else "none",
            status="success",
        )

    if getattr(args, "json_report", None):
        Path(args.json_report).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def _resolve_identity_root(cfg: AppConfig, dir_value: str | None) -> Path:
    """Resolve the conversation root whose archive-local manifest holds identity tables."""
    if dir_value:
        return Path(dir_value).resolve()
    return cfg.conversation_archive.resolve_staging_dir()


def _candidate_display(store: ConversationIdentityStore, candidate) -> dict[str, Any]:
    def side(source_key: str) -> dict[str, Any]:
        record = store.get_source_record(source_key)
        if record is None:
            return {"source_key": source_key, "missing": True}
        return {
            "source_key": record.source_key,
            "canonical_conversation_id": record.canonical_conversation_id,
            "source_system": record.source_system,
            "source_conversation_id": record.source_conversation_id,
            "output_path": record.output_path,
            "message_count": record.message_count,
            "ordered_message_hash": record.ordered_message_hash,
            "message_set_hash": record.message_set_hash,
            "first_seen_at": record.first_seen_at,
            "last_seen_at": record.last_seen_at,
        }

    return {
        "candidate_id": candidate.candidate_id,
        "candidate_type": candidate.candidate_type,
        "status": candidate.status,
        "score": candidate.score,
        "evidence": candidate.evidence(),
        "created_at": candidate.created_at,
        "reviewed_at": candidate.reviewed_at,
        "left": side(candidate.left_source_key),
        "right": side(candidate.right_source_key),
    }


def cmd_dedup_audit(args: argparse.Namespace) -> int:
    cfg = _load_app_config(args.config)
    root = _resolve_identity_root(cfg, getattr(args, "dir", None))
    manifest_path = cfg.conversation_archive.resolve_archive_manifest_path(root)
    if not manifest_path.exists():
        print(f"Error: Manifest database not found: {manifest_path}", file=sys.stderr)
        return 1
    store = ConversationIdentityStore(manifest_path)

    candidates = store.list_candidates()
    pending = [c for c in candidates if c.status == "pending"]
    aliases: list[dict[str, str]] = []
    with store._connect() as connection:
        for row in connection.execute(
            "SELECT alias_canonical_id, active_canonical_id, created_at, reason FROM canonical_aliases"
        ).fetchall():
            aliases.append(dict(row))
    orphans = store.list_orphan_source_records()
    duplicate_paths = store.duplicate_output_paths()
    source_key_collisions = store.source_key_collision_count()

    # Canonical id collisions: two different active canonical rows pointing at
    # the same derived uuid5 would indicate the derivation broke.
    canonical_ids: dict[str, int] = {}
    with store._connect() as connection:
        for row in connection.execute(
            "SELECT canonical_conversation_id, COUNT(*) FROM conversation_source_records GROUP BY 1"
        ).fetchall():
            canonical_ids[row[0]] = int(row[1])
    canonical_collisions = [
        {"canonical_conversation_id": cid, "source_records": n}
        for cid, n in sorted(canonical_ids.items())
        if n > 1
    ]
    # Same canonical group must not contain two records claiming one output path.
    report = {
        "manifest_path": str(manifest_path),
        "identity_stats": store.identity_stats(),
        "pending_candidates": [c.candidate_id for c in pending],
        "pending_candidate_count": len(pending),
        "candidate_count": len(candidates),
        "canonical_aliases": aliases,
        "orphan_source_records": [r.source_key for r in orphans],
        "duplicate_output_paths": [
            {"output_path": path, "count": n} for path, n in duplicate_paths
        ],
        "source_key_collisions": source_key_collisions,
        "canonical_collisions": canonical_collisions,
    }
    if getattr(args, "json_report", None):
        Path(args.json_report).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def cmd_dedup_list(args: argparse.Namespace) -> int:
    cfg = _load_app_config(args.config)
    root = _resolve_identity_root(cfg, getattr(args, "dir", None))
    manifest_path = cfg.conversation_archive.resolve_archive_manifest_path(root)
    if not manifest_path.exists():
        print(f"Error: Manifest database not found: {manifest_path}", file=sys.stderr)
        return 1
    store = ConversationIdentityStore(manifest_path)
    candidates = store.list_candidates(
        status=getattr(args, "status", None),
        source_system=getattr(args, "source_system", None),
        candidate_type=getattr(args, "candidate_type", None),
    )
    payload = {
        "count": len(candidates),
        "candidates": [_candidate_display(store, c) for c in candidates],
    }
    if getattr(args, "json_report", None):
        Path(args.json_report).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def cmd_dedup_show(args: argparse.Namespace) -> int:
    cfg = _load_app_config(args.config)
    root = _resolve_identity_root(cfg, getattr(args, "dir", None))
    manifest_path = cfg.conversation_archive.resolve_archive_manifest_path(root)
    if not manifest_path.exists():
        print(f"Error: Manifest database not found: {manifest_path}", file=sys.stderr)
        return 1
    store = ConversationIdentityStore(manifest_path)
    candidate = store.get_candidate(int(args.candidate_id))
    if candidate is None:
        print(f"Error: Unknown candidate id: {args.candidate_id}", file=sys.stderr)
        return 1
    payload = _candidate_display(store, candidate)
    if getattr(args, "json_report", None):
        Path(args.json_report).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _refresh_index_canonical_metadata(
    cfg: AppConfig,
    *,
    source_keys: list[str],
    canonical_conversation_id: str,
) -> bool:
    """Point indexed documents/sections at the winning canonical id.

    Metadata-only update: contents, vectors and paths are untouched.  Returns
    True when the index database was found and updated.
    """
    try:
        db_path = cfg.conversation_archive.resolve_index_path()
    except (PermissionError, ValueError):
        return False
    if not db_path.exists():
        return False
    with sqlite3.connect(db_path) as conn:
        for source_key in source_keys:
            for table in ("conversation_documents", "conversation_sections"):
                try:
                    conn.execute(
                        f"UPDATE {table} SET canonical_conversation_id = ? WHERE source_key = ?",
                        (canonical_conversation_id, source_key),
                    )
                except sqlite3.OperationalError:
                    return False
    return True


def cmd_dedup_resolve(args: argparse.Namespace) -> int:
    cfg = _load_app_config(args.config)
    root = _resolve_identity_root(cfg, getattr(args, "dir", None))
    manifest_path = cfg.conversation_archive.resolve_archive_manifest_path(root)
    if not manifest_path.exists():
        print(f"Error: Manifest database not found: {manifest_path}", file=sys.stderr)
        return 1
    confirm_same = getattr(args, "confirm_same", False)
    reject = getattr(args, "reject", False)
    if confirm_same == reject:
        print(
            "Error: dedup-resolve requires exactly one explicit decision: --confirm-same or --reject",
            file=sys.stderr,
        )
        return 2
    store = ConversationIdentityStore(manifest_path)
    candidate = store.get_candidate(int(args.candidate_id))
    if candidate is None:
        print(f"Error: Unknown candidate id: {args.candidate_id}", file=sys.stderr)
        return 1

    if reject:
        updated = store.resolve_candidate(
            candidate.candidate_id, decision="rejected", note=getattr(args, "note", "") or ""
        )
        payload = {
            "candidate_id": updated.candidate_id,
            "decision": "rejected",
            "status": updated.status,
            "note": "Source records and canonical conversations were left unchanged.",
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    # --confirm-same: link both source records under one active canonical.
    left = store.get_source_record(candidate.left_source_key)
    right = store.get_source_record(candidate.right_source_key)
    if left is None or right is None:
        print("Error: Both candidate source records must exist.", file=sys.stderr)
        return 1
    try:
        # Decision + canonical link happen in one identity-store transaction;
        # repeated confirms are idempotent no-ops, rejected candidates refuse.
        result = store.confirm_candidate_link(
            candidate.candidate_id,
            winner_source_key=getattr(args, "winner_source_key", "") or "",
            note=getattr(args, "note", "") or "",
        )
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    winner_canonical = result["active_canonical_conversation_id"]
    index_updated = _refresh_index_canonical_metadata(
        cfg,
        source_keys=[left.source_key, right.source_key],
        canonical_conversation_id=winner_canonical,
    )
    payload = {
        "candidate_id": candidate.candidate_id,
        "decision": "confirmed_same",
        "outcome": result["outcome"],
        "winner_source_key": result["winner_source_key"],
        "active_canonical_conversation_id": winner_canonical,
        "loser_alias_resolved": True,
        "source_records_removed": 0,
        "markdown_notes_removed": 0,
        "index_canonical_metadata_updated": index_updated,
        "note": "Both source notes remain on disk; the loser canonical resolves via alias.",
    }
    if getattr(args, "json_report", None):
        Path(args.json_report).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def cmd_identity_show(args: argparse.Namespace) -> int:
    cfg = _load_app_config(args.config)
    root = _resolve_identity_root(cfg, getattr(args, "dir", None))
    manifest_path = cfg.conversation_archive.resolve_archive_manifest_path(root)
    if not manifest_path.exists():
        print(f"Error: Manifest database not found: {manifest_path}", file=sys.stderr)
        return 1
    store = ConversationIdentityStore(manifest_path)
    key = args.conversation_or_source_key
    if key.startswith("srcv1_"):
        records = [r for r in [store.get_source_record(key)] if r is not None]
    else:
        records = store.find_source_records_by_conversation(key)
    if not records:
        print(f"Error: No source records found for: {key}", file=sys.stderr)
        return 1
    payload: dict[str, Any] = {"query": key, "source_record_count": len(records), "records": []}
    for record in records:
        canonical = store.resolve_canonical(record.canonical_conversation_id)
        payload["records"].append(
            {
                "source_key": record.source_key,
                "source_system": record.source_system,
                "source_conversation_id": record.source_conversation_id,
                "source_thread_id": record.source_thread_id,
                "source_branch_id": record.source_branch_id,
                "canonical_conversation_id": record.canonical_conversation_id,
                "active_canonical_conversation_id": canonical.canonical_conversation_id
                if canonical
                else "",
                "output_path": record.output_path,
                "message_count": record.message_count,
                "normalized_transcript_sha256": record.normalized_transcript_sha256,
                "ordered_message_hash": record.ordered_message_hash,
                "message_set_hash": record.message_set_hash,
                "fingerprint_version": record.fingerprint_version,
                "parser_version": record.parser_version,
                "schema_version": record.schema_version,
                "first_seen_at": record.first_seen_at,
                "last_seen_at": record.last_seen_at,
                "snapshots": [s.__dict__ for s in store.list_snapshots(record.source_key)],
            }
        )
    if getattr(args, "json_report", None):
        Path(args.json_report).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def cmd_migrate_identity(args: argparse.Namespace) -> int:
    """Run the additive legacy -> v2 identity migration on one manifest DB."""
    cfg = _load_app_config(args.config)
    root = _resolve_identity_root(cfg, getattr(args, "dir", None))
    manifest_path = cfg.conversation_archive.resolve_archive_manifest_path(root)
    if not manifest_path.exists():
        print(f"Error: Manifest database not found: {manifest_path}", file=sys.stderr)
        return 1
    store = ConversationIdentityStore(manifest_path)
    legacy_rows = load_legacy_import_rows(manifest_path)
    report = store.migrate_legacy_imports(legacy_rows)
    print(json.dumps(report.__dict__, ensure_ascii=False, indent=2))
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    cfg = _load_app_config(args.config)
    try:
        db_path = cfg.conversation_archive.resolve_index_path()
    except (PermissionError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    if not db_path.exists():
        print(f"Error: Index database does not exist: {db_path}", file=sys.stderr)
        return 1

    allowed_roots = [cfg.conversation_archive.resolve_staging_dir()]
    if cfg.conversation_archive.vault_root:
        try:
            allowed_roots.append(cfg.conversation_archive.resolve_vault_root(confirmed=True))
        except Exception:
            pass

    embedding_client = None
    if cfg.retrieval.embedding.enabled:
        embedding_client = EmbeddingClient(cfg.retrieval.embedding)

    idx = ConversationIndexDatabase(db_path, embedding_client=embedding_client)
    service = ConversationRetrievalService(idx, allowed_roots=allowed_roots, embedding_client=embedding_client)
    res = service.search(
        args.query,
        project=args.project,
        conversation_id=args.conversation_id,
        parent_thread_id=getattr(args, "parent_thread_id", None),
        source_system=getattr(args, "source_system", None),
        canonical_conversation_id=getattr(args, "canonical_conversation_id", None),
        source_key=getattr(args, "source_key", None),
        source_conversation_id=getattr(args, "source_conversation_id", None),
        limit=args.limit,
    )
    if getattr(args, "json_report", None):
        Path(args.json_report).write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    cfg = _load_app_config(args.config)
    staging_dir = Path(args.staging_dir) if args.staging_dir else cfg.conversation_archive.resolve_staging_dir()
    if not staging_dir.exists():
        print(f"Error: Staging dir not found: {staging_dir}", file=sys.stderr)
        return 1

    md_files = list(staging_dir.rglob("*.md"))
    valid_count = 0
    errors: list[str] = []

    for f in md_files:
        text = f.read_text(encoding="utf-8")
        if "data:image/" in text or ";base64," in text:
            errors.append(f"{f.name}: Contains unescaped base64 image data")
        if "conversation_id:" not in text:
            errors.append(f"{f.name}: Missing conversation_id in frontmatter")
        if "<!-- source ordinal=" not in text and "Nihao" not in f.name:
            errors.append(f"{f.name}: Missing source anchor comments")
        valid_count += 1

    summary = {
        "total_inspected": len(md_files),
        "valid_count": valid_count - len(errors),
        "errors": errors,
    }
    if getattr(args, "json_report", None):
        Path(args.json_report).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="conversation", description="Conversation Memory Management CLI")
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    # 1. audit-export
    p_audit = subparsers.add_parser("audit-export", help="Audit export ZIP integrity and statistics")
    p_audit.add_argument("archive", help="Path to raw export ZIP")
    p_audit.add_argument("--json-report", help="Output path for JSON report")
    p_audit.set_defaults(func=cmd_audit_export)

    # 2. import
    p_import = subparsers.add_parser("import", help="Import conversations to staging notes")
    p_import.add_argument("archive", help="Path to raw export ZIP")
    p_import.add_argument("--config", help="Path to config.yaml")
    p_import.add_argument("--staging", action="store_true", default=True, help="Target staging folder")
    p_import.add_argument("--staging-dir", help="Explicit staging output directory")
    p_import.add_argument("--vault", action="store_true", help="Target canonical vault (requires --confirm-vault)")
    p_import.add_argument("--confirm-vault", action="store_true", help="Explicit confirmation for vault write")
    p_import.add_argument("--session-id", action="append", help="Specific session ID to import (repeatable)")
    p_import.add_argument("--limit", type=int, help="Limit number of sessions")
    p_import.add_argument("--dry-run", action="store_true", help="Simulate import without modifying files")
    resume_group = p_import.add_mutually_exclusive_group()
    resume_group.add_argument("--resume", dest="resume", action="store_true", help="Resume safely: retry failures and skip unchanged (default)")
    resume_group.add_argument("--no-resume", dest="resume", action="store_false", help="Force rewrite of unchanged selected sessions while preserving conflict checks")
    p_import.set_defaults(resume=True)
    p_import.add_argument("--json-report", help="Output path for JSON report")
    p_import.set_defaults(func=cmd_import)

    # 3. inventory-attachments
    p_att = subparsers.add_parser("inventory-attachments", help="Scan attachment references and report missing files")
    p_att.add_argument("archive", help="Path to raw export ZIP")
    p_att.add_argument("--config", help="Path to config.yaml")
    p_att.add_argument("--session-id", action="append", help="Specific session ID (repeatable)")
    p_att.add_argument("--limit", type=int, help="Limit number of sessions")
    p_att.add_argument("--missing-only", action="store_true", help="Only output missing attachments")
    p_att.add_argument("--output-json", help="Path to write JSON inventory")
    p_att.add_argument("--output-csv", help="Path to write CSV inventory")
    p_att.set_defaults(func=cmd_inventory_attachments)

    # 4. rebuild-index
    p_reb = subparsers.add_parser("rebuild-index", help="Rebuild SQLite FTS and vector index from Markdown")
    p_reb.add_argument("--config", help="Path to config.yaml")
    p_reb.add_argument("--dir", help="Directory containing Markdown files")
    p_reb.add_argument("--session-id", action="append", help="Specific session ID to index (repeatable)")
    p_reb.add_argument("--limit", type=int, help="Limit number of files")
    p_reb.add_argument("--confirm-rebuild", action="store_true", help="Confirmation flag")
    p_reb.add_argument("--dry-run", action="store_true", help="Estimate rebuild without modifying index or calling embeddings")
    p_reb.add_argument("--json-report", help="Path for JSON summary")
    p_reb.set_defaults(func=cmd_rebuild_index)

    # 5. index
    p_idx = subparsers.add_parser("index", help="Incrementally index Markdown files")
    p_idx.add_argument("--config", help="Path to config.yaml")
    p_idx.add_argument("--dir", help="Directory containing Markdown files")
    p_idx.add_argument("--session-id", action="append", help="Specific session ID to index (repeatable)")
    p_idx.add_argument("--limit", type=int, help="Limit number of files")
    p_idx.add_argument("--changed-only", action="store_true", default=True, help="Only index changed files")
    p_idx.add_argument("--dry-run", action="store_true", help="Simulate indexing")
    p_idx.add_argument("--json-report", help="Output path for JSON report")
    p_idx.set_defaults(func=cmd_index)

    # 6. search
    p_search = subparsers.add_parser("search", help="Search indexed conversations")
    p_search.add_argument("query", help="Search query string")
    p_search.add_argument("--config", help="Path to config.yaml")
    p_search.add_argument("--project", help="Filter by project")
    p_search.add_argument("--conversation-id", help="Filter by conversation ID")
    p_search.add_argument("--parent-thread-id", help="Filter by parent thread ID")
    p_search.add_argument("--source-system", help="Filter by source system")
    p_search.add_argument("--canonical-conversation-id", help="Filter by canonical conversation ID")
    p_search.add_argument("--source-key", help="Filter by source key")
    p_search.add_argument("--source-conversation-id", help="Filter by source conversation ID")
    p_search.add_argument("--limit", type=int, default=10, help="Max results")
    p_search.add_argument("--json-report", help="Output path for JSON report")
    p_search.set_defaults(func=cmd_search)

    # 7. validate
    p_val = subparsers.add_parser("validate", help="Validate generated Markdown notes")
    p_val.add_argument("--config", help="Path to config.yaml")
    p_val.add_argument("--staging-dir", help="Path to staging notes folder")
    p_val.add_argument("--json-report", help="Output path for JSON report")
    p_val.set_defaults(func=cmd_validate)

    # 8. dedup-audit (v0.2.4)
    p_audit2 = subparsers.add_parser("dedup-audit", help="Read-only identity/dedup consistency audit")
    p_audit2.add_argument("--config", help="Path to config.yaml")
    p_audit2.add_argument("--dir", help="Conversation root containing .ai-memory/manifest.sqlite")
    p_audit2.add_argument("--json-report", help="Output path for JSON report")
    p_audit2.set_defaults(func=cmd_dedup_audit)

    # 9. dedup-list
    p_list = subparsers.add_parser("dedup-list", help="List duplicate review candidates")
    p_list.add_argument("--config", help="Path to config.yaml")
    p_list.add_argument("--dir", help="Conversation root containing .ai-memory/manifest.sqlite")
    p_list.add_argument("--status", choices=["pending", "confirmed_same", "rejected"], help="Filter by status")
    p_list.add_argument("--source-system", help="Filter by source system")
    p_list.add_argument("--candidate-type", help="Filter by candidate type")
    p_list.add_argument("--json-report", help="Output path for JSON report")
    p_list.set_defaults(func=cmd_dedup_list)

    # 10. dedup-show
    p_show = subparsers.add_parser("dedup-show", help="Show one duplicate candidate in detail")
    p_show.add_argument("candidate_id", type=int, help="Candidate id")
    p_show.add_argument("--config", help="Path to config.yaml")
    p_show.add_argument("--dir", help="Conversation root containing .ai-memory/manifest.sqlite")
    p_show.add_argument("--json-report", help="Output path for JSON report")
    p_show.set_defaults(func=cmd_dedup_show)

    # 11. dedup-resolve
    p_resolve = subparsers.add_parser("dedup-resolve", help="Confirm or reject a duplicate candidate")
    p_resolve.add_argument("candidate_id", type=int, help="Candidate id")
    resolve_group = p_resolve.add_mutually_exclusive_group(required=True)
    resolve_group.add_argument("--confirm-same", action="store_true", help="Explicitly confirm the pair is the same conversation")
    resolve_group.add_argument("--reject", action="store_true", help="Explicitly reject the pair (records stay independent)")
    p_resolve.add_argument("--winner-source-key", default="", help="With --confirm-same: which source keeps the active canonical (default: first seen)")
    p_resolve.add_argument("--note", default="", help="Optional review note stored with the decision")
    p_resolve.add_argument("--config", help="Path to config.yaml")
    p_resolve.add_argument("--dir", help="Conversation root containing .ai-memory/manifest.sqlite")
    p_resolve.add_argument("--json-report", help="Output path for JSON report")
    p_resolve.set_defaults(func=cmd_dedup_resolve)

    # 12. identity-show
    p_ident = subparsers.add_parser("identity-show", help="Show source/canonical identity for a conversation")
    p_ident.add_argument("conversation_or_source_key", help="Bare conversation id or srcv1_ source key")
    p_ident.add_argument("--config", help="Path to config.yaml")
    p_ident.add_argument("--dir", help="Conversation root containing .ai-memory/manifest.sqlite")
    p_ident.add_argument("--json-report", help="Output path for JSON report")
    p_ident.set_defaults(func=cmd_identity_show)

    # 13. migrate-identity
    p_migrate = subparsers.add_parser("migrate-identity", help="Run additive legacy -> v2 identity migration")
    p_migrate.add_argument("--config", help="Path to config.yaml")
    p_migrate.add_argument("--dir", help="Conversation root containing .ai-memory/manifest.sqlite")
    p_migrate.set_defaults(func=cmd_migrate_identity)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
