import json
import sqlite3

import anyio
import pytest
from starlette.applications import Starlette
from starlette.responses import Response
from starlette.routing import Route
from starlette.testclient import TestClient

from research_memory_gateway.admin import BackfillOptions, backfill_embeddings, inspect_sqlite_db
from research_memory_gateway.backends import SQLiteMemoryBackend
from research_memory_gateway.config import (
    AppConfig,
    AuthStore,
    EmbeddingConfig,
    RerankConfig,
    RetrievalConfig,
    RuntimeConfigResolver,
    SecretStore,
    WebConfigStore,
    WebRuntimeConfig,
)
from research_memory_gateway.models import (
    ExportFormat,
    MemoryStatus,
    MemoryType,
    ProposalStatus,
    ResearchMemory,
)
from research_memory_gateway.nocturne import NocturneReservedConnector
from research_memory_gateway.retrieval import EmbeddingClient, RerankClient
from research_memory_gateway.server import BearerAuthMiddleware, build_mcp
from research_memory_gateway.service import ResearchMemoryService
from research_memory_gateway.taxonomy import (
    ACTIONABLE_PLAN_STATUSES,
    MEMORY_TYPES,
    PLAN_REQUIRED_MEMORY_TYPES,
    PLAN_STATUSES,
    PROPOSAL_STATUSES,
)
from research_memory_gateway.webui.app import build_webui_app


class FakeEmbeddingClient:
    enabled = True

    def embed(self, text: str) -> list[float] | None:
        if "photocatalysis" in text.lower():
            return [0.0, 1.0]
        return [1.0, 0.0]


class FakeRerankClient:
    enabled = True

    def rerank(self, query: str, documents: list[str], *, top_n: int) -> list[tuple[int, float]]:
        return [(index, 10.0 - index) for index in reversed(range(len(documents)))][:top_n]


class FailingEmbeddingClient:
    enabled = True
    last_error = "http_error"

    def embed(self, text: str) -> list[float] | None:
        return None


class FailingRerankClient:
    enabled = True
    last_error = "http_error"

    def rerank(self, query: str, documents: list[str], *, top_n: int) -> list[tuple[int, float]]:
        return []


class SlowEmbeddingClient:
    enabled = True
    last_error = None

    def __init__(self) -> None:
        self.calls = 0

    def embed(self, text: str) -> list[float] | None:
        import time

        self.calls += 1
        time.sleep(0.05)
        return [1.0, 0.0]


def write_config(path, db_path, *, embedding_enabled: bool = False) -> None:
    path.write_text(
        "\n".join(
            [
                "backend:",
                "  type: sqlite",
                f"  sqlite_path: {db_path.as_posix()}",
                "retrieval:",
                "  mode: hybrid",
                "  embedding:",
                f"    enabled: {str(embedding_enabled).lower()}",
            ]
        ),
        encoding="utf-8",
    )


def test_evidence_backed_claim_requires_evidence_id() -> None:
    with pytest.raises(ValueError):
        ResearchMemory.model_validate(
            {
                "topic": "Hg2+ fluorescence probe",
                "memory_type": "literature_review",
                "title": "Hg2+ probe route",
                "summary": "Short summary",
                "claims": [
                    {
                        "claim": "Sulfur doping improves Hg2+ affinity.",
                        "verification_status": "evidence_backed",
                        "evidence_ids": [],
                    }
                ],
            }
        )


def test_memory_accepts_unverified_claim_without_evidence() -> None:
    memory = ResearchMemory.model_validate(
        {
            "topic": "Hg2+ fluorescence probe",
            "memory_type": "literature_review",
            "title": "Hg2+ probe route",
            "summary": "Short summary",
            "claims": [
                {
                    "claim": "Sulfur doping may improve Hg2+ affinity.",
                    "verification_status": "unverified",
                    "evidence_ids": [],
                }
            ],
        }
    )
    assert memory.claims[0].verification_status == "unverified"


def test_sqlite_search_handles_hyphenated_scientific_terms(tmp_path) -> None:
    backend = SQLiteMemoryBackend(str(tmp_path / "memory.db"))
    memory = ResearchMemory.model_validate(
        {
            "project": "demo",
            "topic": "Hg2+ fluorescence probe",
            "memory_type": "literature_review",
            "title": "Sulfur-doped carbon dots",
            "summary": "Sulfur-doped carbon dots may detect Hg2+.",
        }
    )
    backend.save(memory)

    results = backend.search("sulfur-doped Hg2+", project="demo")

    assert len(results) == 1
    assert results[0].memory.memory_id == memory.memory_id


def test_sqlite_hybrid_search_uses_vector_candidates(tmp_path) -> None:
    retrieval = RetrievalConfig(mode="hybrid", embedding={"enabled": True})
    backend = SQLiteMemoryBackend(str(tmp_path / "memory.db"), retrieval)
    backend.embedding_client = FakeEmbeddingClient()
    memory = ResearchMemory.model_validate(
        {
            "project": "demo",
            "topic": "Mercury ion probe",
            "memory_type": "material_system",
            "title": "Soft acid capture route",
            "summary": "Soft-acid binding route for mercury ions.",
        }
    )
    backend.save(memory)

    results = backend.search("cadmium selectivity", project="demo")

    assert len(results) == 1
    assert results[0].memory.memory_id == memory.memory_id
    assert results[0].match_reason.startswith("vector:cosine=")


def test_sqlite_hybrid_search_can_rerank_candidates(tmp_path) -> None:
    retrieval = RetrievalConfig(
        mode="hybrid",
        embedding={"enabled": True},
        rerank={"enabled": True},
        rerank_candidate_limit=2,
    )
    backend = SQLiteMemoryBackend(str(tmp_path / "memory.db"), retrieval)
    backend.embedding_client = FakeEmbeddingClient()
    backend.rerank_client = FakeRerankClient()
    first = ResearchMemory.model_validate(
        {
            "project": "demo",
            "topic": "Mercury ion probe",
            "memory_type": "material_system",
            "title": "First mercury route",
            "summary": "Soft-acid binding route for mercury ions.",
        }
    )
    second = ResearchMemory.model_validate(
        {
            "project": "demo",
            "topic": "Mercury ion probe",
            "memory_type": "material_system",
            "title": "Second mercury route",
            "summary": "Alternative route for mercury ions.",
        }
    )
    backend.save(first)
    backend.save(second)

    results = backend.search("Hg2+ affinity", project="demo", limit=2)

    assert [result.memory.memory_id for result in results] == [second.memory_id, first.memory_id]
    assert all("rerank" in result.match_reason for result in results)


def test_embedding_client_extracts_openai_data_embedding() -> None:
    client = EmbeddingClient(EmbeddingConfig(enabled=True))

    vector = client._extract_vector({"data": [{"embedding": ["0.1", 0.2, 0.3]}]})

    assert vector == [0.1, 0.2, 0.3]


