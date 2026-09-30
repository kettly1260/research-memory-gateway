"""HTTP ingest API over the real ASGI app, including authentication.

The server is the same Starlette app the production process serves, so these
tests also prove the ingest routes coexist with the MCP transport and share the
existing ``BearerAuthMiddleware`` (there is no second auth system).
"""

from __future__ import annotations

import socket
import time
from pathlib import Path
from threading import Thread

import httpx
import pytest
import uvicorn

from research_memory_gateway.server import _build_streamable_http_app, build_mcp
from tests.ingest_helpers import build_config, event


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
    raise RuntimeError(f"test server did not start on port {port}")


class GatewayServer:
    """A real uvicorn-hosted gateway for one test."""

    def __init__(self, tmp_path: Path, *, token: str | None = "test-token", enabled: bool = True):
        self.token = token
        self.config = build_config(tmp_path, enabled=enabled)
        self.port = _free_port()
        self.config.server.host = "127.0.0.1"
        self.config.server.port = self.port
        self.mcp = build_mcp(self.config)
        self.app = _build_streamable_http_app(self.mcp, token, self.config)
        self.server = uvicorn.Server(
            uvicorn.Config(
                self.app,
                host=self.config.server.host,
                port=self.port,
                log_level="error",
                timeout_graceful_shutdown=0,
            )
        )
        self.thread = Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> GatewayServer:
        self.thread.start()
        _wait_for_port(self.port)
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=5)
        if self.thread.is_alive():
            self.server.force_exit = True
            self.thread.join(timeout=5)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def client(self, *, token: str | None = None) -> httpx.Client:
        headers = {}
        effective = self.token if token is None else token
        if effective:
            headers["Authorization"] = f"Bearer {effective}"
        # `trust_env=False`: never route loopback traffic through an ambient HTTP
        # proxy -- a proxied keep-alive connection can start answering 404.
        return httpx.Client(base_url=self.url, headers=headers, timeout=15, trust_env=False)

    @property
    def service(self):
        return self.mcp._rmg_service  # type: ignore[attr-defined]


@pytest.fixture
def gateway(tmp_path: Path):
    with GatewayServer(tmp_path) as server:
        yield server


@pytest.fixture
def open_gateway(tmp_path: Path):
    """A gateway with no token configured (loopback is allowed)."""
    with GatewayServer(tmp_path, token=None) as server:
        yield server


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------


def test_batch_requires_a_bearer_token(gateway: GatewayServer) -> None:
    with httpx.Client(base_url=gateway.url, timeout=15, trust_env=False) as client:
        response = client.post(
            "/api/conversations/events/batch",
            json={"schema_version": 1, "client_id": "c", "events": []},
        )
    assert response.status_code == 401
    assert gateway.service.conversation_ingest.store.counters()["accepted_total"] == 0


def test_batch_rejects_a_wrong_token(gateway: GatewayServer) -> None:
    with gateway.client(token="not-the-token") as client:
        response = client.post(
            "/api/conversations/events/batch",
            json={"schema_version": 1, "client_id": "c", "events": []},
        )
    assert response.status_code == 401


def test_stats_endpoint_requires_a_token(gateway: GatewayServer) -> None:
    with httpx.Client(base_url=gateway.url, timeout=15, trust_env=False) as client:
        assert client.get("/api/conversations/ingest/stats").status_code == 401
    with gateway.client() as client:
        assert client.get("/api/conversations/ingest/stats").status_code == 200


def test_loopback_without_a_configured_token_is_allowed(open_gateway: GatewayServer) -> None:
    with open_gateway.client(token=None) as client:
        response = client.post(
            "/api/conversations/events/batch",
            json={"schema_version": 1, "client_id": "c", "events": []},
        )
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# batch / single ingest
# ---------------------------------------------------------------------------


