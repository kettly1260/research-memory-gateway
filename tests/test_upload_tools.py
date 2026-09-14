from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import anyio
import pytest

from research_memory_gateway.backends import SQLiteMemoryBackend
from research_memory_gateway.config import AppConfig
from research_memory_gateway.server import build_mcp
from research_memory_gateway.service import ResearchMemoryService
from tests.chatgpt_fixtures import linear_conversation, build_export


def _setup_service(tmp_path: Path) -> ResearchMemoryService:
    config = AppConfig()
    config.backend.sqlite_path = str(tmp_path / "mem.db")
    config.conversation_archive.enabled = True
    config.conversation_archive.staging_dir = str(tmp_path / "staging")
    config.conversation_archive.index_path = str(tmp_path / "conv_idx.sqlite")
    config.upload.base_dir = str(tmp_path / "uploads")

    backend = SQLiteMemoryBackend(config.backend.sqlite_path)
    return ResearchMemoryService(config, backend)


def test_upload_tools_mcp_surface_registration(tmp_path: Path) -> None:
    config = AppConfig()
    config.backend.sqlite_path = str(tmp_path / "mem.db")
    config.conversation_archive.enabled = True
    config.conversation_archive.staging_dir = str(tmp_path / "staging")
    config.conversation_archive.index_path = str(tmp_path / "conv_idx.sqlite")
    config.upload.base_dir = str(tmp_path / "uploads")

    mcp = build_mcp(config)
    tool_names = {t.name for t in anyio.run(mcp.list_tools)}

    expected_upload_tools = {
        "conversation_upload_create",
        "conversation_upload_status",
        "conversation_upload_abort",
        "conversation_upload_commit",
        "conversation_ingest_turn",
    }
    assert expected_upload_tools.issubset(tool_names)


def test_upload_tools_lifecycle(tmp_path: Path) -> None:
    service = _setup_service(tmp_path)

    # 1. Build a valid ChatGPT export zip
    c1 = linear_conversation(
        "conv-chatgpt-1",
        [("user", "What is the boiling point of nitrogen?"), ("assistant", "Nitrogen boils at 77.36 K (-195.79 C).")],
        create_time=1757800100.0,
    )
    zip_path = tmp_path / "chatgpt_export.zip"
    build_export(zip_path, [c1], account_id="00000000-0000-0000-0000-000000000001")

    zip_bytes = zip_path.read_bytes()
    zip_sha = hashlib.sha256(zip_bytes).hexdigest()
    zip_size = len(zip_bytes)

    # 2. Tool: conversation_upload_create
    from research_memory_gateway.agent_surface.upload_tools import (
        conversation_upload_create,
        conversation_upload_status,
        conversation_upload_abort,
        conversation_upload_commit,
        conversation_ingest_turn,
    )

    create_res = conversation_upload_create(
        service,
        filename="chatgpt_export.zip",
        size_bytes=zip_size,
        sha256=zip_sha,
        source_hint="chatgpt",
    )
    upload_id = create_res["upload_id"]
    assert upload_id
    assert create_res["upload_url"] == f"/uploads/{upload_id}"
    assert create_res["state"] == "created"

    # 3. Tool: conversation_upload_status (initial)
    status_res = conversation_upload_status(service, upload_id=upload_id)
    assert status_res["upload_id"] == upload_id
    assert status_res["total_bytes"] == zip_size
    assert status_res["received_bytes"] == 0
    assert not status_res["completed"]

    # 4. Commit prematurely should fail
    with pytest.raises(ValueError, match="upload is incomplete"):
        conversation_upload_commit(service, upload_id=upload_id)

    # 5. Simulate writing the uploaded bytes to disk
    physical_path = service.upload_manager.storage._get_upload_path(upload_id)
    physical_path.write_bytes(zip_bytes)

    # Status should now report completed
    status_res = conversation_upload_status(service, upload_id=upload_id)
    assert status_res["completed"]

    # 6. Tool: conversation_upload_commit
    commit_res = conversation_upload_commit(service, upload_id=upload_id)
    assert commit_res["status"] == "committed"
    assert commit_res["format"] == "chatgpt"
    assert commit_res["counts"]["written"] >= 1
    assert commit_res["counts"]["failed"] == 0

    # Verify Markdown notes exist in staging
    staging_dir = Path(commit_res["output_root"])
    md_files = list(staging_dir.glob("**/*.md"))
    assert len(md_files) >= 1

    # Verify notes are indexed into SQLite FTS
    search_res = service.conversation_retrieval.search(query="77.36 K")
    assert search_res["count"] >= 1
    assert "77.36 K" in search_res["results"][0]["content"]

    # 7. Test re-commit idempotency (should skip)
    recommit_res = conversation_upload_commit(service, upload_id=upload_id)
    assert recommit_res["counts"]["skipped"] >= 1
    assert recommit_res["counts"]["written"] == 0