def test_embedding_client_extracts_top_level_embedding() -> None:
    client = EmbeddingClient(EmbeddingConfig(enabled=True))

    vector = client._extract_vector({"embedding": [1, 2, 3]})

    assert vector == [1.0, 2.0, 3.0]


def test_embedding_client_tries_v1_path_when_root_embeddings_404(monkeypatch) -> None:
    class FakeResponse:
        def __init__(self, status_code: int, payload: dict | None = None) -> None:
            self.status_code = status_code
            self.payload = payload or {}

        def raise_for_status(self) -> None:
            if self.status_code >= 400:
                raise RuntimeError("request failed")

        def json(self) -> dict:
            return self.payload

    class FakeClient:
        calls: list[str] = []

        def __init__(self, timeout: float) -> None:
            self.timeout = timeout

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def post(self, url: str, json: dict, headers: dict) -> FakeResponse:
            self.calls.append(url)
            if url.endswith("/embeddings") and not url.endswith("/v1/embeddings"):
                return FakeResponse(404)
            return FakeResponse(200, {"data": [{"embedding": [0.4, 0.5]}]})

    monkeypatch.setattr("research_memory_gateway.retrieval.httpx.Client", FakeClient)
    monkeypatch.setenv("EMBEDDING_BASE_URL", "http://models.local")
    client = EmbeddingClient(EmbeddingConfig(enabled=True, max_retries=0))

    vector = client.embed("query")

    assert vector == [0.4, 0.5]
    assert FakeClient.calls == [
        "http://models.local/embeddings",
        "http://models.local/v1/embeddings",
    ]


def test_rerank_client_extracts_results_index_relevance_score() -> None:
    client = RerankClient(RerankConfig(enabled=True))

    scores = client._extract_scores({"results": [{"index": "1", "relevance_score": "0.91"}]})

    assert scores == [(1, 0.91)]


def test_rerank_client_extracts_document_index_score() -> None:
    client = RerankClient(RerankConfig(enabled=True))

    scores = client._extract_scores({"results": [{"document": {"index": "2"}, "score": "0.81"}]})

    assert scores == [(2, 0.81)]


def test_rerank_client_extracts_data_results() -> None:
    client = RerankClient(RerankConfig(enabled=True))

    scores = client._extract_scores({"data": [{"index": 0, "score": 0.71}]})

    assert scores == [(0, 0.71)]


def test_hybrid_search_falls_back_to_fts_when_embedding_fails(tmp_path) -> None:
    retrieval = RetrievalConfig(mode="hybrid", embedding={"enabled": True})
    backend = SQLiteMemoryBackend(str(tmp_path / "memory.db"), retrieval)
    backend.embedding_client = FailingEmbeddingClient()
    memory = ResearchMemory.model_validate(
        {
            "project": "demo",
            "topic": "Mercury ion probe",
            "memory_type": "material_system",
            "title": "Mercury affinity route",
            "summary": "Soft-acid binding route for mercury ions.",
        }
    )
    backend.save(memory)

    results = backend.search("mercury", project="demo")

    assert len(results) == 1
    assert results[0].memory.memory_id == memory.memory_id
    assert results[0].match_reason.startswith("fts:rank_position=")


def test_hybrid_search_returns_unreranked_results_when_rerank_fails(tmp_path) -> None:
    retrieval = RetrievalConfig(
        mode="hybrid",
        embedding={"enabled": True},
        rerank={"enabled": True},
    )
    backend = SQLiteMemoryBackend(str(tmp_path / "memory.db"), retrieval)
    backend.embedding_client = FakeEmbeddingClient()
    backend.rerank_client = FailingRerankClient()
    memory = ResearchMemory.model_validate(
        {
            "project": "demo",
            "topic": "Mercury ion probe",
            "memory_type": "material_system",
            "title": "Mercury affinity route",
            "summary": "Soft-acid binding route for mercury ions.",
        }
    )
    backend.save(memory)

    results = backend.search("mercury", project="demo")

    assert len(results) == 1
    assert results[0].memory.memory_id == memory.memory_id
    assert "rerank" not in results[0].match_reason


def test_vector_search_skips_dimension_mismatch_and_reports_health(tmp_path) -> None:
    retrieval = RetrievalConfig(mode="hybrid", embedding={"enabled": True})
    backend = SQLiteMemoryBackend(str(tmp_path / "memory.db"), retrieval)
    backend.embedding_client = FakeEmbeddingClient()
    memory = ResearchMemory.model_validate(
        {
            "project": "demo",
            "topic": "Mercury ion probe",
            "memory_type": "material_system",
            "title": "Mercury affinity route",
            "summary": "Soft-acid binding route for mercury ions.",
        }
    )
    backend.save(memory)
    with backend._connect() as connection:
        connection.execute(
            "UPDATE memory_embeddings SET embedding = ? WHERE memory_id = ?",
            ("[1.0, 0.0, 0.0]", memory.memory_id),
        )

    results = backend.search("cadmium selectivity", project="demo")
    health = backend.retrieval_health()

    assert results == []
    assert health["last_vector_dimension_mismatches"] == 1
    assert health["stored_embedding_dimensions"] == {3: 1}


def test_inspect_db_reports_counts(tmp_path) -> None:
    backend = SQLiteMemoryBackend(str(tmp_path / "memory.db"))
    memory = ResearchMemory.model_validate(
        {
            "project": "demo",
            "topic": "Mercury ion probe",
            "memory_type": "material_system",
            "title": "Mercury route",
            "summary": "Soft-acid binding route.",
        }
    )
    backend.save(memory)

    result = inspect_sqlite_db(str(tmp_path / "memory.db"))

    assert result["memory_count"] == 1
    assert result["embedding_count"] == 0
    assert result["missing_embedding_count"] == 1
    assert result["projects"] == {"demo": 1}


