from __future__ import annotations

import hashlib
import io
import json
import socket
import time
from pathlib import Path
from threading import Thread

import anyio
import httpx
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from tusclient import client as tus_client

from research_memory_gateway.config import AppConfig
from research_memory_gateway.server import _build_streamable_http_app, build_mcp


REAL_BACKUP_PATH = Path(r"D:\Download\chatgpt_business_backup_selected_fe828cbb-7312-4979-8049-9a25c3362b7c_2026-09-14.zip")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_port(port: int, timeout: float = 8.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise TimeoutError(f"Port {port} did not open within {timeout}s")


@pytest.mark.skipif(not REAL_BACKUP_PATH.exists(), reason="Real backup file not found")
def test_real_chatgpt_backup_mcp_e2e_upload_and_recall(tmp_path: Path) -> None:
    port = _free_port()
    config = AppConfig()
    config.backend.sqlite_path = str(tmp_path / "mem.db")
    config.server.host = "127.0.0.1"
    config.server.port = port
    config.server.surface = "agent"
    config.conversation_archive.enabled = True
    config.conversation_archive.staging_dir = str(tmp_path / "staging")
    config.conversation_archive.index_path = str(tmp_path / "conv_idx.sqlite")
    config.upload.base_dir = str(tmp_path / "uploads")

    mcp = build_mcp(config)
    app = _build_streamable_http_app(mcp, None, config)

    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=config.server.host,
            port=port,
            log_level="error",
            timeout_graceful_shutdown=0,
        )
    )
    thread = Thread(target=server.run, daemon=True)
    thread.start()
    _wait_for_port(port)

    raw_zip = REAL_BACKUP_PATH.read_bytes()
    real_sha256 = hashlib.sha256(raw_zip).hexdigest()
    real_size = len(raw_zip)

    async def scenario() -> None:
        async with (
            streamable_http_client(f"http://127.0.0.1:{port}/mcp") as (
                read_stream,
                write_stream,
            ),
            ClientSession(read_stream, write_stream) as session,
        ):
            await session.initialize()
            tools = await session.list_tools()
            tool_names = {t.name for t in tools.tools}

            assert "conversation_upload_create" in tool_names
            assert "conversation_upload_status" in tool_names
            assert "conversation_upload_abort" in tool_names
            assert "conversation_upload_commit" in tool_names
            assert "conversation_ingest_turn" in tool_names
            assert "conversation_search" in tool_names
            assert "conversation_read" in tool_names
            assert "conversation_recall" in tool_names

            # -------------------------------------------------------------
            # Stage 1: Create upload session via MCP
            # -------------------------------------------------------------
            create_call = await session.call_tool(
                "conversation_upload_create",
                arguments={
                    "filename": REAL_BACKUP_PATH.name,
                    "size_bytes": real_size,
                    "sha256": real_sha256,
                    "source_hint": "chatgpt",
                },
            )
            assert not create_call.is_error
            create_data = create_call.content[0].text if hasattr(create_call.content[0], "text") else create_call.content[0]
            if isinstance(create_data, str):
                create_data = json.loads(create_data)

            upload_id = create_data["upload_id"]
            upload_url_path = create_data["upload_url"]
            assert upload_id
            assert upload_url_path == f"/uploads/{upload_id}"

            # -------------------------------------------------------------
            # Stage 2: Upload via tuspy client in chunks
            # -------------------------------------------------------------
            full_upload_url = f"http://127.0.0.1:{port}{upload_url_path}"
            t_client = tus_client.TusClient(f"http://127.0.0.1:{port}/uploads/")
            uploader = t_client.uploader(
                file_stream=io.BytesIO(raw_zip),
                url=full_upload_url,
                chunk_size=256 * 1024,  # 256 KB chunks
            )
            uploader.upload()

            # -------------------------------------------------------------
            # Stage 3: Verify upload status via MCP
            # -------------------------------------------------------------
            status_call = await session.call_tool(
                "conversation_upload_status",
                arguments={"upload_id": upload_id},
            )
            assert not status_call.is_error
            status_data = status_call.content[0].text if hasattr(status_call.content[0], "text") else status_call.content[0]
            if isinstance(status_data, str):
                status_data = json.loads(status_data)

            assert status_data["completed"] is True
            assert status_data["received_bytes"] == real_size

            # -------------------------------------------------------------
            # Stage 4: Commit upload via MCP
            # -------------------------------------------------------------
            commit_call = await session.call_tool(
                "conversation_upload_commit",
                arguments={"upload_id": upload_id},
            )
            assert not commit_call.is_error
            commit_data = commit_call.content[0].text if hasattr(commit_call.content[0], "text") else commit_call.content[0]
            if isinstance(commit_data, str):
                commit_data = json.loads(commit_data)

            assert commit_data["status"] == "committed"
            assert commit_data["format"] == "chatgpt"
            assert commit_data["counts"]["written"] >= 1
            assert commit_data["counts"]["failed"] == 0

            # -------------------------------------------------------------
            # Stage 5: Search via MCP conversation_search
            # -------------------------------------------------------------
            search_call = await session.call_tool(
                "conversation_search",
                arguments={"query": "CONVERSATION_MEMORY_IMPLEMENTATION_TASKBOOK"},
            )
            assert not search_call.is_error
            search_data = search_call.content[0].text if hasattr(search_call.content[0], "text") else search_call.content[0]
            if isinstance(search_data, str):
                search_data = json.loads(search_data)

            assert search_data["count"] >= 1
            first_match = search_data["results"][0]
            note_path = first_match["vault_path"]
            assert note_path
            assert Path(note_path).exists()

            # -------------------------------------------------------------
            # Stage 6: Read via MCP conversation_read
            # -------------------------------------------------------------
            read_call = await session.call_tool(
                "conversation_read",
                arguments={"file_path": note_path},
            )
            assert not read_call.is_error
            read_data = read_call.content[0].text if hasattr(read_call.content[0], "text") else read_call.content[0]
            if isinstance(read_data, str):
                read_data = json.loads(read_data)

            assert "content" in read_data
            assert len(read_data["content"]) > 100

            # -------------------------------------------------------------
            # Stage 7: Recall via MCP conversation_recall
            # -------------------------------------------------------------
            recall_call = await session.call_tool(
                "conversation_recall",
                arguments={
                    "query": "CONVERSATION_MEMORY_IMPLEMENTATION_TASKBOOK",
                    "token_budget": 1000,
                },
            )
            assert not recall_call.is_error
            recall_data = recall_call.content[0].text if hasattr(recall_call.content[0], "text") else recall_call.content[0]
            if isinstance(recall_data, str):
                recall_data = json.loads(recall_data)

            assert len(recall_data["items"]) >= 1
            assert "context" in recall_data
            assert len(recall_data["context"]) > 50

            # -------------------------------------------------------------
            # Stage 8: Idempotency re-upload test
            # -------------------------------------------------------------
            create_call_2 = await session.call_tool(
                "conversation_upload_create",
                arguments={
                    "filename": REAL_BACKUP_PATH.name,
                    "size_bytes": real_size,
                    "sha256": real_sha256,
                    "source_hint": "chatgpt",
                },
            )
            create_data_2 = json.loads(create_call_2.content[0].text if hasattr(create_call_2.content[0], "text") else create_call_2.content[0])
            upload_id_2 = create_data_2["upload_id"]

            uploader_2 = t_client.uploader(
                file_stream=io.BytesIO(raw_zip),
                url=f"http://127.0.0.1:{port}/uploads/{upload_id_2}",
                chunk_size=256 * 1024,
            )
            uploader_2.upload()

            commit_call_2 = await session.call_tool(
                "conversation_upload_commit",
                arguments={"upload_id": upload_id_2},
            )
            assert not commit_call_2.is_error
            commit_data_2 = json.loads(commit_call_2.content[0].text if hasattr(commit_call_2.content[0], "text") else commit_call_2.content[0])

            # Zero rewrites, all skipped!
            assert commit_data_2["counts"]["written"] == 0
            assert commit_data_2["counts"]["skipped"] >= 1

    try:
        anyio.run(scenario)
    finally:
        server.should_exit = True
        thread.join(timeout=3.0)


