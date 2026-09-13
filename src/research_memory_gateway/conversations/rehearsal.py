from __future__ import annotations

import argparse
import gc
import hashlib
import json
import shutil
import sqlite3
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from ..config import load_config
from .attachments import AttachmentInventory
from .codex_export import CodexExportReader
from .identity_store import ConversationIdentityStore, load_legacy_import_rows
from .index import ConversationIndexDatabase
from .manifest import ImportManifest
from .pipeline import ConversationIngestionPipeline
from .vault_writer import ObsidianConversationWriter


class RehearsalError(RuntimeError):
    """Raised when a migration rehearsal precondition is unsafe or invalid."""


@dataclass(frozen=True)
class RehearsalOptions:
    source_root: Path
    archive_path: Path
    config_path: Path
    report_path: Path
    expected_count: int | None = None
    work_parent: Path | None = None
    keep_workdir: bool = False
    continuation_session_id: str = ""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _note_hashes(root: Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for path in root.rglob("*.md"):
        rel = path.relative_to(root).as_posix()
        hashes[rel] = _sha256_file(path)
    return hashes


def _sqlite_backup(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    uri = f"file:{source.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as src, sqlite3.connect(destination) as dst:
        src.backup(dst)


def _prepare_copy(source_root: Path, work_root: Path) -> Path:
    """Copy only Markdown plus the archive-local manifest into an isolated root."""
    if work_root.exists():
        raise RehearsalError(f"work root already exists: {work_root}")
    source_root = source_root.resolve()
    work_root.mkdir(parents=True)
    for note in source_root.rglob("*.md"):
        relative = note.relative_to(source_root)
        target = work_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(note, target)

    source_manifest = source_root / ".ai-memory" / "manifest.sqlite"
    if not source_manifest.is_file():
        raise RehearsalError(f"archive-local manifest not found: {source_manifest}")
    target_manifest = work_root / ".ai-memory" / "manifest.sqlite"
    _sqlite_backup(source_manifest, target_manifest)
    return target_manifest


def _retarget_manifest(
    manifest_path: Path,
    *,
    source_root: Path,
    work_root: Path,
    archive_path: Path,
) -> int:
    """Retarget legacy output paths inside the copy, never in the source manifest."""
    source_root = source_root.resolve()
    work_root = work_root.resolve()
    with sqlite3.connect(manifest_path) as connection:
        rows = connection.execute(
            "SELECT conversation_id, output_path FROM conversation_imports"
        ).fetchall()
        if not rows:
            raise RehearsalError("conversation_imports is empty; nothing to rehearse")
        for conversation_id, output_path in rows:
            if not output_path:
                raise RehearsalError(f"legacy output_path is blank for {conversation_id}")
            try:
                relative = Path(output_path).resolve().relative_to(source_root)
            except ValueError as exc:
                raise RehearsalError(
                    f"manifest output path is outside source root: {output_path}"
                ) from exc
            target = (work_root / relative).resolve()
            connection.execute(
                "UPDATE conversation_imports SET output_path = ?, source_archive_path = ? "
                "WHERE conversation_id = ?",
                (str(target), str(archive_path.resolve()), conversation_id),
            )
        return len(rows)


def _count_results(results: Sequence[Any]) -> dict[str, int]:
    return {
        "written": sum(1 for item in results if item.status == "written"),
        "skipped": sum(1 for item in results if item.status == "skipped"),
        "unchanged": sum(1 for item in results if item.reason == "unchanged"),
        "conflict": sum(1 for item in results if item.status == "conflict"),
        "failed_retryable": sum(1 for item in results if item.status == "failed_retryable"),
        "index_stale": sum(1 for item in results if item.status == "index_stale"),
    }


def _build_continuation_zip(
    reader: CodexExportReader,
    session_id: str,
    out_path: Path,
) -> Path:
    ref = reader.get_session_ref(session_id)
    with zipfile.ZipFile(reader.archive_path) as archive:
        raw = archive.read(ref.source_entry).decode("utf-8")
        manifest = json.loads(archive.read("manifest.json"))

    lines = [line for line in raw.splitlines() if line.strip()]
    next_ordinal = len(lines) + 10
    continuation_lines = [
        json.dumps(
            {
                "timestamp": "2099-01-01T00:00:00Z",
                "ordinal": next_ordinal,
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "id": "msg-rehearsal-user",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "CONTINUATION MARKER rehearsal question"}
                    ],
                    "internal_chat_message_metadata_passthrough": {
                        "turn_id": "turn-rehearsal"
                    },
                },
            },
            ensure_ascii=False,
        ),
        json.dumps(
            {
                "timestamp": "2099-01-01T00:00:30Z",
                "ordinal": next_ordinal + 1,
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "id": "msg-rehearsal-assistant",
                    "role": "assistant",
                    "phase": "final_answer",
                    "content": [
                        {"type": "output_text", "text": "CONTINUATION MARKER rehearsal answer"}
                    ],
                    "internal_chat_message_metadata_passthrough": {
                        "turn_id": "turn-rehearsal"
                    },
                },
            },
            ensure_ascii=False,
        ),
    ]
    new_raw = ("\n".join([*lines, *continuation_lines]) + "\n").encode("utf-8")

    sessions = [
        item for item in manifest.get("sessions", []) if item.get("sessionId") == session_id
    ]
    if len(sessions) != 1:
        raise RehearsalError(
            f"expected exactly one manifest entry for continuation session {session_id}"
        )
    sessions[0]["title"] = "Rehearsal Continuation Title"
    sessions[0]["sha256"] = hashlib.sha256(new_raw).hexdigest()
    sessions[0]["sizeBytes"] = len(new_raw)
    manifest["exportedAt"] = "2099-01-01T00:00:00Z"
    manifest["sessions"] = sessions

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
        archive.writestr(ref.source_entry, new_raw)
    return out_path


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def _cleanup_workdir(path: Path) -> str:
    """Remove an isolated workdir, tolerating delayed SQLite handle release on Windows."""
    last_error = ""
    for _ in range(5):
        gc.collect()
        try:
            shutil.rmtree(path)
            return ""
        except FileNotFoundError:
            return ""
        except OSError as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(0.1)
    return last_error or "unknown cleanup failure"


