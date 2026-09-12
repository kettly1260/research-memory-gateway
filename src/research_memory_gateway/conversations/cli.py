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
    ImportManifest(manifest_path).record_indexing(
        metadata["conversation_id"],
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
    record = ImportManifest(manifest_path).get_record(metadata["conversation_id"])
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
    p_search.add_argument("--limit", type=int, default=10, help="Max results")
    p_search.add_argument("--json-report", help="Output path for JSON report")
    p_search.set_defaults(func=cmd_search)

    # 7. validate
    p_val = subparsers.add_parser("validate", help="Validate generated Markdown notes")
    p_val.add_argument("--config", help="Path to config.yaml")
    p_val.add_argument("--staging-dir", help="Path to staging notes folder")
    p_val.add_argument("--json-report", help="Output path for JSON report")
    p_val.set_defaults(func=cmd_validate)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