def test_backfill_embeddings_dry_run_does_not_write(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("EMBEDDING_BASE_URL", "http://unused.local")
    db_path = tmp_path / "memory.db"
    config_path = tmp_path / "config.yaml"
    write_config(config_path, db_path, embedding_enabled=True)
    backend = SQLiteMemoryBackend(str(db_path))
    backend.save(
        ResearchMemory.model_validate(
            {
                "project": "demo",
                "topic": "Mercury ion probe",
                "memory_type": "material_system",
                "title": "Mercury route",
                "summary": "Soft-acid binding route.",
            }
        )
    )

    result = backfill_embeddings(str(config_path), BackfillOptions(dry_run=True))
    inspection = inspect_sqlite_db(str(db_path))

    assert result["would_backfill"] == 1
    assert inspection["embedding_count"] == 0


def test_backfill_embeddings_respects_project_filter(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("EMBEDDING_BASE_URL", "http://unused.local")
    db_path = tmp_path / "memory.db"
    config_path = tmp_path / "config.yaml"
    write_config(config_path, db_path, embedding_enabled=True)
    backend = SQLiteMemoryBackend(str(db_path))
    backend.embedding_client = FakeEmbeddingClient()
    for project in ["demo", "other"]:
        backend.save(
            ResearchMemory.model_validate(
                {
                    "project": project,
                    "topic": "Mercury ion probe",
                    "memory_type": "material_system",
                    "title": f"{project} route",
                    "summary": "Soft-acid binding route.",
                }
            )
        )
    with backend._connect() as connection:
        connection.execute("DELETE FROM memory_embeddings")

    result = backfill_embeddings(
        str(config_path), BackfillOptions(project="demo", dry_run=True), embedding_client=FakeEmbeddingClient()
    )

    assert result["matched_memories"] == 1
    assert result["would_backfill"] == 1


def test_backfill_embeddings_force_rebuilds_existing_vector(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("EMBEDDING_BASE_URL", "http://unused.local")
    db_path = tmp_path / "memory.db"
    config_path = tmp_path / "config.yaml"
    write_config(config_path, db_path, embedding_enabled=True)
    backend = SQLiteMemoryBackend(str(db_path))
    memory = ResearchMemory.model_validate(
        {
            "project": "demo",
            "topic": "Mercury ion probe",
            "memory_type": "material_system",
            "title": "Mercury route",
            "summary": "Soft-acid binding route.",
        }
    )
    backend.save(memory)
    with backend._connect() as connection:
        connection.execute(
            "INSERT INTO memory_embeddings(memory_id, embedding, updated_at) VALUES (?, ?, ?)",
            (memory.memory_id, "[0.0, 1.0]", memory.updated_at),
        )

    result_without_force = backfill_embeddings(
        str(config_path), BackfillOptions(), embedding_client=FakeEmbeddingClient()
    )
    result_with_force = backfill_embeddings(
        str(config_path), BackfillOptions(force=True), embedding_client=FakeEmbeddingClient()
    )

    assert result_without_force["skipped_existing"] == 1
    assert result_with_force["backfilled"] == 1


def test_bearer_auth_middleware_rejects_missing_token() -> None:
    def endpoint(request):
        return Response("ok")

    app = Starlette(routes=[Route("/sse", endpoint)])
    app.add_middleware(BearerAuthMiddleware, token="secret")
    client = TestClient(app)

    assert client.get("/sse").status_code == 401
    assert client.get("/sse", headers={"Authorization": "Bearer secret"}).status_code == 200


def test_bearer_auth_middleware_allows_loopback_when_master_token_unset() -> None:
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})

    async def run(client_host: str) -> int:
        messages = []
        middleware = BearerAuthMiddleware(app, token=None)
        scope = {
            "type": "http",
            "path": "/sse",
            "headers": [],
            "method": "GET",
            "scheme": "http",
            "server": ("testserver", 80),
            "client": (client_host, 50000),
            "root_path": "",
            "query_string": b"",
        }

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)

        await middleware(scope, receive, send)
        return messages[0]["status"]

    assert anyio.run(run, "127.0.0.1") == 200
    assert anyio.run(run, "::1") == 200
    assert anyio.run(run, "192.168.1.10") == 401


def test_mcp_uses_configured_remote_host_for_transport_security(tmp_path) -> None:
    config = AppConfig()
    config.server.host = "0.0.0.0"
    config.server.port = 8787
    config.backend.sqlite_path = str(tmp_path / "memory.db")

    mcp = build_mcp(config)

    assert mcp.settings.host == "0.0.0.0"
    assert mcp.settings.port == 8787
    assert mcp.settings.transport_security is None


def test_bearer_auth_middleware_preserves_streaming_asgi_messages() -> None:
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.debug", "info": {"template": None}})
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})

    async def run() -> list[dict]:
        messages = []
        middleware = BearerAuthMiddleware(app, token="secret")
        scope = {
            "type": "http",
            "path": "/sse",
            "headers": [(b"authorization", b"Bearer secret")],
            "method": "GET",
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("testclient", 50000),
            "root_path": "",
            "query_string": b"",
        }

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)

        await middleware(scope, receive, send)
        return messages

    assert [message["type"] for message in anyio.run(run)] == [
        "http.response.start",
        "http.response.debug",
        "http.response.body",
    ]


def test_service_health_reports_sqlite_writable(tmp_path) -> None:
    from research_memory_gateway.config import AppConfig

    config = AppConfig()
    config.backend.sqlite_path = str(tmp_path / "memory.db")
    backend = SQLiteMemoryBackend(config.backend.sqlite_path)
    service = ResearchMemoryService(config, backend)

    health = service.health()

    assert health["status"] == "ok"
    assert health["backend"]["backend"] == "sqlite"
    assert health["backend"]["sqlite_writable"] is True


def make_service(tmp_path):
    from research_memory_gateway.config import AppConfig

    config = AppConfig()
    config.backend.sqlite_path = str(tmp_path / "memory.db")
    backend = SQLiteMemoryBackend(config.backend.sqlite_path)
    return ResearchMemoryService(config, backend)


def test_delete_research_memory_requires_confirmation(tmp_path) -> None:
    service = make_service(tmp_path)
    memory = service.save_research_memory(
        user_confirmed=True,
        memory={
            "project": "demo",
            "topic": "Mercury ion probe",
            "memory_type": "material_system",
            "title": "Mercury route",
            "summary": "Soft-acid binding route.",
        },
    )

    with pytest.raises(PermissionError):
        service.delete_research_memory(memory.memory_id, user_confirmed=False)

    assert service.get_research_memory(memory.memory_id).memory_id == memory.memory_id


def test_service_marks_default_claim_with_evidence_as_evidence_backed(tmp_path) -> None:
    service = make_service(tmp_path)
    memory = service.save_research_memory(
        user_confirmed=True,
        memory={
            "project": "demo",
            "topic": "Mercury ion probe",
            "memory_type": "paper_note",
            "title": "Mercury paper",
            "summary": "A paper reports sulfur-doped probes for mercury ions.",
            "evidence": [{"evidence_id": "ev_1", "quote": "Sulfur doping improves Hg2+ affinity."}],
            "claims": [{"claim": "Sulfur doping improves Hg2+ affinity.", "evidence_ids": ["ev_1"]}],
        },
    )

    assert memory.claims[0].verification_status == "evidence_backed"


def test_service_preserves_explicit_unverified_claim_with_evidence(tmp_path) -> None:
    service = make_service(tmp_path)
    memory = service.save_research_memory(
        user_confirmed=True,
        memory={
            "project": "demo",
            "topic": "Mercury ion probe",
            "memory_type": "paper_note",
            "title": "Mercury paper",
            "summary": "A paper reports sulfur-doped probes for mercury ions.",
            "evidence": [{"evidence_id": "ev_1", "quote": "Sulfur doping improves Hg2+ affinity."}],
            "claims": [
                {
                    "claim": "Sulfur doping improves Hg2+ affinity.",
                    "verification_status": "unverified",
                    "evidence_ids": ["ev_1"],
                }
            ],
        },
    )

    assert memory.claims[0].verification_status == "unverified"