def test_real_interruption_and_resumption_e2e(tmp_path: Path) -> None:
    port = _free_port()
    config = AppConfig()
    config.backend.sqlite_path = str(tmp_path / "mem.db")
    config.server.host = "127.0.0.1"
    config.server.port = port
    config.server.surface = "agent"
    config.conversation_archive.enabled = True
    config.conversation_archive.staging_dir = str(tmp_path / "staging")
    config.conversation_archive.index_path = str(tmp_path / "conv_idx.sqlite")
    config.upload.base_dir = str(tmp_path / "uploads")

    mcp = build_mcp(config)
    app = _build_streamable_http_app(mcp, None, config)

    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=config.server.host,
            port=port,
            log_level="error",
            timeout_graceful_shutdown=0,
        )
    )
    thread = Thread(target=server.run, daemon=True)
    thread.start()
    _wait_for_port(port)

    # 1. Prepare a synthetic payload of 600 KB
    payload = b"A" * (300 * 1024) + b"B" * (300 * 1024)
    sha256 = hashlib.sha256(payload).hexdigest()
    size = len(payload)

    async def scenario() -> None:
        async with (
            streamable_http_client(f"http://127.0.0.1:{port}/mcp") as (
                read_stream,
                write_stream,
            ),
            ClientSession(read_stream, write_stream) as session,
        ):
            await session.initialize()

            # 1. Create upload
            res = await session.call_tool(
                "conversation_upload_create",
                arguments={
                    "filename": "interrupt_test.bin",
                    "size_bytes": size,
                    "sha256": sha256,
                },
            )
            data = json.loads(res.content[0].text if hasattr(res.content[0], "text") else res.content[0])
            upload_id = data["upload_id"]
            upload_url = f"http://127.0.0.1:{port}{data['upload_url']}"

            # 2. Upload first chunk only (200 KB) -> Simulate network interruption
            chunk_1 = payload[: 200 * 1024]
            async with httpx.AsyncClient() as http_client:
                patch_res = await http_client.patch(
                    upload_url,
                    headers={
                        "Tus-Resumable": "1.0.0",
                        "Upload-Offset": "0",
                        "Content-Type": "application/offset+octet-stream",
                    },
                    content=chunk_1,
                )
                assert patch_res.status_code == 204
                assert patch_res.headers.get("Upload-Offset") == str(200 * 1024)

            # 3. Check status via MCP -> incomplete
            status_res = await session.call_tool(
                "conversation_upload_status",
                arguments={"upload_id": upload_id},
            )
            status_data = json.loads(status_res.content[0].text if hasattr(status_res.content[0], "text") else status_res.content[0])
            assert status_data["received_bytes"] == 200 * 1024
            assert status_data["completed"] is False

            # 4. Premature commit via MCP -> must fail closed
            try:
                commit_res = await session.call_tool(
                    "conversation_upload_commit",
                    arguments={"upload_id": upload_id},
                )
                assert commit_res.is_error
            except Exception as exc:
                assert "incomplete" in str(exc).lower() or "error" in str(exc).lower()

            # 5. Resume upload from offset 200 KB to end using standard tusclient
            t_client = tus_client.TusClient(f"http://127.0.0.1:{port}/uploads/")
            uploader = t_client.uploader(
                file_stream=io.BytesIO(payload),
                url=upload_url,
                chunk_size=100 * 1024,
            )
            uploader.upload()

            # 6. Check status via MCP -> now completed
            status_res_after = await session.call_tool(
                "conversation_upload_status",
                arguments={"upload_id": upload_id},
            )
            status_data_after = json.loads(status_res_after.content[0].text if hasattr(status_res_after.content[0], "text") else status_res_after.content[0])
            assert status_data_after["received_bytes"] == size
            assert status_data_after["completed"] is True

    try:
        anyio.run(scenario)
    finally:
        server.should_exit = True
        thread.join(timeout=3.0)
