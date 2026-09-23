import pytest
from starlette.testclient import TestClient

from research_memory_gateway.backends import SQLiteMemoryBackend
from research_memory_gateway.config import (
    AppConfig,
    AuthStore,
    RuntimeConfigResolver,
    SecretStore,
    WebConfigStore,
    WebRuntimeConfig,
)
from research_memory_gateway.models import MemoryStatus
from research_memory_gateway.nocturne import NocturneReservedConnector
from research_memory_gateway.service import ResearchMemoryService
from research_memory_gateway.webui.app import build_webui_app
from research_memory_gateway.webui.runtime import validate_backfill_options


class FakeEmbeddingClient:
    enabled = True

    def embed(self, text: str) -> list[float] | None:
        if "photocatalysis" in text.lower():
            return [0.0, 1.0]
        return [1.0, 0.0]


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


class NamedMultimodalEmbeddingClient(FakeEmbeddingClient):
    model = "test-multimodal"
    last_error = None
    last_status_code = 200

    def embed_image_bytes(self, content: bytes, *, mime_type: str = "image/png") -> list[float] | None:
        return [1.0, 0.0]


def test_backfill_timeout_defaults_match_long_inference_budget() -> None:
    assert WebRuntimeConfig().backfill.default_request_timeout_seconds == 300
    assert validate_backfill_options({})["request_timeout_seconds"] == 300
    assert validate_backfill_options({"request_timeout_seconds": 900})["request_timeout_seconds"] == 900
    assert validate_backfill_options({"request_timeout_seconds": 901})["request_timeout_seconds"] == 900


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


def test_runtime_config_resolver_uses_base_config_when_web_overlay_is_absent(tmp_path, monkeypatch) -> None:
    config = AppConfig()
    config.retrieval.mode = "hybrid"
    config.retrieval.embedding.enabled = True
    config.retrieval.embedding.base_url = "http://config.local/v1"
    config.retrieval.embedding.model = "config-model"
    web_store = WebConfigStore(tmp_path / "missing-web-config.yaml")
    resolver = RuntimeConfigResolver(
        config,
        web_store,
        SecretStore(tmp_path / "secrets.json.enc", secret_key="dev-key"),
    )

    effective = resolver.effective()

    assert effective["retrieval"]["mode"] == {"value": "hybrid", "source": "config"}
    assert effective["embedding"]["enabled"] == {"value": True, "source": "config"}
    assert effective["embedding"]["base_url"] == {"value": "http://config.local/v1", "source": "config"}
    assert effective["embedding"]["model"] == {"value": "config-model", "source": "config"}


def test_web_config_patch_persists_only_explicit_overlay_fields(tmp_path) -> None:
    store = WebConfigStore(tmp_path / "web_config.yaml")

    updated = store.patch({"retrieval.mode": "hybrid"})
    raw = store.load_raw()

    assert updated.retrieval["mode"] == "hybrid"
    assert raw == {"retrieval": {"mode": "hybrid"}}


def make_webui_client(tmp_path, monkeypatch, *, secret_key: str | None = "dev-key"):
    if secret_key is None:
        monkeypatch.delenv("WEBUI_SECRET_KEY", raising=False)
    else:
        monkeypatch.setenv("WEBUI_SECRET_KEY", secret_key)
    config = AppConfig()
    config.backend.sqlite_path = str(tmp_path / "memory.db")
    config.memory.require_user_confirmation = False
    config.webui.enabled = True
    config.webui.initial_password = "admin-pass"
    config.webui.auth_store_path = str(tmp_path / "auth.json")
    config.webui.web_config_path = str(tmp_path / "web_config.yaml")
    config.webui.secret_store_path = str(tmp_path / "secrets.json.enc")
    config.media_index.index_path = str(tmp_path / "media.sqlite")
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


def test_effective_config_does_not_create_media_index(tmp_path, monkeypatch) -> None:
    client, _app = make_webui_client(tmp_path, monkeypatch)
    login_webui(client)
    media_path = tmp_path / "media.sqlite"
    assert media_path.exists() is False

    response = client.get("/admin/api/config/effective")

    assert response.status_code == 200
    assert response.json()["system"]["media_index"]["vector_generation"] == 1
    assert media_path.exists() is False