def run_rehearsal(options: RehearsalOptions) -> dict[str, Any]:
    source_root = options.source_root.resolve()
    archive_path = options.archive_path.resolve()
    config_path = options.config_path.resolve()
    report_path = options.report_path.resolve()
    if not source_root.is_dir():
        raise RehearsalError(f"source root does not exist: {source_root}")
    if not archive_path.is_file():
        raise RehearsalError(f"raw archive does not exist: {archive_path}")
    if not config_path.is_file():
        raise RehearsalError(f"config does not exist: {config_path}")

    work_parent = options.work_parent.resolve() if options.work_parent else None
    if work_parent is not None:
        work_parent.mkdir(parents=True, exist_ok=True)
    work_dir = Path(
        tempfile.mkdtemp(prefix="rmg-conversation-rehearsal-", dir=str(work_parent) if work_parent else None)
    ).resolve()
    copy_root = work_dir / "archive-copy"
    index_path = work_dir / "rehearsal-index.sqlite"
    continuation_zip = work_dir / "continuation.zip"

    archive_sha_before = _sha256_file(archive_path)
    report: dict[str, Any] = {
        "schema": "rmg-conversation-migration-rehearsal-v1",
        "overall": "FAIL",
        "inputs": {
            "source_root": str(source_root),
            "archive_path": str(archive_path),
            "archive_sha256": archive_sha_before,
            "expected_count": options.expected_count,
        },
        "workdir": str(work_dir),
        "steps": {},
    }
    success = False
    try:
        manifest_path = _prepare_copy(source_root, copy_root)
        legacy_count = _retarget_manifest(
            manifest_path,
            source_root=source_root,
            work_root=copy_root,
            archive_path=archive_path,
        )
        expected = options.expected_count if options.expected_count is not None else legacy_count
        if legacy_count != expected:
            raise RehearsalError(
                f"legacy manifest count mismatch: expected {expected}, saw {legacy_count}"
            )
        before_hashes = _note_hashes(copy_root)
        if len(before_hashes) != expected:
            raise RehearsalError(
                f"Markdown count mismatch: expected {expected}, saw {len(before_hashes)}"
            )
        report["steps"]["copy_prep"] = {
            "legacy_records": legacy_count,
            "markdown_notes": len(before_hashes),
            "PASS": True,
        }

        store = ConversationIdentityStore(manifest_path)
        migration = store.migrate_legacy_imports(load_legacy_import_rows(manifest_path))
        stats = store.identity_stats()
        after_migration_hashes = _note_hashes(copy_root)
        changed_notes = [
            path for path, digest in before_hashes.items()
            if after_migration_hashes.get(path) != digest
        ]
        migration_ok = (
            migration.legacy_records == expected
            and stats["source_records"] == expected
            and stats["canonical_conversations"] == expected
            and migration.source_key_collisions == 0
            and migration.canonical_id_collisions == 0
            and migration.output_paths_changed == 0
            and not changed_notes
            and stats["pending_duplicate_candidates"] == 0
        )
        report["steps"]["migration"] = {
            "legacy_records": migration.legacy_records,
            "source_records": stats["source_records"],
            "canonical_conversations": stats["canonical_conversations"],
            "pending_duplicates": stats["pending_duplicate_candidates"],
            "source_key_collisions": migration.source_key_collisions,
            "canonical_id_collisions": migration.canonical_id_collisions,
            "output_paths_changed": migration.output_paths_changed,
            "markdown_files_changed": len(changed_notes),
            "PASS": migration_ok,
        }
        if not migration_ok:
            return report

        cfg = load_config(config_path)
        reader = CodexExportReader(archive_path)
        writer = ObsidianConversationWriter(copy_root)
        manifest = ImportManifest(manifest_path)
        pipeline = ConversationIngestionPipeline(
            reader,
            writer,
            manifest,
            attachment_inventory=AttachmentInventory(
                allowlist_roots=[entry.path for entry in cfg.sources.allowlist]
            ),
            identity_store=store,
        )
        session_ids = [ref.conversation_id for ref in reader.list_sessions()]
        if len(session_ids) != expected:
            raise RehearsalError(
                f"archive session count mismatch: expected {expected}, saw {len(session_ids)}"
            )

        first_results = pipeline.run(session_ids, resume=True)
        first_counts = _count_results(first_results)
        reason_counts: dict[str, int] = {}
        for item in first_results:
            reason_counts[item.reason] = reason_counts.get(item.reason, 0) + 1
        first_after = _note_hashes(copy_root)
        drifted_results = [
            item for item in first_results
            if item.status == "written" and item.reason == "attachment_changed"
        ]
        drifted_relpaths = {
            Path(item.output_path).resolve().relative_to(copy_root).as_posix()
            for item in drifted_results
        }
        unexpected_note_changes = [
            path for path, digest in after_migration_hashes.items()
            if first_after.get(path) != digest and path not in drifted_relpaths
        ]
        non_drift_writes = [
            item for item in first_results
            if item.status == "written" and item.reason != "attachment_changed"
        ]
        first_ok = (
            first_counts["skipped"] + first_counts["written"] == expected
            and first_counts["written"] == len(drifted_results)
            and first_counts["conflict"] == 0
            and first_counts["failed_retryable"] == 0
            and not non_drift_writes
            and not unexpected_note_changes
        )
        report["steps"]["repeat_import"] = {
            **first_counts,
            "reasons": reason_counts,
            "attachment_drift_sessions": sorted(item.conversation_id for item in drifted_results),
            "unexpected_notes_touched": len(unexpected_note_changes),
            "PASS": first_ok,
        }
        if not first_ok:
            return report

        index = ConversationIndexDatabase(index_path)
        for item in drifted_results:
            index.index_file(item.output_path)
            metadata = index.index_metadata_for_file(item.output_path)
            if metadata:
                manifest.record_indexing(
                    metadata.get("legacy_conversation_id") or metadata["conversation_id"],
                    index_source_hash=metadata["index_source_hash"],
                    last_indexed_at=metadata["last_indexed_at"],
                    content_section_hashes=metadata["content_section_hashes"],
                )
        report["steps"]["reconcile_index"] = {
            "reindexed_notes": len(drifted_results),
            "index_path": str(index_path),
            "PASS": True,
        }

        strict_before = _note_hashes(copy_root)
        strict_results = pipeline.run(session_ids, resume=True)
        strict_counts = _count_results(strict_results)
        strict_after = _note_hashes(copy_root)
        strict_changed = [
            path for path, digest in strict_before.items() if strict_after.get(path) != digest
        ]
        strict_ok = (
            strict_counts["skipped"] == expected
            and strict_counts["written"] == 0
            and strict_counts["conflict"] == 0
            and strict_counts["failed_retryable"] == 0
            and strict_counts["index_stale"] == 0
            and not strict_changed
        )
        report["steps"]["repeat_import_after_reconcile"] = {
            **strict_counts,
            "notes_touched": len(strict_changed),
            "strict_gate": f"{expected} skipped / 0 written / 0 conflict / 0 failed",
            "PASS": strict_ok,
        }
        if not strict_ok:
            return report

        target_id = options.continuation_session_id or session_ids[0]
        if target_id not in session_ids:
            raise RehearsalError(f"continuation session is not in archive: {target_id}")
        legacy_before = manifest.get_record(target_id)
        if not legacy_before or not legacy_before.get("output_path"):
            raise RehearsalError(f"missing legacy output path for continuation session: {target_id}")
        stable_path_before = Path(str(legacy_before["output_path"])).resolve()
        _build_continuation_zip(reader, target_id, continuation_zip)
        pipeline.reader = CodexExportReader(continuation_zip)
        continuation = pipeline.run([target_id], resume=True)[0]
        note_count = len(list(copy_root.rglob("*.md")))
        stable_path_after = Path(continuation.output_path).resolve()
        continuation_ok = (
            continuation.status == "written"
            and continuation.reason == "source_continued"
            and stable_path_after == stable_path_before
            and stable_path_after.is_file()
            and note_count == expected
        )
        report["steps"]["continuation"] = {
            "session": target_id,
            "status": continuation.status,
            "reason": continuation.reason,
            "output_path_unchanged": stable_path_after == stable_path_before,
            "note_count": note_count,
            "PASS": continuation_ok,
        }
        if not continuation_ok:
            return report

        pipeline.reader = reader
        stale = pipeline.run([target_id], resume=True)[0]
        note_text = stable_path_after.read_text(encoding="utf-8")
        stale_ok = (
            stale.status == "skipped"
            and stale.reason == "stale_snapshot"
            and "CONTINUATION MARKER" in note_text
        )
        report["steps"]["stale_replay"] = {
            "status": stale.status,
            "reason": stale.reason,
            "note_still_has_continuation": "CONTINUATION MARKER" in note_text,
            "PASS": stale_ok,
        }
        if not stale_ok:
            return report

        archive_sha_after = _sha256_file(archive_path)
        archive_ok = archive_sha_after == archive_sha_before
        report["steps"]["raw_archive_immutable"] = {
            "sha256_before": archive_sha_before,
            "sha256_after": archive_sha_after,
            "PASS": archive_ok,
        }
        if not archive_ok:
            return report

        report["overall"] = "PASS"
        success = True
        return report
    finally:
        cleanup_error = ""
        if success and not options.keep_workdir:
            cleanup_error = _cleanup_workdir(work_dir)
        report["workdir_preserved"] = bool(
            options.keep_workdir or not success or cleanup_error
        )
        if cleanup_error:
            report["cleanup_error"] = cleanup_error
        _write_report(report_path, report)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Rehearse Conversation Memory legacy->multi-source identity migration on an "
            "isolated copy. The source archive, source Markdown and production manifest are read-only."
        )
    )
    parser.add_argument("--source-root", required=True, type=Path, help="Pristine conversation archive root")
    parser.add_argument("--archive", required=True, type=Path, help="Immutable Codex export ZIP")
    parser.add_argument("--config", required=True, type=Path, help="Gateway config used for attachment allowlist")
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("conversation-migration-rehearsal-report.json"),
        help="JSON report path (default: ./conversation-migration-rehearsal-report.json)",
    )
    parser.add_argument("--expected-count", type=int, default=None, help="Expected legacy/session/note count")
    parser.add_argument("--work-parent", type=Path, default=None, help="Parent directory for unique temporary workdir")
    parser.add_argument("--keep-workdir", action="store_true", help="Keep isolated workdir after a successful rehearsal")
    parser.add_argument(
        "--continuation-session-id",
        default="",
        help="Session used for synthetic continuation/stale replay (default: first archive session)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    options = RehearsalOptions(
        source_root=args.source_root,
        archive_path=args.archive,
        config_path=args.config,
        report_path=args.report,
        expected_count=args.expected_count,
        work_parent=args.work_parent,
        keep_workdir=args.keep_workdir,
        continuation_session_id=args.continuation_session_id,
    )
    try:
        report = run_rehearsal(options)
    except Exception as exc:
        payload = {"overall": "ERROR", "error": f"{type(exc).__name__}: {exc}"}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("overall") == "PASS" else 1


__all__ = [
    "RehearsalError",
    "RehearsalOptions",
    "build_parser",
    "main",
    "run_rehearsal",
]
