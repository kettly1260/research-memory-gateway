from __future__ import annotations

import json
from pathlib import Path

from research_memory_gateway.conversations import (
    CodexExportReader,
    ImportManifest,
    ObsidianConversationWriter,
)
from research_memory_gateway.conversations.rehearsal import RehearsalOptions, run_rehearsal
from tests.test_conversation_ingestion import make_export


def _legacy_fixture(tmp_path: Path) -> tuple[Path, Path, Path, str]:
    export_dir = tmp_path / "export"
    export_dir.mkdir()
    archive, conversation_id = make_export(export_dir, title="Rehearsal Source")

    source_root = tmp_path / "source"
    manifest = ImportManifest(source_root / ".ai-memory" / "manifest.sqlite")
    writer = ObsidianConversationWriter(source_root)
    reader = CodexExportReader(archive)
    ref = reader.get_session_ref(conversation_id)
    note = writer.write(reader.parse(conversation_id))
    manifest.record(
        ref,
        archive_path=str(archive),
        archive_sha256=reader.archive_sha256,
        output_path=str(note),
        status="written",
    )

    config = tmp_path / "config.yaml"
    config.write_text("sources:\n  allowlist: []\n", encoding="utf-8")
    return source_root, archive, config, conversation_id


def test_formal_rehearsal_is_repeatable_and_uses_isolated_workdirs(tmp_path: Path) -> None:
    source_root, archive, config, conversation_id = _legacy_fixture(tmp_path)
    source_note_hashes = {
        path.relative_to(source_root).as_posix(): path.read_bytes()
        for path in source_root.rglob("*.md")
    }
    archive_before = archive.read_bytes()

    reports = []
    for ordinal in (1, 2):
        report_path = tmp_path / f"report-{ordinal}.json"
        report = run_rehearsal(
            RehearsalOptions(
                source_root=source_root,
                archive_path=archive,
                config_path=config,
                report_path=report_path,
                expected_count=1,
                work_parent=tmp_path / "work",
                continuation_session_id=conversation_id,
            )
        )
        reports.append(report)
        persisted = json.loads(report_path.read_text(encoding="utf-8"))
        assert persisted["overall"] == "PASS"
        assert persisted["steps"]["repeat_import_after_reconcile"]["skipped"] == 1
        assert persisted["steps"]["continuation"]["output_path_unchanged"] is True
        assert persisted["steps"]["stale_replay"]["reason"] == "stale_snapshot"
        assert persisted["steps"]["raw_archive_immutable"]["PASS"] is True
        assert persisted["workdir_preserved"] is False
        assert not Path(persisted["workdir"]).exists()

    assert reports[0]["workdir"] != reports[1]["workdir"]
    assert archive.read_bytes() == archive_before
    assert {
        path.relative_to(source_root).as_posix(): path.read_bytes()
        for path in source_root.rglob("*.md")
    } == source_note_hashes


def test_rehearsal_keep_workdir_preserves_isolated_artifacts(tmp_path: Path) -> None:
    source_root, archive, config, conversation_id = _legacy_fixture(tmp_path)
    report_path = tmp_path / "report.json"
    report = run_rehearsal(
        RehearsalOptions(
            source_root=source_root,
            archive_path=archive,
            config_path=config,
            report_path=report_path,
            expected_count=1,
            work_parent=tmp_path / "work",
            keep_workdir=True,
            continuation_session_id=conversation_id,
        )
    )

    assert report["overall"] == "PASS"
    assert report["workdir_preserved"] is True
    workdir = Path(report["workdir"])
    assert workdir.is_dir()
    assert (workdir / "archive-copy" / ".ai-memory" / "manifest.sqlite").is_file()
    assert (workdir / "rehearsal-index.sqlite").is_file()
    assert (workdir / "continuation.zip").is_file()
