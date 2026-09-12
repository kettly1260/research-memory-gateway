from __future__ import annotations

import json
import hashlib
import sqlite3
import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).parent))
from research_memory_gateway.conversations.cli import main
from research_memory_gateway.conversations.manifest import ImportManifest
from test_conversation_ingestion import make_export


def test_cli_audit_and_import(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    archive_path, conversation_id = make_export(tmp_path)
    report_json = tmp_path / "audit.json"

    # 1. audit-export
    ret = main(["audit-export", str(archive_path), "--json-report", str(report_json)])
    assert ret == 0
    assert report_json.exists()
    audit_data = json.loads(report_json.read_text(encoding="utf-8"))
    assert audit_data["total_sessions"] == 1
    assert "archive_sha256" in audit_data

    # 2. import --dry-run
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        f"""
conversation_archive:
  staging_dir: "{(tmp_path / 'staging').as_posix()}"
  index_path: "{(tmp_path / 'idx.sqlite').as_posix()}"
""",
        encoding="utf-8",
    )
    ret_dry = main(["import", str(archive_path), "--config", str(cfg_file), "--dry-run"])
    assert ret_dry == 0
    captured = capsys.readouterr()
    assert "dry_run" in captured.out

    # 3. inventory-attachments
    inv_json = tmp_path / "attachments.json"
    ret_att = main(["inventory-attachments", str(archive_path), "--config", str(cfg_file), "--output-json", str(inv_json)])
    assert ret_att == 0
    assert inv_json.exists()

    # 4. import actual
    ret_import = main(["import", str(archive_path), "--config", str(cfg_file)])
    assert ret_import == 0

    # 5. validate
    staging_dir = tmp_path / "staging"
    ret_val = main(["validate", "--staging-dir", str(staging_dir)])
    assert ret_val == 0

    # 6. rebuild-index guard
    ret_fail = main(["rebuild-index", "--config", str(cfg_file)])
    assert ret_fail == 1

    ret_ok = main(["rebuild-index", "--config", str(cfg_file), "--confirm-rebuild"])
    assert ret_ok == 0


def test_cli_vault_confirmation_and_canonical_subdir(tmp_path: Path) -> None:
    archive_path, _ = make_export(tmp_path)
    vault_dir = tmp_path / "real_vault"
    vault_dir.mkdir()

    cfg_file = tmp_path / "config_vault.yaml"
    cfg_file.write_text(
        f"""
conversation_archive:
  vault_root: "{vault_dir.as_posix()}"
  canonical_subdir: "90_System/AI-Memory"
  require_explicit_vault_confirmation: true
""",
        encoding="utf-8",
    )

    # 1. 尝试 --vault 但无 --confirm-vault 应失败 (ret == 1)
    ret_no_confirm = main(["import", str(archive_path), "--config", str(cfg_file), "--vault"])
    assert ret_no_confirm == 1

    # 2. 携带 --confirm-vault 应成功，并写入 canonical_subdir/Conversations/
    ret_confirm = main(["import", str(archive_path), "--config", str(cfg_file), "--vault", "--confirm-vault"])
    assert ret_confirm == 0

    expected_dir = vault_dir / "90_System" / "AI-Memory" / "Conversations"
    assert expected_dir.exists()
    assert len(list(expected_dir.rglob("*.md"))) == 1