def test_validate_research_memory_for_write_applies_write_policy(tmp_path) -> None:
    service = make_service(tmp_path)

    parsed = service.validate_research_memory_for_write(
        {
            "project": "demo",
            "topic": "Mercury ion probe",
            "memory_type": "paper_note",
            "title": "Mercury paper",
            "summary": "A paper reports sulfur-doped probes for mercury ions.",
            "evidence": [{"evidence_id": "ev_1", "quote": "Sulfur doping improves Hg2+ affinity."}],
            "claims": [{"claim": "Sulfur doping improves Hg2+ affinity.", "evidence_ids": ["ev_1"]}],
        }
    )

    assert parsed.claims[0].verification_status == "evidence_backed"
    with pytest.raises(ValueError, match="summary exceeds max_summary_chars"):
        service.validate_research_memory_for_write(
            {
                "project": "demo",
                "topic": "Mercury ion probe",
                "memory_type": "paper_note",
                "title": "Mercury paper",
                "summary": "x" * (service.config.memory.max_summary_chars + 1),
            }
        )


def test_taxonomy_includes_chinese_labels(tmp_path) -> None:
    service = make_service(tmp_path)

    taxonomy = service.get_memory_taxonomy()

    workflow_plan = next(item for item in taxonomy["memory_types"] if item["key"] == "workflow_plan")
    accepted = next(item for item in taxonomy["plan_statuses"] if item["key"] == "accepted")
    pending = next(item for item in taxonomy["proposal_statuses"] if item["key"] == "pending")
    assert workflow_plan["label_zh"] == "工作流规划"
    assert workflow_plan["requires_plan_status"] is True
    assert accepted["label_zh"] == "已确认"
    assert accepted["actionable"] is True
    assert pending["label_zh"] == "待审"


def test_taxonomy_keys_match_model_enums_and_rules() -> None:
    assert {item["key"] for item in MEMORY_TYPES} == {item.value for item in MemoryType}
    assert {item["key"] for item in PROPOSAL_STATUSES} == {item.value for item in ProposalStatus}
    assert PLAN_REQUIRED_MEMORY_TYPES == {
        item["key"] for item in MEMORY_TYPES if item.get("requires_plan_status")
    }
    assert ACTIONABLE_PLAN_STATUSES == {item["key"] for item in PLAN_STATUSES if item.get("actionable")}


def test_plan_memories_require_plan_status(tmp_path) -> None:
    service = make_service(tmp_path)

    with pytest.raises(ValueError, match="workflow_plan requires metadata.plan_status"):
        service.save_research_memory(
            user_confirmed=True,
            memory={
                "project": "research-memory-gateway",
                "topic": "Agent memory policy",
                "memory_type": "workflow_plan",
                "title": "Chat confirmation memory flow",
                "summary": "Agents propose memory candidates and save only after user confirmation.",
            },
        )

    with pytest.raises(ValueError, match="experiment_plan requires metadata.plan_status"):
        service.save_research_memory(
            user_confirmed=True,
            memory={
                "project": "demo",
                "topic": "Hg experiment",
                "memory_type": "experiment_plan",
                "title": "Probe validation plan",
                "summary": "Validate selectivity against competing ions.",
            },
        )


def test_workflow_plan_accepts_valid_plan_status_and_type(tmp_path) -> None:
    service = make_service(tmp_path)

    saved = service.save_research_memory(
        user_confirmed=True,
        memory={
            "project": "research-memory-gateway",
            "topic": "Agent memory policy",
            "memory_type": "workflow_plan",
            "title": "Confirmed memory proposal flow",
            "summary": "Agents should save memories only after explicit user confirmation.",
            "metadata": {"plan_status": "accepted", "plan_type": "agent_memory_policy"},
        },
    )

    assert saved.memory_type.value == "workflow_plan"
    assert saved.metadata["plan_status"] == "accepted"


def test_invalid_plan_type_is_rejected(tmp_path) -> None:
    service = make_service(tmp_path)

    with pytest.raises(ValueError, match="metadata.plan_type must be one of"):
        service.save_research_memory(
            user_confirmed=True,
            memory={
                "project": "research-memory-gateway",
                "topic": "Agent memory policy",
                "memory_type": "workflow_plan",
                "title": "Invalid plan type",
                "summary": "Invalid plan type should fail.",
                "metadata": {"plan_status": "accepted", "plan_type": "misc"},
            },
        )


def test_direct_confirmed_save_records_proposal_snapshot(tmp_path) -> None:
    service = make_service(tmp_path)

    saved = service.save_research_memory(
        user_confirmed=True,
        memory={
            "project": "research-memory-gateway",
            "topic": "Agent memory policy",
            "memory_type": "workflow_plan",
            "title": "User-confirmed save audit",
            "summary": "A chat-confirmed save records confirmation metadata.",
            "metadata": {"plan_status": "accepted", "plan_type": "agent_memory_policy"},
        },
        confirmation={"source": "chat", "text": "可以，保存", "confirmed_by": "user"},
    )

    proposals = service.list_memory_proposals(status="saved")
    assert len(proposals) == 1
    proposal = proposals[0]
    versions = service.get_memory_proposal_versions(proposal.proposal_id)
    assert proposal.saved_memory_id == saved.memory_id
    assert versions[0]["confirmation"]["source"] == "chat"
    assert versions[0]["confirmation"]["text"] == "可以，保存"
    assert saved.metadata["saved_from_proposal_id"] == proposal.proposal_id
    assert saved.metadata["saved_from_proposal_version"] == 1
    assert saved.metadata["save_confirmation"]["confirmed_by"] == "user"


def test_only_pending_or_approved_proposals_can_be_saved(tmp_path) -> None:
    service = make_service(tmp_path)
    disallowed_statuses = [
        "needs_edit",
        "rejected",
        "expired",
    ]

    for proposal_status in disallowed_statuses:
        proposal = service.propose_save(
            reason=f"{proposal_status} candidate",
            suggested_memory={
                "project": "research-memory-gateway",
                "topic": "Agent memory policy",
                "memory_type": "workflow_plan",
                "title": f"{proposal_status} proposal",
                "summary": "Only pending or approved proposals should be saveable.",
                "metadata": {"plan_status": "draft", "plan_type": "agent_memory_policy"},
            },
            check_overlap=False,
        )
        service.update_memory_proposal_status(
            proposal.proposal_id,
            proposal_status,
            reason="test transition",
            user_confirmed=True,
        )

        with pytest.raises(ValueError, match="Only .* memory proposals can be saved"):
            service.save_research_memory(user_confirmed=True, proposal_id=proposal.proposal_id)


def test_saved_proposals_are_terminal(tmp_path) -> None:
    service = make_service(tmp_path)
    proposal = service.propose_save(
        reason="saveable candidate",
        suggested_memory={
            "project": "research-memory-gateway",
            "topic": "Agent memory policy",
            "memory_type": "workflow_plan",
            "title": "Terminal saved proposal",
            "summary": "Saved proposals cannot later become rejected or expired.",
            "metadata": {"plan_status": "accepted", "plan_type": "agent_memory_policy"},
        },
        check_overlap=False,
    )
    service.save_research_memory(
        user_confirmed=True,
        proposal_id=proposal.proposal_id,
        confirmation={"source": "chat", "text": "save", "confirmed_by": "user"},
    )

    with pytest.raises(ValueError, match="terminal"):
        service.update_memory_proposal_status(
            proposal.proposal_id,
            "rejected",
            reason="should not be allowed",
            user_confirmed=True,
        )