def test_single_event_endpoint_accepts_and_reports(gateway: GatewayServer) -> None:
    payload = event(event_id="rmb1_" + "1" * 40)
    with gateway.client() as client:
        response = client.post("/api/conversations/events", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["schema_version"] == 1
    assert body["accepted"] == [payload["event_id"]]
    assert body["duplicates"] == []
    assert body["rejected"] == []


def test_batch_endpoint_reports_accepted_duplicates_and_rejected(gateway: GatewayServer) -> None:
    first = event(event_id="rmb1_" + "2" * 40, content="first")
    second = event(event_id="rmb1_" + "3" * 40, message_id="msg-2", content="second")
    with gateway.client() as client:
        initial = client.post(
            "/api/conversations/events/batch",
            json={"schema_version": 1, "client_id": "machine-a", "events": [first, second]},
        )
        assert initial.status_code == 200
        assert initial.json()["accepted"] == [first["event_id"], second["event_id"]]

        # Replay: identical ids must come back as duplicates, never re-archived.
        replay = client.post(
            "/api/conversations/events/batch",
            json={"schema_version": 1, "client_id": "machine-a", "events": [first, second]},
        )
        body = replay.json()
        assert body["accepted"] == []
        assert sorted(body["duplicates"]) == sorted([first["event_id"], second["event_id"]])

        # A partial failure is reported per event and does not roll back the rest.
        third = event(event_id="rmb1_" + "4" * 40, message_id="msg-3", content="third")
        conflict = dict(first, content="tampered")
        mixed = client.post(
            "/api/conversations/events/batch",
            json={
                "schema_version": 1,
                "client_id": "machine-a",
                "events": [third, conflict],
            },
        )
        mixed_body = mixed.json()
        assert mixed_body["accepted"] == [third["event_id"]]
        assert [item["code"] for item in mixed_body["rejected"]] == ["event_id_conflict"]
        assert mixed_body["rejected"][0]["event_id"] == first["event_id"]


def test_unsupported_schema_version_is_rejected(gateway: GatewayServer) -> None:
    with gateway.client() as client:
        response = client.post(
            "/api/conversations/events/batch",
            json={"schema_version": 99, "client_id": "c", "events": []},
        )
    # A whole-request refusal is a client error; the envelope is still returned
    # so the client can read the stable machine-readable code.
    assert response.status_code == 400
    body = response.json()
    assert body["accepted"] == []
    assert [item["code"] for item in body["rejected"]] == ["unsupported_schema_version"]


def test_unknown_fields_are_rejected_rather_than_ignored(gateway: GatewayServer) -> None:
    payload = event(event_id="rmb1_" + "5" * 40)
    payload["not_a_real_field"] = "x"
    with gateway.client() as client:
        response = client.post("/api/conversations/events", json=payload)
    assert response.status_code == 400
    assert response.json()["rejected"][0]["code"] == "invalid_payload"


def test_invalid_role_and_event_type_produce_stable_codes(gateway: GatewayServer) -> None:
    with gateway.client() as client:
        bad_role = client.post(
            "/api/conversations/events",
            json=event(event_id="rmb1_" + "6" * 40, role="wizard"),
        )
        bad_type = client.post(
            "/api/conversations/events",
            json=event(event_id="rmb1_" + "7" * 40, event_type="something_else"),
        )
    assert bad_role.status_code == 400
    assert bad_role.json()["rejected"][0]["code"] == "invalid_role"
    assert bad_type.status_code == 400
    assert bad_type.json()["rejected"][0]["code"] == "invalid_event_type"


def test_malformed_json_is_rejected(gateway: GatewayServer) -> None:
    with gateway.client() as client:
        response = client.post(
            "/api/conversations/events",
            content=b"{not json",
            headers={"Content-Type": "application/json"},
        )
    assert response.status_code == 400
    assert response.json()["rejected"][0]["code"] == "invalid_payload"


def test_oversized_batch_is_rejected(gateway: GatewayServer) -> None:
    gateway.config.conversation_ingest.max_batch_events = 1
    events = [
        event(event_id=f"rmb1_{index:040d}") for index in range(2)
    ]
    with gateway.client() as client:
        response = client.post(
            "/api/conversations/events/batch",
            json={"schema_version": 1, "client_id": "c", "events": events},
        )
    assert response.status_code == 400
    assert [item["code"] for item in response.json()["rejected"]] == ["invalid_payload"]


def test_ingest_disabled_returns_503_with_a_stable_code(tmp_path: Path) -> None:
    with GatewayServer(tmp_path, enabled=False) as server, server.client() as client:
        response = client.post(
            "/api/conversations/events",
            json=event(event_id="rmb1_" + "8" * 40),
        )
    assert response.status_code == 503
    assert response.json()["rejected"][0]["code"] == "ingest_disabled"


# ---------------------------------------------------------------------------
# session end / snapshot / stats
# ---------------------------------------------------------------------------


def test_session_end_endpoint(gateway: GatewayServer) -> None:
    payload = event(event_id="rmb1_" + "9" * 40)
    with gateway.client() as client:
        client.post("/api/conversations/events", json=payload)
        response = client.post(
            "/api/conversations/session-end",
            json={
                "schema_version": 1,
                "client_id": "c",
                "source_system": "codex",
                "session_id": "session-1",
                "conversation_id": "session-1",
                "observed_message_count": 3,
                "last_message_id": "msg-3",
            },
        )
    assert response.status_code == 200
    body = response.json()
    assert body["stored_message_count"] == 1
    assert body["missing_event_count"] == 2
    assert body["reconciliation_required"] is True


def test_snapshot_endpoint_reconciles_a_whole_conversation(gateway: GatewayServer) -> None:
    with gateway.client() as client:
        response = client.post(
            "/api/conversations/snapshot",
            json={
                "schema_version": 1,
                "client_id": "c",
                "source_system": "claude-code",
                "session_id": "claude-1",
                "conversation_id": "claude-1",
                "title": "Fe stock preparation",
                "messages": [
                    {"role": "user", "content": "之前 Fe 的硝酸溶液怎么配的？", "message_id": "m1"},
                    {
                        "role": "assistant",
                        "content": "Fe3+ 储备液为 10 mM，介质为 0.1 M HNO3。",
                        "message_id": "m2",
                    },
                ],
                "ended": True,
            },
        )
    assert response.status_code == 200
    body = response.json()
    assert len(body["accepted"]) == 2
    assert body["session"]["stored_message_count"] == 2
    assert body["session"]["reconciliation_required"] is False


def test_snapshot_requires_messages(gateway: GatewayServer) -> None:
    with gateway.client() as client:
        response = client.post(
            "/api/conversations/snapshot",
            json={
                "schema_version": 1,
                "source_system": "codex",
                "session_id": "s",
                "messages": [],
            },
        )
    assert response.status_code == 400
    assert response.json()["rejected"][0]["code"] == "invalid_payload"


def test_stats_endpoint_reports_counters(gateway: GatewayServer) -> None:
    payload = event(event_id="rmb1_" + "A" * 40)
    with gateway.client() as client:
        client.post("/api/conversations/events", json=payload)
        client.post("/api/conversations/events", json=payload)
        response = client.get("/api/conversations/ingest/stats")
    body = response.json()
    assert body["enabled"] is True
    assert body["schema_version"] == 1
    assert body["accepted_total"] == 1
    assert body["duplicate_total"] == 1
    assert body["source_distribution"] == {"codex": 1}


def test_health_reports_ingest_state(gateway: GatewayServer) -> None:
    with httpx.Client(base_url=gateway.url, timeout=15, trust_env=False) as client:
        health = client.get("/health")
    assert health.status_code == 200
    ingest = health.json()["conversation_ingest"]
    assert ingest["enabled"] is True
    assert ingest["endpoint"] == "/api/conversations/events/batch"
    assert ingest["accepted_total"] == 0


# ---------------------------------------------------------------------------
# the MCP read path still works and can see ingested content
# ---------------------------------------------------------------------------


def test_ingested_content_is_visible_through_the_mcp_read_path(gateway: GatewayServer) -> None:
    payload = event(
        event_id="rmb1_" + "B" * 40,
        content="The Fe3+ stock concentration is 10 mM in 0.1 M HNO3.",
    )
    with gateway.client() as client:
        assert client.post("/api/conversations/events", json=payload).status_code == 200

    # The read path is the same retrieval service the MCP tools call.
    search = gateway.service.conversation_retrieval.search("Fe3+ stock concentration")
    assert search["count"] == 1
    recall = gateway.service.conversation_retrieval.recall("Fe3+ stock concentration")
    assert len(recall["items"]) == 1
    note_path = search["results"][0]["vault_path"]
    assert "10 mM" in gateway.service.conversation_retrieval.read(note_path)["content"]


def test_mcp_http_transport_still_serves_tools(tmp_path: Path) -> None:
    """The ingest routes must not have broken the Streamable HTTP MCP surface."""
    import anyio
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    # No token: the MCP SDK client does not send one, and loopback access is
    # allowed when no token is configured.
    with GatewayServer(tmp_path, token=None) as server:

        async def exercise() -> None:
            async with (
                streamable_http_client(f"{server.url}/mcp") as (read_stream, write_stream),
                ClientSession(read_stream, write_stream) as session,
            ):
                await session.initialize()
                tools = await session.list_tools()
                names = {tool.name for tool in tools.tools}
                assert {"conversation_search", "conversation_recall", "conversation_read"} <= names

        anyio.run(exercise)