def test_cli_changed_only_json_reports_and_search(tmp_path: Path) -> None:
    archive_path, conversation_id = make_export(tmp_path)
    staging_dir = tmp_path / "staging_p8"
    index_db = tmp_path / "idx_p8.sqlite"
    cfg_file = tmp_path / "config_p8.yaml"
    cfg_file.write_text(
        f"""
conversation_archive:
  staging_dir: "{staging_dir.as_posix()}"
  index_path: "{index_db.as_posix()}"
""",
        encoding="utf-8",
    )

    # 1. 导入并生成 JSON report，包含 resume 和 counts
    import_report = tmp_path / "import_rep.json"
    ret = main([
        "import", str(archive_path),
        "--config", str(cfg_file),
        "--staging-dir", str(staging_dir),
        "--resume",
        "--json-report", str(import_report),
    ])
    assert ret == 0
    imp_data = json.loads(import_report.read_text(encoding="utf-8"))
    assert imp_data["resume"] is True
    assert imp_data["counts"]["written"] == 1
    assert imp_data["counts"]["skipped"] == 0

    # 再次 import --resume：应跳过 unchanged
    import_report_2 = tmp_path / "import_rep_2.json"
    ret2 = main([
        "import", str(archive_path),
        "--config", str(cfg_file),
        "--staging-dir", str(staging_dir),
        "--resume",
        "--json-report", str(import_report_2),
    ])
    assert ret2 == 0
    imp_data_2 = json.loads(import_report_2.read_text(encoding="utf-8"))
    assert imp_data_2["counts"]["written"] == 0
    assert imp_data_2["counts"]["skipped"] == 1

    # 2. 索引测试：首次建索引
    index_report_1 = tmp_path / "index_rep_1.json"
    ret_idx1 = main([
        "index",
        "--config", str(cfg_file),
        "--dir", str(staging_dir),
        "--changed-only",
        "--json-report", str(index_report_1),
    ])
    assert ret_idx1 == 0
    idx_data_1 = json.loads(index_report_1.read_text(encoding="utf-8"))
    assert idx_data_1["indexed_files"] == 1
    assert idx_data_1["skipped_unchanged"] == 0
    assert idx_data_1["chunks_indexed"] > 0
    assert idx_data_1["change_reasons"]["new_document"] == 1

    manifest_row = ImportManifest(staging_dir / ".ai-memory" / "manifest.sqlite").get_record(conversation_id)
    assert manifest_row is not None
    assert manifest_row["last_indexed_at"]
    assert manifest_row["index_source_hash"]
    assert manifest_row["content_section_hashes"]

    # 3. 再次运行 index --changed-only：必须跳过未变文件并计入 skipped_unchanged
    index_report_2 = tmp_path / "index_rep_2.json"
    ret_idx2 = main([
        "index",
        "--config", str(cfg_file),
        "--dir", str(staging_dir),
        "--changed-only",
        "--json-report", str(index_report_2),
    ])
    assert ret_idx2 == 0
    idx_data_2 = json.loads(index_report_2.read_text(encoding="utf-8"))
    assert idx_data_2["indexed_files"] == 0
    assert idx_data_2["skipped_unchanged"] == 1
    assert idx_data_2["chunks_indexed"] == 0
    assert idx_data_2["change_reasons"]["unchanged"] == 1

    with sqlite3.connect(index_db) as conn:
        row = conn.execute(
            "SELECT skipped_files FROM conversation_index_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row == (1,)

    # 4. search 携带 --json-report
    search_report = tmp_path / "search_rep.json"
    ret_search = main([
        "search", "324 nm",
        "--config", str(cfg_file),
        "--json-report", str(search_report),
    ])
    assert ret_search == 0
    search_data = json.loads(search_report.read_text(encoding="utf-8"))
    assert search_data["count"] > 0
    assert any("324 nm" in r["content"] for r in search_data["results"])

    # 5. validate 携带 --json-report
    val_report = tmp_path / "val_rep.json"
    ret_val = main([
        "validate",
        "--staging-dir", str(staging_dir),
        "--json-report", str(val_report),
    ])
    assert ret_val == 0
    val_data = json.loads(val_report.read_text(encoding="utf-8"))
    assert val_data["valid_count"] == 1
    assert len(val_data["errors"]) == 0


def test_cli_staging_path_safety_and_resume_semantics(tmp_path: Path) -> None:
    archive_path, conversation_id = make_export(tmp_path)
    staging_root = tmp_path / "staging-root"
    staging_root.mkdir()
    vault_root = staging_root / "vault"
    vault_root.mkdir()
    canonical = vault_root / "90_System" / "AI-Memory"
    canonical.mkdir(parents=True)
    cfg_file = tmp_path / "config_paths.yaml"
    cfg_file.write_text(
        f"""
conversation_archive:
  staging_dir: "{staging_root.as_posix()}"
  vault_root: "{vault_root.as_posix()}"
  canonical_subdir: "90_System/AI-Memory"
  require_explicit_vault_confirmation: true
  index_path: "{(tmp_path / 'safe-index.sqlite').as_posix()}"
""",
        encoding="utf-8",
    )

    outside = tmp_path / "outside"
    assert main(["import", str(archive_path), "--config", str(cfg_file), "--staging-dir", str(outside)]) == 1
    assert main(["import", str(archive_path), "--config", str(cfg_file), "--staging-dir", str(staging_root / ".." / "outside")]) == 1
    assert main(["import", str(archive_path), "--config", str(cfg_file), "--staging-dir", str(vault_root)]) == 1
    assert main(["import", str(archive_path), "--config", str(cfg_file), "--staging-dir", str(canonical)]) == 1
    assert not outside.exists()

    safe_target = staging_root / "safe"
    report_1 = tmp_path / "resume-default.json"
    assert main([
        "import", str(archive_path), "--config", str(cfg_file),
        "--staging-dir", str(safe_target), "--json-report", str(report_1),
    ]) == 0
    assert json.loads(report_1.read_text(encoding="utf-8"))["resume"] is True

    report_2 = tmp_path / "no-resume.json"
    assert main([
        "import", str(archive_path), "--config", str(cfg_file),
        "--staging-dir", str(safe_target), "--no-resume", "--json-report", str(report_2),
    ]) == 0
    data_2 = json.loads(report_2.read_text(encoding="utf-8"))
    assert data_2["resume"] is False
    assert data_2["counts"]["written"] == 1
    assert data_2["results"][0]["reason"] == "forced_reimport"


def test_rebuild_dry_run_does_not_modify_index_or_call_embedding(tmp_path: Path) -> None:
    archive_path, _ = make_export(tmp_path)
    staging = tmp_path / "staging"
    index_db = tmp_path / "dry-rebuild.sqlite"
    cfg_file = tmp_path / "config_dry.yaml"
    cfg_file.write_text(
        f"""
conversation_archive:
  staging_dir: "{staging.as_posix()}"
  index_path: "{index_db.as_posix()}"
retrieval:
  embedding:
    enabled: false
""",
        encoding="utf-8",
    )
    assert main(["import", str(archive_path), "--config", str(cfg_file)]) == 0
    assert main(["index", "--config", str(cfg_file), "--dir", str(staging)]) == 0
    before_hash = hashlib.sha256(index_db.read_bytes()).hexdigest()

    report = tmp_path / "rebuild-dry.json"
    assert main([
        "rebuild-index", "--config", str(cfg_file), "--dir", str(staging),
        "--dry-run", "--json-report", str(report),
    ]) == 0
    after_hash = hashlib.sha256(index_db.read_bytes()).hexdigest()
    data = json.loads(report.read_text(encoding="utf-8"))
    assert before_hash == after_hash
    assert data["dry_run"] is True
    assert data["estimated_chunks"] > 0
    assert data["estimated_embeddings"] == 0

    # Enable an intentionally unreachable endpoint: dry-run must still avoid
    # network access and report the actually missing unique identities.
    cfg_enabled = tmp_path / "config_dry_enabled.yaml"
    cfg_enabled.write_text(
        f"""
conversation_archive:
  staging_dir: "{staging.as_posix()}"
  index_path: "{index_db.as_posix()}"
retrieval:
  embedding:
    enabled: true
    base_url: "http://127.0.0.1:9/v1"
    model: "bge-m3"
""",
        encoding="utf-8",
    )
    report_enabled = tmp_path / "rebuild-dry-enabled.json"
    assert main([
        "rebuild-index", "--config", str(cfg_enabled), "--dir", str(staging),
        "--dry-run", "--json-report", str(report_enabled),
    ]) == 0
    data_enabled = json.loads(report_enabled.read_text(encoding="utf-8"))
    assert hashlib.sha256(index_db.read_bytes()).hexdigest() == before_hash
    assert data_enabled["embedding_enabled"] is True
    assert data_enabled["estimated_embedding_identities"] > 0
    assert data_enabled["estimated_embeddings"] == data_enabled["estimated_embedding_identities"]


def test_cli_rejects_index_path_escape(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    cfg_file = tmp_path / "config_escape.yaml"
    cfg_file.write_text(
        f"""
conversation_archive:
  staging_dir: "{staging.as_posix()}"
  index_path: "../../escape.sqlite"
""",
        encoding="utf-8",
    )
    assert main(["index", "--config", str(cfg_file), "--dir", str(staging)]) == 1
    assert main(["rebuild-index", "--config", str(cfg_file), "--dir", str(staging), "--dry-run"]) == 1


def test_index_stale_manual_note_orchestration_and_archive_local_manifest(tmp_path: Path) -> None:
    archive_path, conversation_id = make_export(tmp_path)
    staging = tmp_path / "staging-stale"
    index_db = tmp_path / "stale-index.sqlite"
    legacy_manifest = tmp_path / "legacy-global-manifest.sqlite"
    cfg_file = tmp_path / "config_stale.yaml"
    cfg_file.write_text(
        f"""
conversation_archive:
  staging_dir: "{staging.as_posix()}"
  manifest_path: "{legacy_manifest.as_posix()}"
  index_path: "{index_db.as_posix()}"
""",
        encoding="utf-8",
    )

    assert main(["import", str(archive_path), "--config", str(cfg_file)]) == 0
    local_manifest = staging / ".ai-memory" / "manifest.sqlite"
    assert local_manifest.exists()
    assert not legacy_manifest.exists()

    assert main([
        "index", "--config", str(cfg_file), "--dir", str(staging), "--changed-only"
    ]) == 0
    record = ImportManifest(local_manifest).get_record(conversation_id)
    assert record is not None
    note = Path(record["output_path"])
    original = note.read_text(encoding="utf-8")
    manual_keyword = "MANUAL_SEARCHABLE_V3_9C17"
    note.write_text(original + f"\n\n{manual_keyword}\n", encoding="utf-8")

    stale_report = tmp_path / "stale-import.json"
    assert main([
        "import", str(archive_path), "--config", str(cfg_file),
        "--json-report", str(stale_report),
    ]) == 0
    stale = json.loads(stale_report.read_text(encoding="utf-8"))
    assert stale["counts"]["index_stale"] == 1
    assert stale["counts"]["written"] == 0
    assert stale["results"][0]["reason"] == "index_stale"
    assert manual_keyword in note.read_text(encoding="utf-8")

    reindex_report = tmp_path / "stale-reindex.json"
    assert main([
        "index", "--config", str(cfg_file), "--dir", str(staging), "--changed-only",
        "--json-report", str(reindex_report),
    ]) == 0
    reindexed = json.loads(reindex_report.read_text(encoding="utf-8"))
    assert reindexed["indexed_files"] == 1
    assert reindexed["change_reasons"]["changed_file"] == 1

    search_report = tmp_path / "manual-search.json"
    assert main([
        "search", manual_keyword, "--config", str(cfg_file),
        "--json-report", str(search_report),
    ]) == 0
    searched = json.loads(search_report.read_text(encoding="utf-8"))
    assert searched["count"] > 0
    assert any(manual_keyword in row["content"] for row in searched["results"])

    fresh_report = tmp_path / "fresh-import.json"
    assert main([
        "import", str(archive_path), "--config", str(cfg_file),
        "--json-report", str(fresh_report),
    ]) == 0
    fresh = json.loads(fresh_report.read_text(encoding="utf-8"))
    assert fresh["counts"]["skipped"] == 1
    assert fresh["results"][0]["reason"] == "unchanged"