def test_rejected_and_expired_proposals_are_terminal(tmp_path) -> None:
    service = make_service(tmp_path)
    for initial_status, next_status in [("rejected", "pending"), ("expired", "approved")]:
        proposal = service.propose_save(
            reason=f"{initial_status} candidate",
            suggested_memory={
                "project": "research-memory-gateway",
                "topic": "Agent memory policy",
                "memory_type": "workflow_plan",
                "title": f"{initial_status} terminal proposal",
                "summary": "Rejected and expired proposals are terminal.",
                "metadata": {"plan_status": "draft", "plan_type": "agent_memory_policy"},
            },
            check_overlap=False,
        )
        service.update_memory_proposal_status(
            proposal.proposal_id,
            initial_status,
            reason="terminal transition",
            user_confirmed=True,
        )

        with pytest.raises(ValueError, match="terminal"):
            service.update_memory_proposal_status(
                proposal.proposal_id,
                next_status,
                reason="should not be allowed",
                user_confirmed=True,
            )


def test_update_research_memory_refreshes_search_index(tmp_path) -> None:
    service = make_service(tmp_path)
    memory = service.save_research_memory(
        user_confirmed=True,
        memory={
            "project": "demo",
            "topic": "Mercury ion probe",
            "memory_type": "material_system",
            "title": "Mercury route",
            "summary": "Soft-acid binding route.",
        },
    )

    updated = service.update_research_memory(
        memory.memory_id,
        {"title": "Cadmium selectivity route", "summary": "Cadmium selectivity finding."},
        user_confirmed=True,
    )
    results = service.search_research_memory(query="cadmium", project="demo")

    assert updated.title == "Cadmium selectivity route"
    assert [result.memory.memory_id for result in results] == [memory.memory_id]


def test_update_research_memory_marks_existing_embedding_for_backfill_when_disabled(tmp_path) -> None:
    service = make_service(tmp_path)
    memory = service.save_research_memory(
        user_confirmed=True,
        memory={
            "project": "demo",
            "topic": "Mercury ion probe",
            "memory_type": "material_system",
            "title": "Mercury route",
            "summary": "Soft-acid binding route.",
        },
    )
    with service.backend._connect() as connection:
        connection.execute(
            "INSERT INTO memory_embeddings(memory_id, embedding, updated_at) VALUES (?, ?, ?)",
            (memory.memory_id, "[1.0, 0.0]", memory.updated_at),
        )

    service.update_research_memory(
        memory.memory_id,
        {"summary": "Updated cadmium route."},
        user_confirmed=True,
    )
    audit = service.audit_database_integrity()

    assert audit["embedding_backfill_needed"] == [
        {
            "memory_id": memory.memory_id,
            "reason": "embedding_disabled_after_update",
            "updated_at": service.get_research_memory(memory.memory_id).updated_at,
        }
    ]


def test_merge_research_memories_preserves_sources_and_supersedes_old(tmp_path) -> None:
    service = make_service(tmp_path)
    first = service.save_research_memory(
        user_confirmed=True,
        memory={
            "project": "demo",
            "topic": "Mercury ion probe",
            "memory_type": "paper_note",
            "title": "Paper one",
            "summary": "First finding.",
            "source_refs": [{"source_type": "doi", "doi": "10.1/demo", "excerpt": "first"}],
        },
    )
    second = service.save_research_memory(
        user_confirmed=True,
        memory={
            "project": "demo",
            "topic": "Mercury ion probe",
            "memory_type": "paper_note",
            "title": "Paper two",
            "summary": "Second finding.",
            "source_refs": [{"source_type": "doi", "doi": "10.2/demo", "excerpt": "second"}],
        },
    )

    merged = service.merge_research_memories(
        [first.memory_id, second.memory_id],
        {
            "project": "demo",
            "topic": "Mercury ion probe",
            "memory_type": "literature_review",
            "title": "Merged review",
            "summary": "Merged finding.",
        },
        reason="consolidate notes",
        user_confirmed=True,
    )
    old_first = service.get_research_memory(first.memory_id)

    assert merged.memory_id not in {first.memory_id, second.memory_id}
    assert {ref.excerpt for ref in merged.source_refs} >= {"first", "second", "consolidate notes"}
    assert old_first.tags == ["superseded"]


def test_schema_migrations_table_is_initialized(tmp_path) -> None:
    backend = SQLiteMemoryBackend(str(tmp_path / "memory.db"))

    with backend._connect() as connection:
        rows = connection.execute("SELECT version, name FROM schema_migrations ORDER BY version").fetchall()

    assert (rows[0]["version"], rows[0]["name"]) == (1, "initial_sqlite_memory_schema")
    assert (rows[1]["version"], rows[1]["name"]) == (2, "webui_memory_lifecycle_and_audit")
    assert (rows[2]["version"], rows[2]["name"]) == (3, "webui_api_keys_and_connections")
    assert (rows[3]["version"], rows[3]["name"]) == (4, "embedding_backfill_state")
    assert (rows[4]["version"], rows[4]["name"]) == (5, "memory_proposals_and_versions")