def test_unified_vector_index_status_and_generation_rebuild_api(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("WEBUI_SECRET_KEY", "dev-key")
    config = AppConfig()
    config.backend.sqlite_path = str(tmp_path / "memory.db")
    config.memory.require_user_confirmation = False
    config.webui.enabled = True
    config.webui.initial_password = "admin-pass"
    config.webui.auth_store_path = str(tmp_path / "auth.json")
    config.webui.web_config_path = str(tmp_path / "web_config.yaml")
    config.webui.secret_store_path = str(tmp_path / "secrets.json.enc")
    config.media_index.index_path = str(tmp_path / "media.sqlite")

    backend = SQLiteMemoryBackend(config.backend.sqlite_path, config.retrieval)
    backend.embedding_client = NamedMultimodalEmbeddingClient()
    service = ResearchMemoryService(config, backend)
    app = build_webui_app(config, service)
    client = TestClient(app)
    token = login_webui(client)

    status = client.get("/admin/api/retrieval/vector-index/status")
    assert status.status_code == 200
    status_data = status.json()
    assert status_data["embedding_model"] == "test-multimodal"
    assert status_data["vector_generation"] == 1
    assert status_data["memory"]["total"] == 0
    assert status_data["conversation"]["sections"] == 0
    assert status_data["media"]["resources"] == 0

    effective = client.get("/admin/api/config/effective")
    assert effective.status_code == 200
    media_config = effective.json()["system"]["media_index"]
    assert media_config["vector_generation"] == 1
    assert "embedding_version" not in media_config

    dry_run = client.post(
        "/admin/api/retrieval/vector-index/rebuild/dry-run",
        headers={"x-csrf-token": token},
        json={"new_generation": True},
    )
    assert dry_run.status_code == 200
    assert dry_run.json()["next_generation"] == 2

    started = client.post(
        "/admin/api/retrieval/vector-index/rebuild/start",
        headers={"x-csrf-token": token},
        json={"new_generation": True, "reason": "test_weight_change"},
    )
    assert started.status_code == 202
    started_data = started.json()
    assert started_data["generation"] == 2
    assert started_data["new_generation"] is True
    assert service.vector_generation == 2
    assert service.media_index.vector_generation == 2


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


def test_webui_proposal_batch_review_and_save(tmp_path, monkeypatch) -> None:
    client, app = make_webui_client(tmp_path, monkeypatch)
    token = login_webui(client)
    headers = {"x-csrf-token": token}
    proposals = []
    for index in range(2):
        proposals.append(
            app.state.webui.service.propose_save(
                reason=f"batch candidate {index}",
                suggested_memory={
                    "project": "research-memory-gateway",
                    "topic": "Agent memory policy",
                    "memory_type": "workflow_plan",
                    "title": f"Batch proposal {index}",
                    "summary": f"Batch review candidate {index}.",
                    "metadata": {"plan_status": "draft", "plan_type": "agent_memory_policy"},
                },
                check_overlap=False,
            )
        )
    proposal_ids = [item.proposal_id for item in proposals]

    approved = client.post(
        "/admin/api/proposals/batch",
        headers=headers,
        json={"proposal_ids": proposal_ids, "action": "approve", "reason": "Approve selected"},
    )
    saved = client.post(
        "/admin/api/proposals/batch",
        headers=headers,
        json={"proposal_ids": proposal_ids, "action": "save", "reason": "Save selected"},
    )

    assert approved.status_code == 200
    assert approved.json()["succeeded"] == 2
    assert {item["status"] for item in approved.json()["results"]} == {"approved"}
    assert saved.status_code == 200
    assert saved.json()["succeeded"] == 2
    assert len(app.state.webui.service.search_research_memory(query="Batch proposal")) == 2


def test_webui_config_secret_masking_and_env_override(tmp_path, monkeypatch) -> None:
    client, app = make_webui_client(tmp_path, monkeypatch)
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
    app.state.webui.service.media_index._state_set(
        image_embedding_state="image_input_rejected",
        image_embedding_last_error="http_error",
        image_embedding_last_status_code=415,
        image_embedding_last_model="test-model",
        image_embedding_last_attempt_at="2026-09-15T00:00:00+00:00",
    )
    effective = client.get("/admin/api/config/effective").json()

    assert patched.status_code == 200
    assert secret.json()["embedding.api_key"]["masked"] != "plain-secret"
    assert "value" not in effective["embedding"]["api_key"]
    assert effective["system"]["backend"]["sqlite_path"].endswith("memory.db")
    assert effective["system"]["conversation_archive"]["enabled"] is False
    assert effective["system"]["media_index"]["enabled"] is True
    assert effective["system"]["media_index"]["shares_embedding_provider"] is True
    assert effective["system"]["media_index"]["image_embedding_state"] == "image_input_rejected"
    assert effective["system"]["media_index"]["image_embedding_last_status_code"] == 415
    assert effective["system"]["upload"]["configured_enabled"] is True
    assert effective["system"]["upload"]["enabled"] is True
    assert effective["system"]["upload"]["mcp_tools_enabled"] is False


def test_webui_secret_api_returns_409_when_secret_store_is_not_configured(tmp_path, monkeypatch) -> None:
    client, _app = make_webui_client(tmp_path, monkeypatch, secret_key=None)
    token = login_webui(client)

    response = client.patch(
        "/admin/api/config/secrets",
        headers={"x-csrf-token": token},
        json={"embedding.api_key": "not-written"},
    )

    assert response.status_code == 409
    assert response.json()["error"] == "secret_store_not_configured"


def test_webui_config_invalid_patch_returns_400_without_persisting(tmp_path, monkeypatch) -> None:
    client, app = make_webui_client(tmp_path, monkeypatch)
    token = login_webui(client)

    response = client.patch(
        "/admin/api/config/web-config",
        headers={"x-csrf-token": token},
        json={"retrieval.mode": "invalid-mode"},
    )

    assert response.status_code == 400
    assert not app.state.webui.web_config_store.path.exists()


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


def test_webui_security_api_key_routes(tmp_path, monkeypatch) -> None:
    client, _app = make_webui_client(tmp_path, monkeypatch)
    token = login_webui(client)
    headers = {"x-csrf-token": token}

    missing_name = client.post("/admin/api/security/api-keys", headers=headers, json={"name": ""})
    created = client.post(
        "/admin/api/security/api-keys",
        headers=headers,
        json={"name": "Notebook", "custom_key": "rmg_test_key"},
    )
    key_id = created.json()["key_id"]
    listed = client.get("/admin/api/security/api-keys")
    usage = client.get(f"/admin/api/security/api-keys/{key_id}/usage")
    connections = client.get("/admin/api/security/connections")
    deleted = client.delete(f"/admin/api/security/api-keys/{key_id}", headers=headers)
    listed_after_delete = client.get("/admin/api/security/api-keys")

    assert missing_name.status_code == 400
    assert created.status_code == 201
    assert created.json()["api_key"] == "rmg_test_key"
    assert listed.json()["items"][0]["key_id"] == key_id
    assert "api_key" not in listed.json()["items"][0]
    assert usage.json() == {"connections": []}
    assert connections.json() == {"items": []}
    assert deleted.json() == {"deleted": True}
    assert listed_after_delete.json()["items"] == []


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


def test_webui_overwrite_import_without_existing_item_imports_normally(tmp_path, monkeypatch) -> None:
    client, _app = make_webui_client(tmp_path, monkeypatch)
    token = login_webui(client)
    memory = {
        "memory_id": "mem_new_overwrite_policy",
        "project": "demo",
        "topic": "Hg",
        "memory_type": "paper_note",
        "title": "New item",
        "summary": "No existing item should mean no overwrite confirmation is needed.",
    }

    response = client.post(
        "/admin/api/import/json/execute",
        headers={"x-csrf-token": token},
        json={"memories": [memory], "policy": "overwrite_existing"},
    )

    assert response.status_code == 200
    assert response.json()["imported"] == 1
    assert client.get("/admin/api/memories/mem_new_overwrite_policy").json()["title"] == "New item"


def test_webui_overwrite_import_confirmed_updates_existing_item(tmp_path, monkeypatch) -> None:
    client, _app = make_webui_client(tmp_path, monkeypatch)
    token = login_webui(client)
    original = {
        "memory_id": "mem_confirmed_overwrite",
        "project": "demo",
        "topic": "Hg",
        "memory_type": "paper_note",
        "title": "Original",
        "summary": "Original summary.",
    }
    replacement = {**original, "title": "Replacement", "summary": "Replacement summary."}
    client.post("/admin/api/import/json/execute", headers={"x-csrf-token": token}, json={"memories": [original]})

    response = client.post(
        "/admin/api/import/json/execute",
        headers={"x-csrf-token": token},
        json={"memories": [replacement], "policy": "overwrite_existing", "confirmed": True},
    )
    updated = client.get("/admin/api/memories/mem_confirmed_overwrite").json()

    assert response.status_code == 200
    assert response.json()["imported"] == 1
    assert updated["title"] == "Replacement"
    assert updated["summary"] == "Replacement summary."


def test_webui_import_rejects_unsupported_policy(tmp_path, monkeypatch) -> None:
    client, _app = make_webui_client(tmp_path, monkeypatch)
    token = login_webui(client)
    memory = {
        "memory_id": "mem_bad_policy",
        "project": "demo",
        "topic": "Hg",
        "memory_type": "paper_note",
        "title": "Bad policy",
        "summary": "Unsupported policies should not fall through to save.",
    }

    response = client.post(
        "/admin/api/import/json/execute",
        headers={"x-csrf-token": token},
        json={"memories": [memory], "policy": "overwrite_everything"},
    )

    assert response.status_code == 400
    assert response.json()["error"] == "unsupported_import_policy"


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
