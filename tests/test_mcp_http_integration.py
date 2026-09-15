from __future__ import annotations

import socket
import time
from threading import Thread

import anyio
import httpx
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from research_memory_gateway.agent_surface.tools import AGENT_TOOL_NAMES
from research_memory_gateway.backends import SQLiteMemoryBackend
from research_memory_gateway.config import AppConfig
from research_memory_gateway.models import ResearchMemory
from research_memory_gateway.server import _build_streamable_http_app, build_mcp


def test_real_streamable_http_agent_surface_and_recall(tmp_path) -> None:
    port = _free_port()
    config = AppConfig()
    config.backend.sqlite_path = str(tmp_path / "mcp-http.db")
    config.server.host = "127.0.0.1"
    config.server.port = port
    config.server.surface = "agent"

    backend = SQLiteMemoryBackend(config.backend.sqlite_path)
    backend.save(
        ResearchMemory.model_validate(
            {
                "project": "Fe3-probe",
                "topic": "Fe3+ stock preparation",
                "memory_type": "material_system",
                "title": "Fe3+ nitrate stock preparation",
                "summary": "Fe3+ stock preparation record.",
                "claims": [
                    {
                        "claim_id": "claim_fe_stock",
                        "claim": "Fe3+ 储备液浓度为 10 mM，并使用 0.1 M HNO3 作为酸性介质。",
                        "verification_status": "unverified",
                    }
                ],
            }
        )
    )

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

    health = httpx.get(f"http://127.0.0.1:{port}/health", timeout=5)
    assert health.status_code == 200
    health_data = health.json()
    assert health_data["media_index"]["enabled"] is True
    assert health_data["media_index"]["shares_embedding_provider"] is True
    assert set(health_data["mcp"]["tools"]) == set(AGENT_TOOL_NAMES)

    async def exercise() -> None:
        async with (
            streamable_http_client(f"http://127.0.0.1:{port}/mcp") as (
                read_stream,
                write_stream,
            ),
            ClientSession(read_stream, write_stream) as session,
        ):
                await session.initialize()
                tools = await session.list_tools()
                assert {tool.name for tool in tools.tools} == set(AGENT_TOOL_NAMES)
                tool_by_name = {tool.name: tool for tool in tools.tools}
                assert (tool_by_name["recall_memory"].annotations.read_only_hint
                        if hasattr(tool_by_name["recall_memory"].annotations, "read_only_hint")
                        else tool_by_name["recall_memory"].annotations.readOnlyHint) is True
                assert (tool_by_name["verify_memory"].annotations.read_only_hint
                        if hasattr(tool_by_name["verify_memory"].annotations, "read_only_hint")
                        else tool_by_name["verify_memory"].annotations.readOnlyHint) is True
                assert (tool_by_name["get_project_state"].annotations.read_only_hint
                        if hasattr(tool_by_name["get_project_state"].annotations, "read_only_hint")
                        else tool_by_name["get_project_state"].annotations.readOnlyHint) is True
                assert (tool_by_name["capture_memory"].annotations.read_only_hint
                        if hasattr(tool_by_name["capture_memory"].annotations, "read_only_hint")
                        else tool_by_name["capture_memory"].annotations.readOnlyHint) is False
                assert (tool_by_name["capture_memory"].annotations.destructive_hint
                        if hasattr(tool_by_name["capture_memory"].annotations, "destructive_hint")
                        else tool_by_name["capture_memory"].annotations.destructiveHint) is False

                recalled = await session.call_tool(
                    "recall_memory",
                    {
                        "query": "之前 Fe 的硝酸溶液怎么配的？",
                        "project": "Fe3-probe",
                    },
                )
                payload = getattr(recalled, "structured_content", None) or getattr(recalled, "structuredContent", None)
                assert payload is not None
                assert payload["result_count"] == 1
                assert payload["results"][0]["matched_claims"][0]["claim_id"] == "claim_fe_stock"
                assert "10 mM" in payload["results"][0]["content"]

                captured = await session.call_tool(
                    "capture_memory",
                    {
                        "content": "该材料介电常数为 2.8。",
                        "project": "materials",
                    },
                )
                capture_payload = getattr(captured, "structured_content", None) or getattr(captured, "structuredContent", None)
                assert capture_payload is not None
                assert capture_payload["action"] == "queued"
                assert capture_payload["memory_tier"] == "trusted"

                secret_capture = await session.call_tool(
                    "capture_memory",
                    {
                        "content": (
                            "MCP config uses API token sk-test-SECRET-123456789 "
                            "and repository path G:\\LLM\\memory."
                        ),
                        "project": "security-e2e",
                    },
                )
                secret_payload = getattr(secret_capture, "structured_content", None) or getattr(secret_capture, "structuredContent", None)
                assert secret_payload is not None
                assert secret_payload["action"] == "saved"
                assert secret_payload["secret_redaction"]["detected"] is True

                secret_recall = await session.call_tool(
                    "recall_memory",
                    {"query": "SECRET-123456789", "project": "security-e2e"},
                )
                secret_recall_payload = getattr(secret_recall, "structured_content", None) or getattr(secret_recall, "structuredContent", None)
                assert secret_recall_payload is not None
                assert secret_recall_payload["result_count"] == 0

                old_path = await session.call_tool(
                    "capture_memory",
                    {
                        "content": "Origin MCP repository path is G:\\LLM\\originlab-old.",
                        "project": "origin-e2e",
                    },
                )
                new_path = await session.call_tool(
                    "capture_memory",
                    {
                        "content": "Origin MCP repository path changed to G:\\LLM\\originlab-jx.",
                        "project": "origin-e2e",
                    },
                )
                old_payload = getattr(old_path, "structured_content", None) or getattr(old_path, "structuredContent", None)
                new_payload = getattr(new_path, "structured_content", None) or getattr(new_path, "structuredContent", None)
                assert old_payload["action"] == "saved"
                assert new_payload["action"] == "saved"

                path_recall = await session.call_tool(
                    "recall_memory",
                    {"query": "Origin MCP repository path", "project": "origin-e2e"},
                )
                path_payload = getattr(path_recall, "structured_content", None) or getattr(path_recall, "structuredContent", None)
                assert path_payload is not None
                assert path_payload["result_count"] == 1
                assert "originlab-jx" in path_payload["results"][0]["content"]

    try:
        anyio.run(exercise)
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        if thread.is_alive():
            server.force_exit = True
            thread.join(timeout=5)
        assert not thread.is_alive()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_port(port: int, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.05)
    raise RuntimeError(f"MCP test server did not start on port {port}")