def test_legacy_database_is_upgraded_without_losing_memories(tmp_path) -> None:
    db_path = tmp_path / "legacy.db"
    memory = ResearchMemory.model_validate(
        {
            "project": "demo",
            "topic": "Hg",
            "memory_type": "paper_note",
            "title": "Legacy memory",
            "summary": "Legacy summary.",
        }
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE memories (
                memory_id TEXT PRIMARY KEY,
                project TEXT NOT NULL,
                topic TEXT NOT NULL,
                memory_type TEXT NOT NULL,
                title TEXT NOT NULL,
                summary TEXT NOT NULL,
                data TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO memories(memory_id, project, topic, memory_type, title, summary, data, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                memory.memory_id,
                memory.project,
                memory.topic,
                memory.memory_type.value,
                memory.title,
                memory.summary,
                json.dumps(memory.model_dump(mode="json")),
                memory.created_at,
                memory.updated_at,
            ),
        )

    backend = SQLiteMemoryBackend(str(db_path))

    assert backend.get(memory.memory_id).title == "Legacy memory"
    with backend._connect() as connection:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(memories)").fetchall()}
        migration_versions = [
            row["version"]
            for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
        ]

    assert {"memory_status", "status_changed_at", "status_change_reason"} <= columns
    assert migration_versions == [1, 2, 3, 4, 5]


def test_integrity_audit_repairs_missing_fts_and_orphan_embeddings(tmp_path) -> None:
    backend = SQLiteMemoryBackend(str(tmp_path / "memory.db"))
    memory = ResearchMemory.model_validate(
        {
            "project": "demo",
            "topic": "Mercury ion probe",
            "memory_type": "material_system",
            "title": "Mercury route",
            "summary": "Soft-acid binding route.",
        }
    )
    backend.save(memory)
    with backend._connect() as connection:
        connection.execute("DELETE FROM memories_fts WHERE memory_id = ?", (memory.memory_id,))
        connection.execute(
            "INSERT INTO memory_embeddings(memory_id, embedding, updated_at) VALUES (?, ?, ?)",
            ("missing-memory", "[1.0, 0.0]", memory.updated_at),
        )

    before = backend.audit_integrity()
    after = backend.audit_integrity(repair_fts=True, repair_orphans=True)
    final = backend.audit_integrity()

    assert before["missing_fts"] == [memory.memory_id]
    assert before["orphan_embeddings"] == ["missing-memory"]
    assert after["repaired_fts"] == 1
    assert after["removed_orphan_embeddings"] == 1
    assert final["missing_fts"] == []
    assert final["orphan_embeddings"] == []


def test_memory_lifecycle_search_defaults_to_active_and_overlap_includes_deleted(tmp_path) -> None:
    service = make_service(tmp_path)
    active = service.save_research_memory(
        user_confirmed=True,
        memory={"project": "demo", "topic": "Hg", "memory_type": "paper_note", "title": "Active mercury", "summary": "Mercury active note."},
    )
    deleted = service.save_research_memory(
        user_confirmed=True,
        memory={"project": "demo", "topic": "Hg", "memory_type": "paper_note", "title": "Deleted mercury", "summary": "Mercury deleted note."},
    )
    service.soft_delete_memory(deleted.memory_id, reason="obsolete", user_confirmed=True)

    search_ids = [result.memory.memory_id for result in service.search_research_memory(query="mercury", project="demo")]
    overlaps = service.check_overlap(query="Deleted mercury", project="demo")

    assert active.memory_id in search_ids
    assert deleted.memory_id not in search_ids
    assert any(item["memory_id"] == deleted.memory_id and item["memory_status"] == "deleted" for item in overlaps)


def test_audit_and_export_default_to_active_with_include_flags(tmp_path) -> None:
    service = make_service(tmp_path)
    active = service.save_research_memory(
        user_confirmed=True,
        memory={"project": "demo", "topic": "Hg", "memory_type": "paper_note", "title": "Active audit", "summary": "Active audit.", "claims": [{"claim": "Needs evidence"}]},
    )
    archived = service.save_research_memory(
        user_confirmed=True,
        memory={"project": "demo", "topic": "Hg", "memory_type": "paper_note", "title": "Archived audit", "summary": "Archived audit.", "claims": [{"claim": "Needs evidence"}]},
    )
    service.archive_memory(archived.memory_id, "old", True)

    default_audit_ids = {item["memory_id"] for item in service.audit_unverified(project="demo")}
    included_audit_ids = {item["memory_id"] for item in service.audit_unverified(project="demo", include_archived=True)}
    exported_default = service.export_memories(ExportFormat.json)
    exported_included = service.export_memories(ExportFormat.json, include_archived=True)

    assert default_audit_ids == {active.memory_id}
    assert included_audit_ids == {active.memory_id, archived.memory_id}
    assert exported_default["count"] == 1
    assert exported_included["count"] == 2


def test_hard_delete_requires_deleted_state_and_removes_embeddings(tmp_path) -> None:
    service = make_service(tmp_path)
    memory = service.save_research_memory(
        user_confirmed=True,
        memory={"project": "demo", "topic": "Hg", "memory_type": "paper_note", "title": "Hard delete", "summary": "Delete target."},
    )
    with pytest.raises(PermissionError):
        service.hard_delete_memory(memory.memory_id, confirm_memory_id=memory.memory_id, current_password_valid=True)
    service.soft_delete_memory(memory.memory_id, reason="remove", user_confirmed=True)
    with service.backend._connect() as connection:
        connection.execute("INSERT INTO memory_embeddings(memory_id, embedding, updated_at) VALUES (?, ?, ?)", (memory.memory_id, "[1.0]", memory.updated_at))

    result = service.hard_delete_memory(memory.memory_id, confirm_memory_id=memory.memory_id, current_password_valid=True, reason="confirmed")

    assert result["deleted"] is True
    assert service.backend.get(memory.memory_id) is None
    with service.backend._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM memory_embeddings WHERE memory_id = ?", (memory.memory_id,)).fetchone()[0] == 0


def test_auth_store_bootstraps_and_initial_password_stops_applying(tmp_path) -> None:
    config = AppConfig()
    config.webui.auth_store_path = str(tmp_path / "auth.json")
    config.webui.initial_password = "first"
    store = AuthStore(config.webui.auth_store_path)

    store.bootstrap(config.webui)
    config.webui.initial_password = "second"

    assert store.verify("first", config.webui) is True
    assert store.verify("second", config.webui) is False
    store.change_password("first", "third")
    assert store.verify("third", config.webui) is True


def test_secret_store_masks_and_requires_key(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("WEBUI_SECRET_KEY", raising=False)
    store = SecretStore(tmp_path / "secrets.json.enc")
    with pytest.raises(RuntimeError):
        store.save_secret("embedding.api_key", "secret-value")
    keyed = SecretStore(tmp_path / "secrets.json.enc", secret_key="dev-key")
    keyed.save_secret("embedding.api_key", "secret-value")

    masked = keyed.masked("embedding.api_key")

    assert masked["configured"] is True
    assert masked["masked"] != "secret-value"
    assert keyed.load()["embedding.api_key"] == "secret-value"


def test_runtime_config_resolver_precedence_and_sources(tmp_path, monkeypatch) -> None:
    config = AppConfig()
    web_store = WebConfigStore(tmp_path / "web_config.yaml")
    web_store.save(WebRuntimeConfig(retrieval={"mode": "hybrid"}, embedding={"enabled": True, "base_url": "http://web.local", "model": "web-model"}))
    secrets_store = SecretStore(tmp_path / "secrets.json.enc", secret_key="dev-key")
    secrets_store.save_secret("embedding.api_key", "stored-key")
    resolver = RuntimeConfigResolver(config, web_store, secrets_store)
    monkeypatch.setenv("EMBEDDING_MODEL", "env-model")

    effective = resolver.effective()

    assert effective["retrieval"]["mode"] == {"value": "hybrid", "source": "web_config"}
    assert effective["embedding"]["base_url"]["source"] == "web_config"
    assert effective["embedding"]["model"] == {"value": "env-model", "source": "env"}
    assert effective["embedding"]["api_key"]["source"] == "secret_store"


def make_webui_client(tmp_path, monkeypatch):
    monkeypatch.setenv("WEBUI_SECRET_KEY", "dev-key")
    config = AppConfig()
    config.backend.sqlite_path = str(tmp_path / "memory.db")
    config.memory.require_user_confirmation = False
    config.webui.enabled = True
    config.webui.initial_password = "admin-pass"
    config.webui.auth_store_path = str(tmp_path / "auth.json")
    config.webui.web_config_path = str(tmp_path / "web_config.yaml")
    config.webui.secret_store_path = str(tmp_path / "secrets.json.enc")
    app = build_webui_app(config)
    client = TestClient(app)
    return client, app


def login_webui(client: TestClient) -> str:
    response = client.post("/admin/api/auth/login", json={"password": "admin-pass"})
    assert response.status_code == 200
    data = response.json()
    token = data["access_token"]
    
    import base64
    import json
    payload_b64 = token.split(".")[1]
    payload_b64 += "=" * ((4 - len(payload_b64) % 4) % 4)
    payload = json.loads(base64.urlsafe_b64decode(payload_b64))
    
    return payload["csrf"]


def test_webui_login_session_csrf_and_memory_api(tmp_path, monkeypatch) -> None:
    client, app = make_webui_client(tmp_path, monkeypatch)
    # The SPA doesn't redirect unauthenticated /admin to /admin/login anymore
    # because routing is handled client-side. The API returns 401.
    token = login_webui(client)
    no_csrf = client.post("/admin/api/memories", json={})
    assert no_csrf.status_code == 403

    created = client.post(
        "/admin/api/memories",
        headers={"x-csrf-token": token},
        json={"project": "demo", "topic": "Hg", "memory_type": "paper_note", "title": "Web memory", "summary": "Created from WebUI.", "confirmed": True},
    )

    assert created.status_code == 201
    assert client.get("/admin/api/memories").json()["items"][0]["title"] == "Web memory"
    saved = created.json()
    proposals = app.state.webui.service.list_memory_proposals(status="saved")
    assert len(proposals) == 1
    assert proposals[0].saved_memory_id == saved["memory_id"]
    assert saved["metadata"]["save_confirmation"]["source"] == "webui"


def test_webui_memory_patch_returns_400_for_invalid_plan_metadata(tmp_path, monkeypatch) -> None:
    client, _app = make_webui_client(tmp_path, monkeypatch)
    token = login_webui(client)
    created = client.post(
        "/admin/api/memories",
        headers={"x-csrf-token": token},
        json={
            "project": "demo",
            "topic": "Agent memory policy",
            "memory_type": "workflow_plan",
            "title": "WebUI patch validation",
            "summary": "Plan memories must keep valid plan metadata.",
            "metadata": {"plan_status": "accepted", "plan_type": "agent_memory_policy"},
            "confirmed": True,
        },
    )

    assert created.status_code == 201
    patched = client.patch(
        f"/admin/api/memories/{created.json()['memory_id']}",
        headers={"x-csrf-token": token},
        json={"metadata": {"plan_type": "agent_memory_policy"}},
    )

    assert patched.status_code == 400
    assert "plan_status" in patched.json()["error"]


def test_webui_taxonomy_and_proposal_api(tmp_path, monkeypatch) -> None:
    client, app = make_webui_client(tmp_path, monkeypatch)
    token = login_webui(client)
    proposal = app.state.webui.service.propose_save(
        reason="agent prepared candidate",
        suggested_memory={
            "project": "research-memory-gateway",
            "topic": "Agent memory policy",
            "memory_type": "workflow_plan",
            "title": "Review queue flow",
            "summary": "Agent proposals can be reviewed in WebUI before saving.",
            "metadata": {"plan_status": "draft", "plan_type": "agent_memory_policy"},
        },
        check_overlap=False,
    )

    taxonomy = client.get("/admin/api/taxonomy")
    proposals = client.get("/admin/api/proposals?status=pending")
    detail = client.get(f"/admin/api/proposals/{proposal.proposal_id}")
    saved = client.post(
        f"/admin/api/proposals/{proposal.proposal_id}/save",
        headers={"x-csrf-token": token},
        json={"text": "WebUI confirmed"},
    )

    assert taxonomy.status_code == 200
    assert any(item["label_zh"] == "工作流规划" for item in taxonomy.json()["memory_types"])
    assert proposals.json()["items"][0]["proposal_id"] == proposal.proposal_id
    assert detail.json()["versions"][0]["version"] == 1
    assert saved.status_code == 201
    assert saved.json()["metadata"]["save_confirmation"]["text"] == "WebUI confirmed"


def test_webui_config_secret_masking_and_env_override(tmp_path, monkeypatch) -> None:
    client, _app = make_webui_client(tmp_path, monkeypatch)
    token = login_webui(client)
    patched = client.patch(
        "/admin/api/config/web-config",
        headers={"x-csrf-token": token},
        json={"retrieval": {"mode": "hybrid"}, "embedding": {"enabled": True, "base_url": "http://web.local"}},
    )
    secret = client.patch(
        "/admin/api/config/secrets",
        headers={"x-csrf-token": token},
        json={"embedding.api_key": "plain-secret"},
    )
    effective = client.get("/admin/api/config/effective").json()

    assert patched.status_code == 200
    assert secret.json()["embedding.api_key"]["masked"] != "plain-secret"
    assert "value" not in effective["embedding"]["api_key"]


def test_webui_config_accepts_dotted_provider_fields(tmp_path, monkeypatch) -> None:
    client, _app = make_webui_client(tmp_path, monkeypatch)
    token = login_webui(client)

    patched = client.patch(
        "/admin/api/config/web-config",
        headers={"x-csrf-token": token},
        json={"rerank.enabled": True, "rerank.base_url": "http://rerank.local"},
    )
    effective = client.get("/admin/api/config/effective").json()

    assert patched.status_code == 200
    assert effective["rerank"]["enabled"] == {"value": True, "source": "web_config"}
    assert effective["rerank"]["base_url"] == {"value": "http://rerank.local", "source": "web_config"}


def test_webui_config_models_fetches_openai_compatible_list(tmp_path, monkeypatch) -> None:
    calls = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self):
            return {"data": [{"id": "z-model"}, {"id": "a-model"}, {"id": "a-model"}]}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def get(self, url, headers=None):
            calls.append((url, headers or {}))
            return FakeResponse()

    monkeypatch.setattr("research_memory_gateway.webui.app.httpx.AsyncClient", FakeAsyncClient)
    client, _app = make_webui_client(tmp_path, monkeypatch)
    login_webui(client)

    response = client.get("/admin/api/config/models?provider=embedding&base_url=http://models.local/v1")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "models": ["a-model", "z-model"]}
    assert calls == [("http://models.local/v1/models", {})]


