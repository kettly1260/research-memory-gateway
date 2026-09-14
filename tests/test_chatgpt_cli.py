"""v0.2.6 ChatGPT importer CLI tests: format auto-detection, namespace flags,
branch filtering, and Codex default-behaviour preservation (C9)."""

from __future__ import annotations

import json
import sqlite3
import zipfile
from pathlib import Path

from research_memory_gateway.conversations.cli import main
from tests.chatgpt_fixtures import (
    TEST_ACCOUNT_GUID,
    build_export,
    linear_conversation,
)
from tests.chatgpt_fixtures import GraphBuilder, make_message


def _write_config(tmp_path: Path) -> Path:
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        f"""
conversation_archive:
  staging_dir: "{(tmp_path / 'staging').as_posix()}"
  index_path: "{(tmp_path / 'idx.sqlite').as_posix()}"
""",
        encoding="utf-8",
    )
    return cfg_file


def _branchy_conversation() -> dict:
    builder = GraphBuilder("conv-cli")
    builder.append(make_message("user", "question"))
    fork = builder.tail
    builder.append(make_message("assistant", "answer main"))
    builder.attach_branch_under(fork, make_message("assistant", "answer alt"))
    return builder.to_conversation(title="CLI Conversation")


def test_cli_chatgpt_auto_detect_import(tmp_path: Path, capsys) -> None:
    conversation = _branchy_conversation()
    archive = build_export(tmp_path / "export.zip", [conversation])
    cfg_file = _write_config(tmp_path)

    ret = main(
        [
            "import",
            str(archive),
            "--config",
            str(cfg_file),
            "--account-namespace",
            "test-label",
            "--json-report",
            str(tmp_path / "report.json"),
        ]
    )
    assert ret == 0
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    # a bare provider conversation id selects ALL of its branches
    assert report["total_requested"] == 2
    assert report["counts"]["written"] == 2

    # repeat import of the same archive: everything unchanged
    ret2 = main(
        [
            "import",
            str(archive),
            "--config",
            str(cfg_file),
            "--account-namespace",
            "test-label",
            "--json-report",
            str(tmp_path / "report2.json"),
        ]
    )
    assert ret2 == 0
    report2 = json.loads((tmp_path / "report2.json").read_text(encoding="utf-8"))
    assert report2["counts"]["skipped"] == 2
    assert report2["counts"]["written"] == 0


def test_cli_branch_id_narrows_selection(tmp_path: Path) -> None:
    conversation = _branchy_conversation()
    archive = build_export(tmp_path / "export.zip", [conversation])
    cfg_file = _write_config(tmp_path)

    # first import both branches
    assert (
        main(
            [
                "import",
                str(archive),
                "--config",
                str(cfg_file),
                "--account-namespace",
                "test-label",
            ]
        )
        == 0
    )

    # find the branch ids from the reader
    from research_memory_gateway.conversations.chatgpt_export import (
        ChatGPTExportReader,
        resolve_account_namespace_hash,
    )

    ns_hash, _ = resolve_account_namespace_hash(archive, namespace_label="test-label")
    reader = ChatGPTExportReader(archive, account_namespace_hash=ns_hash)
    refs = reader.list_sessions()
    assert len(refs) == 2
    target_branch = refs[1].source_branch_id

    # re-import only one branch via --branch-id: nothing to write
    ret = main(
        [
            "import",
            str(archive),
            "--config",
            str(cfg_file),
            "--account-namespace",
            "test-label",
            "--conversation-id",
            "conv-cli",
            "--branch-id",
            target_branch,
            "--json-report",
            str(tmp_path / "branch-report.json"),
        ]
    )
    assert ret == 0
    report = json.loads((tmp_path / "branch-report.json").read_text(encoding="utf-8"))
    assert report["total_requested"] == 1
    assert report["counts"]["skipped"] == 1


def test_cli_missing_namespace_fails(tmp_path: Path, capsys) -> None:
    conversation = linear_conversation("conv-ns-cli", [("user", "u"), ("assistant", "a")])
    archive = build_export(tmp_path / "ns.zip", [conversation], account_id=None)
    cfg_file = _write_config(tmp_path)

    ret = main(["import", str(archive), "--config", str(cfg_file)])
    assert ret == 1
    assert "ACCOUNT_NAMESPACE_REQUIRED" in capsys.readouterr().err

    # explicit opt-in default succeeds
    ret2 = main(
        [
            "import",
            str(archive),
            "--config",
            str(cfg_file),
            "--use-default-account-namespace",
        ]
    )
    assert ret2 == 0


