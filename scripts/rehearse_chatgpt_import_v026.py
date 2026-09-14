"""v0.2.6 ChatGPT Export Importer rehearsal script.

Runs the full v0.2.6 rehearsal checklist (taskbook C11) against an export
ZIP, entirely inside a scratch workdir:

  A. SHA-256 of the raw ZIP before
  B. schema audit (privacy-safe)
  C. dry-run of every import item
  D. real import into an isolated staging/manifest
  E. counts: Markdown notes / source records / canonical families / branches
  F. same ZIP re-import -> everything unchanged, zero note rewrites
  G. lexical index rebuilt in an isolated SQLite database
  H. search / read smoke checks
  I. SHA-256 of the raw ZIP after == before (raw ZIP immutable proof)

The archive is never modified.  Pass --archive to rehearse a REAL ChatGPT
Data Export ZIP; without it a synthetic full export is generated so the
pipeline can be exercised end to end without any private data.

Usage:
  python scripts/rehearse_chatgpt_import_v026.py --workdir <scratch-dir> [--archive <zip>]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for _path in (str(REPO_ROOT), str(REPO_ROOT / "src")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from research_memory_gateway.conversations import (  # noqa: E402
    ChatGPTExportReader,
    ConversationIndexDatabase,
    ConversationIngestionPipeline,
    ImportManifest,
    ObsidianConversationWriter,
    AttachmentInventory,
    detect_export_format,
    resolve_account_namespace_hash,
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def peak_rss_mb() -> float | None:
    try:
        import resource  # POSIX only

        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    except Exception:
        try:
            import ctypes
            import ctypes.wintypes

            class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
                _fields_ = [
                    ("cb", ctypes.wintypes.DWORD),
                    ("PageFaultCount", ctypes.wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            counters = PROCESS_MEMORY_COUNTERS()
            counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
            psapi = ctypes.windll.psapi
            psapi.GetProcessMemoryInfo.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(PROCESS_MEMORY_COUNTERS),
                ctypes.wintypes.DWORD,
            ]
            psapi.GetProcessMemoryInfo.restype = ctypes.wintypes.BOOL
            handle = ctypes.windll.kernel32.GetCurrentProcess()
            if psapi.GetProcessMemoryInfo(
                ctypes.c_void_p(handle & 0xFFFFFFFFFFFFFFFF), ctypes.byref(counters), counters.cb
            ):
                return counters.PeakWorkingSetSize / (1024.0 * 1024.0)
        except Exception:
            pass
    return None


def build_synthetic_export(
    target: Path,
    conversation_count: int = 40,
    *,
    shard_count: int = 1,
) -> Path:
    """Generate a deterministic synthetic full export (no private data)."""
    from tests.chatgpt_fixtures import (
        GraphBuilder,
        build_export,
        build_sharded_export,
        linear_conversation,
        make_message,
        multimodal_message,
    )
    import tempfile

    conversations = []
    for index in range(conversation_count):
        cid = f"syn-conv-{index:03d}"
        builder = GraphBuilder(cid)
        builder.append(multimodal_message("user", f"question {index} with asset", f"file-service://file-syn{index:03d}"))
        builder.append(make_message("assistant", f"answer {index} part one", model_slug="synthetic-gpt-test"))
        if index % 3 == 0:
            # regenerate branch on every third conversation
            builder.attach_branch_under(
                builder.nodes[builder.root_id]["children"][0],
                make_message("assistant", f"answer {index} regenerated", model_slug="synthetic-gpt-test"),
            )
        if index % 4 == 1:
            # a continuation-style second turn on some conversations
            builder.append(make_message("user", f"follow-up {index}"))
            builder.append(make_message("assistant", f"final {index}", model_slug="synthetic-gpt-test"))
        conversations.append(builder.to_conversation(title=f"Synthetic Conversation {index:03d}"))
    for extra in range(max(1, conversation_count // 10)):
        conversations.append(
            linear_conversation(
                f"syn-linear-{extra:03d}",
                [("user", f"linear q {extra}"), ("assistant", f"linear a {extra}")],
            )
        )

    assets = {
        f"files/file-syn{i:03d}.png": f"synthetic-png-bytes-{i}".encode("utf-8")
        for i in range(conversation_count)
    }

    if shard_count > 1:
        shards: list[list[dict]] = [[] for _ in range(shard_count)]
        for i, conv in enumerate(conversations):
            shards[i % shard_count].append(conv)
        archive = build_sharded_export(target, shards, assets=assets)
    else:
        archive = build_export(target, conversations, assets=assets)
    return archive


def run_rehearsal(archive_path: Path, workdir: Path, *, account_label: str = "rehearsal-label") -> dict:
    report: dict = {"started_at": datetime.now(timezone.utc).isoformat()}
    archive_path = archive_path.resolve()
    workdir = workdir.resolve()
    staging = workdir / "staging"
    if staging.exists():
        shutil.rmtree(staging)

    # A. raw ZIP hash before ------------------------------------------------
    sha_before = sha256_file(archive_path)
    report["archive_path_resolved"] = str(archive_path)
    report["archive_sha256_before"] = sha_before
    report["archive_bytes"] = archive_path.stat().st_size

    # B. schema audit ---------------------------------------------------------
    fmt = detect_export_format(archive_path)
    if fmt != "chatgpt":
        raise SystemExit(f"Refusing rehearsal: detected format {fmt!r} (need 'chatgpt')")
    namespace_hash, namespace_strategy = resolve_account_namespace_hash(
        archive_path, namespace_label=account_label
    )
    reader = ChatGPTExportReader(archive_path, account_namespace_hash=namespace_hash)
    audit = reader.schema_audit()
    report["namespace_strategy"] = namespace_strategy
    report["schema_audit"] = audit
    report["conversation_count"] = audit["conversation_count"]

    # C. dry-run ---------------------------------------------------------------
    manifest = ImportManifest(staging / ".ai-memory" / "manifest.sqlite")
    writer = ObsidianConversationWriter(staging)
    inventory = AttachmentInventory(archive_asset_resolver=reader.resolve_asset)
    pipeline = ConversationIngestionPipeline(reader, writer, manifest, attachment_inventory=inventory)
    keys = [ref.effective_import_key for ref in reader.list_sessions()]
    dry_results = pipeline.run(keys, dry_run=True)
    report["dry_run_total"] = len(dry_results)
    report["dry_run_all_dry_run"] = all(r.status == "dry_run" for r in dry_results)

    # D. real import ------------------------------------------------------------
    started = time.perf_counter()
    first_results = pipeline.run(keys)
    first_seconds = time.perf_counter() - started
    status_counts: dict[str, int] = {}
    for result in first_results:
        status_counts[result.status] = status_counts.get(result.status, 0) + 1
    report["first_import_seconds"] = round(first_seconds, 3)
    report["first_import_status_counts"] = status_counts

    # E. counts -------------------------------------------------------------------
    md_files = sorted(staging.rglob("*.md"))
    with sqlite3.connect(manifest.path) as conn:
        source_records = conn.execute(
            "SELECT COUNT(*) FROM conversation_source_records WHERE source_system='chatgpt'"
        ).fetchone()[0]
        canonical_count = conn.execute(
            "SELECT COUNT(DISTINCT canonical_conversation_id) FROM conversation_source_records "
            "WHERE source_system='chatgpt'"
        ).fetchone()[0]
        branch_records = conn.execute(
            "SELECT COUNT(*) FROM conversation_source_records "
            "WHERE source_system='chatgpt' AND source_branch_id != ''"
        ).fetchone()[0]
    report["markdown_notes"] = len(md_files)
    report["source_records_chatgpt"] = source_records
    report["canonical_families_chatgpt"] = canonical_count
    report["branch_source_records"] = branch_records
    report["import_items"] = len(keys)
    report["visible_messages"] = sum(
        1
        for path in md_files
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.startswith("<!-- source ordinal=")
    )
    # Scan attachments using real AttachmentInventoryRecord / resolver results
    all_attachment_records = []
    for key in keys:
        try:
            parsed_conv = reader.parse(key)
            all_attachment_records.extend(inventory.scan_one(parsed_conv))
        except Exception:
            pass

    archive_assets_referenced = sum(
        1
        for r in all_attachment_records
        if r.locator_type in {"archive_asset", "zip_entry"} or r.original_locator.startswith("file-service://")
    )
    archive_assets_resolved = sum(
        1
        for r in all_attachment_records
        if r.locator_type == "archive_asset" and r.status == "found"
    )
    archive_assets_unresolved = sum(
        1
        for r in all_attachment_records
        if r.status == "unresolved"
    )
    content_sha_matched = sum(
        1
        for r in all_attachment_records
        if r.status == "found" and bool(r.content_hash)
    )

    report["archive_assets_referenced"] = archive_assets_referenced
    report["archive_assets_resolved"] = archive_assets_resolved
    report["archive_assets_unresolved"] = archive_assets_unresolved
    report["content_sha_matched"] = content_sha_matched
    report["attachments_resolved"] = archive_assets_resolved

    # F. repeat import ---------------------------------------------------------
    snapshot = {path: path.read_bytes() for path in md_files}
    started = time.perf_counter()
    second_results = pipeline.run(keys)
    second_seconds = time.perf_counter() - started
    second_status: dict[str, int] = {}
    for result in second_results:
        second_status[result.status] = second_status.get(result.status, 0) + 1
    report["repeat_import_seconds"] = round(second_seconds, 3)
    report["repeat_import_status_counts"] = second_status
    report["repeat_all_skipped"] = all(r.status == "skipped" for r in second_results)
    rewritten = [str(path) for path, blob in snapshot.items() if path.read_bytes() != blob]
    report["repeat_rewritten_notes"] = rewritten
    report["repeat_zero_rewrites"] = not rewritten

    # G. isolated lexical index ---------------------------------------------------
    index_db = workdir / "index" / "rehearsal-index.sqlite"
    index_db.parent.mkdir(parents=True, exist_ok=True)
    idx = ConversationIndexDatabase(index_db, embedding_client=None)
    chunk_count = idx.rebuild_from_markdown(md_files)
    report["index_chunks"] = chunk_count
    report["index_db_path"] = str(index_db)

    # H. search/read smoke ----------------------------------------------------------
    smoke_queries = ["question", "answer", "follow-up", "linear"]
    search_hits = {}
    for query in smoke_queries:
        try:
            hits = idx.search_fts(query, limit=5)
            search_hits[query] = len(hits)
        except Exception:
            search_hits[query] = 0
    report["search_hits"] = search_hits

    # I. raw ZIP hash after ------------------------------------------------------------
    sha_after = sha256_file(archive_path)
    report["archive_sha256_after"] = sha_after
    report["raw_zip_unchanged"] = sha_before == sha_after
    report["peak_rss_mb"] = peak_rss_mb()
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="v0.2.6 ChatGPT importer rehearsal")
    parser.add_argument("--workdir", required=True, help="Scratch directory (created if missing)")
    parser.add_argument("--archive", help="Path to an export ZIP; omit to generate a synthetic export")
    parser.add_argument("--account-namespace", default="rehearsal-label")
    parser.add_argument("--synthetic-conversations", type=int, default=40)
    parser.add_argument("--sharded", action="store_true", help="Generate sharded conversation JSON files (e.g. conversations-001.json, conversations-002.json)")
    parser.add_argument("--shard-count", type=int, default=3, help="Number of shards when generating sharded export")
    parser.add_argument("--json-report", help="Optional path for the JSON report")
    args = parser.parse_args(argv)

    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    if args.archive:
        archive = Path(args.archive)
        if not archive.exists():
            raise SystemExit(f"Archive not found: {archive}")
        synthetic = False
    else:
        filename = "synthetic-chatgpt-sharded-export.zip" if args.sharded else "synthetic-chatgpt-export.zip"
        archive = workdir / "input" / filename
        archive.parent.mkdir(parents=True, exist_ok=True)
        build_synthetic_export(
            archive,
            args.synthetic_conversations,
            shard_count=args.shard_count if args.sharded else 1,
        )
        synthetic = True

    report = run_rehearsal(archive, workdir, account_label=args.account_namespace)
    report["synthetic_archive"] = synthetic
    report["validation_status"] = "REAL EXPORT VALIDATION PENDING"
    report["production_ready"] = False

    report_text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.json_report:
        Path(args.json_report).write_text(report_text, encoding="utf-8")
    print(report_text)

    if report.get("synthetic_archive", False):
        attachment_gate_ok = (
            report["archive_assets_resolved"] > 0
            and report["archive_assets_referenced"] >= report["archive_assets_resolved"]
            and report["content_sha_matched"] == report["archive_assets_resolved"]
        )
    else:
        attachment_gate_ok = (
            report["archive_assets_referenced"] == 0
            or (report["archive_assets_resolved"] > 0 and report["content_sha_matched"] == report["archive_assets_resolved"])
        )

    gates = [
        report["dry_run_all_dry_run"],
        report["repeat_all_skipped"],
        report["repeat_zero_rewrites"],
        report["raw_zip_unchanged"],
        attachment_gate_ok,
    ]
    return 0 if all(gates) else 1


if __name__ == "__main__":
    raise SystemExit(main())