def test_webui_json_import_export_and_lifecycle(tmp_path, monkeypatch) -> None:
    client, app = make_webui_client(tmp_path, monkeypatch)
    token = login_webui(client)
    memory = {
        "memory_id": "mem_imported",
        "project": "demo",
        "topic": "Hg",
        "memory_type": "paper_note",
        "title": "Imported",
        "summary": "Imported summary.",
        "evidence": [{"evidence_id": "ev_1", "quote": "Sulfur doping improves Hg2+ affinity."}],
        "claims": [{"claim": "Sulfur doping improves Hg2+ affinity.", "evidence_ids": ["ev_1"]}],
    }
    validate = client.post("/admin/api/import/json/validate", headers={"x-csrf-token": token}, json={"memories": [memory]})
    execute = client.post("/admin/api/import/json/execute", headers={"x-csrf-token": token}, json={"memories": [memory], "policy": "skip_existing"})
    imported = client.get("/admin/api/memories/mem_imported", headers={"x-csrf-token": token})
    archived = client.post("/admin/api/memories/mem_imported/archive", headers={"x-csrf-token": token}, json={"reason": "old"})
    exported = client.post("/admin/api/export", headers={"x-csrf-token": token}, json={"format": "json", "include_archived": True})

    assert validate.json()["valid"] == 1
    assert execute.json()["imported"] == 1
    assert imported.json()["memory_type"] == "paper_note"
    assert imported.json()["claims"][0]["verification_status"] == "evidence_backed"
    assert imported.json()["metadata"]["save_confirmation"]["source"] == "webui_import"
    assert app.state.webui.service.list_memory_proposals(status="saved")[0].saved_memory_id == "mem_imported"
    assert archived.json()["memory_status"] == MemoryStatus.archived.value
    assert exported.json()["count"] == 1


