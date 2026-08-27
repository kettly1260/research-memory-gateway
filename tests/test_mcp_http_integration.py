from __future__ import annotations

import socket
import time
from threading import Thread

import anyio
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

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
        )
    )
    thread = Thread(target=server.run, daemon=True)
    thread.start()
    _wait_for_port(port)

    async def exercise() -> None:
        async with (
            streamable_http_client(f"http://127.0.0.1:{port}/mcp") as (
                read_stream,
                write_stream,
                _session_id,
            ),
            ClientSession(read_stream, write_stream) as session,
        ):
                await session.initialize()
                tools = await session.list_tools()
                assert {tool.name for tool in tools.tools} == {
                    "recall_memory",
                    "capture_memory",
                    "verify_memory",
                    "get_project_state",
                }

                recalled = await session.call_tool(
                    "recall_memory",
                    {
                        "query": "之前 Fe 的硝酸溶液怎么配的？",
                        "project": "Fe3-probe",
                    },
                )
                payload = recalled.structuredContent
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
                capture_payload = captured.structuredContent
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
                secret_payload = secret_capture.structuredContent
                assert secret_payload is not None
                assert secret_payload["action"] == "saved"
                assert secret_payload["secret_redaction"]["detected"] is True

                secret_recall = await session.call_tool(
                    "recall_memory",
                    {"query": "SECRET-123456789", "project": "security-e2e"},
                )
                secret_recall_payload = secret_recall.structuredContent
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
                assert old_path.structuredContent["action"] == "saved"
                assert new_path.structuredContent["action"] == "saved"

                path_recall = await session.call_tool(
                    "recall_memory",
                    {"query": "Origin MCP repository path", "project": "origin-e2e"},
                )
                path_payload = path_recall.structuredContent
                assert path_payload is not None
                assert path_payload["result_count"] == 1
                assert "originlab-jx" in path_payload["results"][0]["content"]

    try:
        anyio.run(exercise)
    finally:
        server.should_exit = True
        thread.join(timeout=10)
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