def test_conversation_upload_abort(tmp_path: Path) -> None:
    service = _setup_service(tmp_path)
    from research_memory_gateway.agent_surface.upload_tools import (
        conversation_upload_create,
        conversation_upload_abort,
        conversation_upload_status,
    )

    create_res = conversation_upload_create(
        service,
        filename="dummy.zip",
        size_bytes=100,
        sha256="a" * 64,
    )
    upload_id = create_res["upload_id"]

    abort_res = conversation_upload_abort(service, upload_id=upload_id)
    assert abort_res["aborted"] is True
    assert abort_res["state"] == "aborted"

    status = conversation_upload_status(service, upload_id=upload_id)
    assert status["state"] == "aborted"


def test_conversation_ingest_turn(tmp_path: Path) -> None:
    service = _setup_service(tmp_path)
    from research_memory_gateway.agent_surface.upload_tools import conversation_ingest_turn

    # Invalid role
    with pytest.raises(ValueError, match="Invalid role"):
        conversation_ingest_turn(
            service,
            session_id="session-turn-1",
            role="alien",
            content="Hello world",
        )

    # Ingest user turn
    res1 = conversation_ingest_turn(
        service,
        session_id="session-turn-1",
        role="user",
        content="Could you synthesize 4-methylumbelliferone derivatives?",
        title="Fluorescent Probe Synthesis",
    )
    assert res1["status"] == "ingested"
    assert res1["indexed"] is True
    file_path = Path(res1["file_path"])
    assert file_path.exists()

    # Search should immediately find the turn
    search1 = service.conversation_retrieval.search(query="4-methylumbelliferone")
    assert search1["count"] >= 1
    assert "derivatives" in search1["results"][0]["content"]

    # Ingest assistant turn
    res2 = conversation_ingest_turn(
        service,
        session_id="session-turn-1",
        role="assistant",
        content="Yes, the Pechmann condensation with resorcinol and ethyl acetoacetate works well.",
    )
    assert res2["status"] == "ingested"
    assert res2["indexed"] is True

    # Search should immediately find the assistant turn
    search2 = service.conversation_retrieval.search(query="Pechmann condensation")
    assert search2["count"] >= 1
    assert "resorcinol" in search2["results"][0]["content"]


def test_conversation_ingest_snapshot(tmp_path: Path) -> None:
    service = _setup_service(tmp_path)
    from research_memory_gateway.agent_surface.upload_tools import conversation_ingest_snapshot

    # Empty messages should fail
    with pytest.raises(ValueError, match="messages must be a non-empty list"):
        conversation_ingest_snapshot(
            service,
            session_id="session-snap-1",
            messages=[],
        )

    # Ingest multi-turn snapshot
    snapshot_res = conversation_ingest_snapshot(
        service,
        session_id="session-snap-1",
        title="Catalyst Stability Analysis",
        model="gpt-5-preview",
        messages=[
            {
                "role": "user",
                "content": "What is the degradation mechanism of perovskite quantum dots under UV irradiation?",
                "timestamp": "2026-09-14T10:00:00Z",
            },
            {
                "role": "assistant",
                "content": "UV induces photo-oxidation of surface organic ligands (oleate/oleylammonium), leading to lead halide cluster desorption and deep-level defect generation.",
                "timestamp": "2026-09-14T10:00:30Z",
            },
        ],
    )

    assert snapshot_res["status"] == "ingested"
    assert snapshot_res["message_count"] == 2
    assert snapshot_res["indexed"] is True
    file_path = Path(snapshot_res["file_path"])
    assert file_path.exists()

    # Search for terms in snapshot
    search_res = service.conversation_retrieval.search(query="oleylammonium")
    assert search_res["count"] >= 1
    assert "photo-oxidation" in search_res["results"][0]["content"]

    search_user = service.conversation_retrieval.search(query="perovskite")
    assert search_user["count"] >= 1
    assert "degradation mechanism" in search_user["results"][0]["content"]