def test_webui_import_validation_rejects_plan_without_plan_status(tmp_path, monkeypatch) -> None:
    client, _app = make_webui_client(tmp_path, monkeypatch)
    token = login_webui(client)
    memory = {
        "memory_id": "mem_bad_plan",
        "project": "demo",
        "topic": "Workflow",
        "memory_type": "workflow_plan",
        "title": "Missing status",
        "summary": "Plan memories need plan_status.",
    }

    validate = client.post("/admin/api/import/json/validate", headers={"x-csrf-token": token}, json={"memories": [memory]})
    execute = client.post("/admin/api/import/json/execute", headers={"x-csrf-token": token}, json={"memories": [memory]})

    assert validate.json()["invalid"] == 1
    assert "plan_status" in validate.json()["errors"][0]["error"]
    assert execute.status_code == 400
    assert execute.json()["error"] == "invalid_import_payload"


def test_webui_overwrite_import_requires_confirmation_and_returns_diff(tmp_path, monkeypatch) -> None:
    client, _app = make_webui_client(tmp_path, monkeypatch)
    token = login_webui(client)
    first = {"memory_id": "mem_overwrite", "project": "demo", "topic": "Hg", "memory_type": "paper_note", "title": "Original", "summary": "Original summary."}
    second = {**first, "title": "Changed"}
    client.post("/admin/api/import/json/execute", headers={"x-csrf-token": token}, json={"memories": [first]})

    response = client.post(
        "/admin/api/import/json/execute",
        headers={"x-csrf-token": token},
        json={"memories": [second], "policy": "overwrite_existing"},
    )

    assert response.status_code == 409
    assert "mem_overwrite" in response.json()["diffs"]
    assert "Changed" in response.json()["diffs"]["mem_overwrite"]


def test_webui_backfill_dry_run_and_single_job_lock(tmp_path, monkeypatch) -> None:
    client, app = make_webui_client(tmp_path, monkeypatch)
    token = login_webui(client)
    client.post(
        "/admin/api/memories",
        headers={"x-csrf-token": token},
        json={"project": "demo", "topic": "Hg", "memory_type": "paper_note", "title": "Backfill", "summary": "Backfill summary.", "confirmed": True},
    )
    app.state.webui.service.backend.embedding_client = FakeEmbeddingClient()
    dry = client.post("/admin/api/retrieval/backfill/dry-run", headers={"x-csrf-token": token}, json={"scope": "active"})

    assert dry.json()["total"] == 1


def test_webui_backfill_single_job_lock_and_cancel(tmp_path, monkeypatch) -> None:
    client, app = make_webui_client(tmp_path, monkeypatch)
    token = login_webui(client)
    for index in range(4):
        client.post(
            "/admin/api/memories",
            headers={"x-csrf-token": token},
            json={"project": "demo", "topic": "Hg", "memory_type": "paper_note", "title": f"Backfill {index}", "summary": "Backfill summary.", "confirmed": True},
        )
    app.state.webui.service.backend.embedding_client = SlowEmbeddingClient()
    first = client.post("/admin/api/retrieval/backfill/start", headers={"x-csrf-token": token}, json={"scope": "active", "batch_size": 1, "concurrency": 1})
    app.state.webui.backfills.running_job_id = first.json()["job_id"]
    app.state.webui.backfills.jobs[first.json()["job_id"]].status = "running"
    second = client.post(
        "/admin/api/retrieval/backfill/start",
        headers={"x-csrf-token": token},
        json={"scope": "active"},
    )
    cancel = client.post(
        f"/admin/api/retrieval/backfill/jobs/{first.json()['job_id']}/cancel",
        headers={"x-csrf-token": token},
        json={},
    )

    assert first.status_code == 202
    assert second.status_code == 409
    assert cancel.json()["cancel_requested"] is True


def test_nocturne_reserved_connector_returns_not_implemented() -> None:
    connector = NocturneReservedConnector(transport="rest", url="http://nocturne.local", token="secret-token")

    result = connector.create({"memory": "demo"})

    assert result["error"] == "not_implemented"
    assert result["reserved"] is True
    assert "create" in result["unsupported"]


def test_webui_security_does_not_expose_secret_in_html_audit_or_export(tmp_path, monkeypatch) -> None:
    client, app = make_webui_client(tmp_path, monkeypatch)
    token = login_webui(client)
    secret = "super-secret-api-key"
    client.patch("/admin/api/config/secrets", headers={"x-csrf-token": token}, json={"embedding.api_key": secret})
    app.state.webui.service.append_audit_event("security.test", metadata={"api_key": secret, "nested": {"token": secret}})
    client.post(
        "/admin/api/memories",
        headers={"x-csrf-token": token},
        json={"project": "demo", "topic": "Hg", "memory_type": "paper_note", "title": "Secret safe", "summary": "No secret here.", "confirmed": True},
    )
    html = client.get("/admin/config").text
    audit = app.state.webui.service.list_audit_events(limit=10)
    exported = client.post("/admin/api/export", headers={"x-csrf-token": token}, json={"format": "json"}).json()

    assert secret not in html
    assert secret not in repr(audit)
    assert secret not in repr(exported)


def test_webui_pages_render_forms_without_secret_values(tmp_path, monkeypatch) -> None:
    client, _app = make_webui_client(tmp_path, monkeypatch)
    login_webui(client)

    for path in ["/admin/memories/new", "/admin/proposals", "/admin/config", "/admin/config/nocturne", "/admin/import", "/admin/exports"]:
        response = client.get(path)
        assert response.status_code == 200
        assert "https://" not in response.text
        assert "cdn" not in response.text.lower()
        assert "value=\"plain-secret\"" not in response.text
