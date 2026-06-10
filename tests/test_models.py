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
from research_memory_gateway.models import ExportFormat, MemoryStatus, ResearchMemory
from research_memory_gateway.nocturne import NocturneReservedConnector
from research_memory_gateway.retrieval import EmbeddingClient, RerankClient
from research_memory_gateway.server import BearerAuthMiddleware, build_mcp
from research_memory_gateway.service import ResearchMemoryService
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
    response = client.post("/admin/login", data={"password": "admin-pass"}, follow_redirects=False)
    assert response.status_code == 303
    dashboard = client.get("/admin")
    assert dashboard.status_code == 200
    import re

    token = re.search(r'name="csrf-token" content="([^"]+)"', dashboard.text).group(1)
    return token


def test_webui_login_session_csrf_and_memory_api(tmp_path, monkeypatch) -> None:
    client, _app = make_webui_client(tmp_path, monkeypatch)
    assert client.get("/admin", follow_redirects=False).status_code == 303
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
    client, _app = make_webui_client(tmp_path, monkeypatch)
    token = login_webui(client)
    memory = {"memory_id": "mem_imported", "project": "demo", "topic": "Hg", "memory_type": "paper_note", "title": "Imported", "summary": "Imported summary."}
    validate = client.post("/admin/api/import/json/validate", headers={"x-csrf-token": token}, json={"memories": [memory]})
    execute = client.post("/admin/api/import/json/execute", headers={"x-csrf-token": token}, json={"memories": [memory], "policy": "skip_existing"})
    archived = client.post("/admin/api/memories/mem_imported/archive", headers={"x-csrf-token": token}, json={"reason": "old"})
    exported = client.post("/admin/api/export", headers={"x-csrf-token": token}, json={"format": "json", "include_archived": True})

    assert validate.json()["valid"] == 1
    assert execute.json()["imported"] == 1
    assert archived.json()["memory_status"] == MemoryStatus.archived.value
    assert exported.json()["count"] == 1


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

    for path in ["/admin/memories/new", "/admin/config", "/admin/config/nocturne", "/admin/import", "/admin/exports"]:
        response = client.get(path)
        assert response.status_code == 200
        assert "https://" not in response.text
        assert "cdn" not in response.text.lower()
        assert "value=\"plain-secret\"" not in response.text