def test_cli_export_guid_namespace_auto(tmp_path: Path, capsys) -> None:
    conversation = linear_conversation("conv-guid-cli", [("user", "u"), ("assistant", "a")])
    archive = build_export(
        tmp_path / "guid.zip", [conversation], account_id=TEST_ACCOUNT_GUID
    )
    cfg_file = _write_config(tmp_path)
    ret = main(["import", str(archive), "--config", str(cfg_file)])
    assert ret == 0
    # no namespace flags needed: the export GUID supplies the namespace
    out = capsys.readouterr().out
    assert TEST_ACCOUNT_GUID not in out  # raw GUID never surfaces


def test_cli_ambiguous_format_rejected(tmp_path: Path, capsys) -> None:
    conversation = linear_conversation("conv-amb", [("user", "u"), ("assistant", "a")])
    archive = tmp_path / "ambiguous.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("manifest.json", json.dumps({"kind": "codex-session-export", "sessions": []}))
        zf.writestr("conversations.json", json.dumps([conversation]))

    ret = main(["import", str(archive), "--config", str(_write_config(tmp_path))])
    assert ret == 1
    assert "AMBIGUOUS_EXPORT_FORMAT" in capsys.readouterr().err


def test_cli_audit_export_chatgpt_schema(tmp_path: Path, capsys) -> None:
    conversation = _branchy_conversation()
    archive = build_export(tmp_path / "audit.zip", [conversation])
    ret = main(
        [
            "audit-export",
            str(archive),
            "--account-namespace",
            "test-label",
            "--json-report",
            str(tmp_path / "audit.json"),
        ]
    )
    assert ret == 0
    audit = json.loads((tmp_path / "audit.json").read_text(encoding="utf-8"))
    assert audit["conversation_count"] == 1
    assert audit["branch_shape"]["primary_branches"] == 1
    assert audit["branch_shape"]["alternate_branches"] == 1
    assert audit["conversations_top_level_type"] == "list"


def test_cli_codex_default_behaviour_preserved(tmp_path: Path, capsys) -> None:
    """H-regression: a Codex ZIP still imports through the same CLI path."""
    sys_path_marker = tmp_path / "codex.zip"
    with zipfile.ZipFile(sys_path_marker, "w") as zf:
        # minimal Codex-shaped export with one linear session
        session_id = "codex-session-1"
        rollout = f"sessions/{session_id}.jsonl"
        lines = [
            json.dumps({"type": "session_meta", "ordinal": 0, "timestamp": "", "payload": {
                "id": session_id, "timestamp": "2026-01-01T00:00:00Z", "cwd": "C:/work",
                "originator": "codex-cli", "cli_version": "0.1.0", "instructions": "",
            }}),
            json.dumps({"type": "response_item", "ordinal": 1, "timestamp": "2026-01-01T00:00:01Z",
                        "payload": {"type": "message", "role": "user", "id": "m1",
                                    "content": [{"type": "input_text", "text": "hello codex"}]}}),
            json.dumps({"type": "response_item", "ordinal": 2, "timestamp": "2026-01-01T00:00:02Z",
                        "payload": {"type": "message", "role": "assistant", "id": "m2", "phase": "final_answer",
                                    "content": [{"type": "output_text", "text": "hi there"}]}}),
        ]
        zf.writestr(rollout, "\n".join(lines))
        zf.writestr(
            "manifest.json",
            json.dumps(
                {
                    "kind": "codex-session-export",
                    "packageVersion": 1,
                    "exportedAt": "2026-01-01T00:00:00Z",
                    "sessions": [
                        {
                            "sessionId": session_id,
                            "title": "Codex Session",
                            "cwd": "C:/work",
                            "updatedAt": 1767225600,
                            "fileEntry": rollout,
                            "sha256": __import__("hashlib").sha256("\n".join(lines).encode("utf-8")).hexdigest(),
                            "sizeBytes": len("\n".join(lines).encode("utf-8")),
                            "relativeRolloutPath": rollout,
                            "sourceInstance": "main",
                            "sessionIndexEntry": {},
                        }
                    ],
                }
            ),
        )

    cfg_file = _write_config(tmp_path)
    ret = main(["import", str(sys_path_marker), "--config", str(cfg_file)])
    assert ret == 0
    out = json.loads(capsys.readouterr().out)
    assert out["counts"]["written"] == 1
    assert out["total_requested"] == 1
