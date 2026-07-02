from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from ..backends import SQLiteMemoryBackend
from ..config import (
    AppConfig,
    AuthStore,
    RuntimeConfigResolver,
    SecretStore,
    WebConfigStore,
)
from ..models import ExportFormat, MemoryStatus, ResearchMemory
from ..nocturne import NocturneReservedConnector
from ..service import ResearchMemoryService
from .auth_routes import SessionManager, security_routes
from .runtime import (
    BackfillManager,
    ImportConfirmationRequired,
    ImportValidationError,
    UnsupportedImportPolicy,
    bounded_int,
    diff_json,
    import_execute,
    import_validate,
    validate_backfill_options,
)


SAFE_SECRET_KEYS = {"embedding.api_key", "rerank.api_key", "nocturne.token"}
WRITE_METHODS = {"POST", "PATCH", "PUT", "DELETE"}


def build_webui_app(config: AppConfig, service: ResearchMemoryService | None = None) -> Starlette:
    if not config.webui.enabled:
        raise RuntimeError("WebUI is disabled by config.webui.enabled")
    auth_store = AuthStore(config.webui.auth_store_path)
    auth_store.bootstrap(config.webui)
    secret_store = SecretStore(config.webui.secret_store_path)
    web_config_store = WebConfigStore(config.webui.web_config_path)
    resolver = RuntimeConfigResolver(config, web_config_store, secret_store)
    if service is None:
        backend = SQLiteMemoryBackend(config.backend.sqlite_path, config.retrieval, resolver)
        service = ResearchMemoryService(config, backend)
    sessions = SessionManager(config)
    backfills = BackfillManager(service)
    state = WebState(config, service, auth_store, secret_store, web_config_store, resolver, sessions, backfills)

    routes = [
        Route("/admin/api/memories", api_memories, methods=["GET", "POST"]),
        Route("/admin/api/memories/overlap-check", api_overlap, methods=["POST"]),
        Route("/admin/api/memories/diff", api_diff, methods=["POST"]),
        Route("/admin/api/memories/{memory_id:str}", api_memory_detail, methods=["GET", "PATCH"]),
        Route("/admin/api/memories/{memory_id:str}/archive", api_archive, methods=["POST"]),
        Route("/admin/api/memories/{memory_id:str}/restore", api_restore, methods=["POST"]),
        Route("/admin/api/memories/{memory_id:str}/soft-delete", api_soft_delete, methods=["POST"]),
        Route("/admin/api/memories/{memory_id:str}/hard-delete", api_hard_delete, methods=["DELETE"]),
        Route("/admin/api/taxonomy", api_taxonomy, methods=["GET"]),
        Route("/admin/api/proposals", api_proposals, methods=["GET"]),
        Route("/admin/api/proposals/{proposal_id:str}", api_proposal_detail, methods=["GET", "PATCH"]),
        Route("/admin/api/proposals/{proposal_id:str}/save", api_proposal_save, methods=["POST"]),
        Route("/admin/api/projects", api_projects, methods=["GET"]),
        Route("/admin/api/config/effective", api_config_effective, methods=["GET"]),
        Route("/admin/api/config/web-config", api_config_web_patch, methods=["PATCH"]),
        Route("/admin/api/config/secrets", api_config_secrets_patch, methods=["PATCH"]),
        Route("/admin/api/config/secrets/{provider:str}/{field:str}", api_config_secret_delete, methods=["DELETE"]),
        Route("/admin/api/config/models", api_config_models, methods=["GET"]),
        Route("/admin/api/config/test", api_config_test, methods=["POST"]),
        Route("/admin/api/nocturne/{operation:str}", api_nocturne_reserved_operation, methods=["POST"]),
        Route("/admin/api/retrieval/vector-coverage", api_vector_coverage, methods=["GET"]),
        Route("/admin/api/retrieval/backfill/dry-run", api_backfill_dry_run, methods=["POST"]),
        Route("/admin/api/retrieval/backfill/start", api_backfill_start, methods=["POST"]),
        Route("/admin/api/retrieval/backfill/jobs/{job_id:str}", api_backfill_job, methods=["GET"]),
        Route("/admin/api/retrieval/backfill/jobs/{job_id:str}/cancel", api_backfill_cancel, methods=["POST"]),
        Route("/admin/api/import/json/validate", api_import_validate, methods=["POST"]),
        Route("/admin/api/import/json/execute", api_import_execute, methods=["POST"]),
        Route("/admin/api/export", api_export, methods=["POST"]),
        Route("/admin/api/audit", api_audit_events, methods=["GET"]),
        Route("/admin/api/stats", api_stats, methods=["GET"]),
        *security_routes(),
        Mount("/admin/assets", StaticFiles(directory=Path(__file__).parent / "static" / "dist" / "assets"), name="admin-assets"),
        Route("/admin/favicon.svg", serve_favicon, methods=["GET"]),
        Route("/admin/{path:path}", serve_spa, methods=["GET"]),
        Route("/admin", serve_spa, methods=["GET"]),
    ]
    app = Starlette(routes=routes)
    app.state.webui = state
    app.add_middleware(SecurityMiddleware)
    return app


@dataclass
class WebState:
    config: AppConfig
    service: ResearchMemoryService
    auth_store: AuthStore
    secret_store: SecretStore
    web_config_store: WebConfigStore
    resolver: RuntimeConfigResolver
    sessions: "SessionManager"
    backfills: BackfillManager


class SecurityMiddleware:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        state = request.app.state.webui
        # Allow static assets, favicon, and login page through without auth
        if (
            request.url.path.startswith("/admin/assets")
            or request.url.path.startswith("/admin/static")
            or request.url.path == "/admin/favicon.svg"
            or request.url.path == "/admin/login"
        ):
            await self._send_with_headers(scope, receive, send)
            return
        if request.url.path.startswith("/admin") and request.url.path != "/admin/login":
            if request.url.path.startswith("/admin/api/auth/"):
                await self._send_with_headers(scope, receive, send)
                return
            if not state.sessions.current(request):
                response: Response
                if request.url.path.startswith("/admin/api"):
                    response = JSONResponse({"error": "unauthorized"}, status_code=401)
                else:
                    response = RedirectResponse("/admin/login", status_code=303)
                await response(scope, receive, send)
                return
        if request.method in WRITE_METHODS and request.url.path.startswith("/admin"):
            if request.url.path.startswith("/admin/api/auth/"):
                await self._send_with_headers(scope, receive, send)
                return
            if not state.sessions.verify_csrf(request):
                response = JSONResponse({"error": "csrf_failed"}, status_code=403)
                await response(scope, receive, send)
                return
        await self._send_with_headers(scope, receive, send)

    async def _send_with_headers(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        async def send_wrapper(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                headers = Headers(raw=message.setdefault("headers", []))
                existing = list(message["headers"])
                additions = {
                    b"content-security-policy": b"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; font-src 'self'; frame-ancestors 'none'",
                    b"x-content-type-options": b"nosniff",
                    b"referrer-policy": b"same-origin",
                }
                for key, value in additions.items():
                    if key.decode("ascii") not in headers:
                        existing.append((key, value))
                message["headers"] = existing
            await send(message)

        await self.app(scope, receive, send_wrapper)


async def serve_spa(request: Request) -> Response:
    index_path = Path(__file__).parent / "static" / "dist" / "index.html"
    if index_path.exists():
        return HTMLResponse(index_path.read_text("utf-8"))
    return HTMLResponse("<h1>WebUI build not found</h1><p>Please run 'npm run build' in frontend directory.</p>", status_code=404)


async def serve_favicon(request: Request) -> Response:
    favicon_path = Path(__file__).parent / "static" / "dist" / "favicon.svg"
    if favicon_path.exists():
        return Response(favicon_path.read_bytes(), media_type="image/svg+xml")
    return Response(status_code=404)


async def api_memories(request: Request) -> Response:
    state = request.app.state.webui
    if request.method == "GET":
        memories = await _memory_list(request)
        return JSONResponse({"items": [m.model_dump(mode="json") for m in memories]})
    payload = await request.json()
    try:
        memory = state.service.validate_research_memory_for_write(payload)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    overlaps = state.service.check_overlap(query=f"{memory.title} {memory.summary}", project=memory.project)
    if overlaps and not payload.get("confirmed"):
        return JSONResponse({"error": "overlap_confirmation_required", "overlap_candidates": overlaps}, status_code=409)
    try:
        saved = state.service.save_research_memory(
            user_confirmed=True,
            memory=payload,
            confirmation={
                "source": "webui",
                "text": "WebUI memory create",
                "confirmed_by": "webui_user",
            },
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    state.service.append_audit_event("memory.created", memory_id=saved.memory_id, metadata={"project": saved.project})
    return JSONResponse(saved.model_dump(mode="json"), status_code=201)


async def api_memory_detail(request: Request) -> Response:
    state = request.app.state.webui
    memory_id = request.path_params["memory_id"]
    if request.method == "GET":
        try:
            memory = state.service.get_research_memory(memory_id)
        except KeyError:
            return JSONResponse({"error": "not_found"}, status_code=404)
        return JSONResponse(memory.model_dump(mode="json"))
    payload = await request.json()
    try:
        updated = state.service.update_research_memory(memory_id, payload, user_confirmed=True)
    except KeyError:
        return JSONResponse({"error": "not_found"}, status_code=404)
    except (PermissionError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    state.service.append_audit_event("memory.updated", memory_id=memory_id, metadata={"fields": sorted(payload)})
    return JSONResponse(updated.model_dump(mode="json"))


async def api_archive(request: Request) -> Response:
    payload = await _json_or_empty(request)
    memory = request.app.state.webui.service.archive_memory(request.path_params["memory_id"], payload.get("reason", ""), True)
    return JSONResponse(memory.model_dump(mode="json"))


async def api_restore(request: Request) -> Response:
    payload = await _json_or_empty(request)
    memory = request.app.state.webui.service.restore_memory(request.path_params["memory_id"], payload.get("reason", ""), True)
    return JSONResponse(memory.model_dump(mode="json"))


async def api_soft_delete(request: Request) -> Response:
    payload = await _json_or_empty(request)
    memory = request.app.state.webui.service.soft_delete_memory(request.path_params["memory_id"], payload.get("reason", ""), True)
    return JSONResponse(memory.model_dump(mode="json"))


async def api_hard_delete(request: Request) -> Response:
    state = request.app.state.webui
    payload = await _json_or_empty(request)
    current_password_valid = state.auth_store.verify(payload.get("current_password", ""), state.config.webui)
    result = state.service.hard_delete_memory(
        request.path_params["memory_id"],
        confirm_memory_id=payload.get("confirm_memory_id", ""),
        current_password_valid=current_password_valid,
        reason=payload.get("reason", ""),
        user_confirmed=True,
    )
    return JSONResponse(result)


async def api_projects(request: Request) -> Response:
    memories = request.app.state.webui.service.backend.list_all(statuses=[s.value for s in MemoryStatus])
    return JSONResponse({"projects": sorted({m.project for m in memories})})


async def api_taxonomy(request: Request) -> Response:
    return JSONResponse(request.app.state.webui.service.get_memory_taxonomy())


async def api_proposals(request: Request) -> Response:
    state = request.app.state.webui
    status = request.query_params.get("status") or None
    limit = bounded_int(request.query_params.get("limit"), 1, 200, 50)
    try:
        proposals = state.service.list_memory_proposals(status=status, limit=limit)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"items": [item.model_dump(mode="json") for item in proposals]})


async def api_proposal_detail(request: Request) -> Response:
    state = request.app.state.webui
    proposal_id = request.path_params["proposal_id"]
    if request.method == "PATCH":
        payload = await request.json()
        try:
            proposal = state.service.update_memory_proposal_status(
                proposal_id,
                str(payload.get("proposal_status", "")),
                reason=str(payload.get("reason", "")),
                user_confirmed=True,
            )
        except KeyError:
            return JSONResponse({"error": "not_found"}, status_code=404)
        except (PermissionError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse(proposal.model_dump(mode="json"))
    try:
        proposal = state.service.get_memory_proposal(proposal_id).model_dump(mode="json")
    except KeyError:
        return JSONResponse({"error": "not_found"}, status_code=404)
    proposal["versions"] = state.service.get_memory_proposal_versions(proposal_id)
    return JSONResponse(proposal)


async def api_proposal_save(request: Request) -> Response:
    state = request.app.state.webui
    payload = await _json_or_empty(request)
    proposal_id = request.path_params["proposal_id"]
    try:
        saved = state.service.save_research_memory(
            user_confirmed=True,
            proposal_id=proposal_id,
            confirmation={
                "source": "webui",
                "text": str(payload.get("text") or payload.get("reason") or "WebUI proposal save"),
                "confirmed_by": "webui_user",
            },
        )
    except KeyError:
        return JSONResponse({"error": "not_found"}, status_code=404)
    except (PermissionError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    state.service.append_audit_event(
        "memory_proposal.saved",
        memory_id=saved.memory_id,
        metadata={"proposal_id": proposal_id},
    )
    return JSONResponse(saved.model_dump(mode="json"), status_code=201)


async def api_overlap(request: Request) -> Response:
    payload = await request.json()
    return JSONResponse({"items": request.app.state.webui.service.check_overlap(query=payload.get("query", ""), project=payload.get("project"), memory_type=payload.get("memory_type"))})


async def api_diff(request: Request) -> Response:
    payload = await request.json()
    return JSONResponse({"diff": diff_json(payload.get("before", {}), payload.get("after", {}))})


async def api_config_effective(request: Request) -> Response:
    return JSONResponse(_redact_effective(request.app.state.webui.resolver.effective()))


async def api_config_web_patch(request: Request) -> Response:
    updated = request.app.state.webui.web_config_store.patch(await request.json())
    return JSONResponse(updated.model_dump(mode="json"))


async def api_config_secrets_patch(request: Request) -> Response:
    state = request.app.state.webui
    payload = await request.json()
    for key, value in payload.items():
        if key not in SAFE_SECRET_KEYS:
            return JSONResponse({"error": "unsupported_secret"}, status_code=400)
        state.secret_store.save_secret(key, str(value))
    return JSONResponse({key: state.secret_store.masked(key) for key in payload})


async def api_config_secret_delete(request: Request) -> Response:
    state = request.app.state.webui
    key = f"{request.path_params['provider']}.{request.path_params['field']}"
    if key not in SAFE_SECRET_KEYS:
        return JSONResponse({"error": "unsupported_secret"}, status_code=400)
    return JSONResponse({"deleted": state.secret_store.delete_secret(key)})


async def api_config_models(request: Request) -> Response:
    provider = request.query_params.get("provider", "")
    if provider not in {"embedding", "rerank"}:
        return JSONResponse({"error": "unsupported_provider"}, status_code=400)
    effective = request.app.state.webui.resolver.effective()[provider]
    base_url = (request.query_params.get("base_url") or effective["base_url"]["value"] or "").strip()
    if not base_url:
        return JSONResponse({"ok": False, "status": "not_configured", "models": []})
    return await _safe_models_fetch(base_url, effective["api_key"]["value"])


async def api_config_test(request: Request) -> Response:
    state = request.app.state.webui
    payload = await request.json()
    provider = payload.get("provider")
    effective = state.resolver.effective()
    if provider == "nocturne":
        connector = NocturneReservedConnector(
            transport=effective["nocturne"]["transport"]["value"],
            url=effective["nocturne"]["url"]["value"],
            token=effective["nocturne"]["token"]["value"],
        )
        return JSONResponse(await connector.test_connection())
    if provider in {"embedding", "rerank"}:
        config = effective[provider]
        url = config["base_url"]["value"]
        endpoint = config["endpoint_path"]["value"]
        if not url:
            return JSONResponse({"ok": False, "status": "not_configured"})
        return await _safe_probe(f"{url.rstrip('/')}{endpoint}", config["api_key"]["value"])
    return JSONResponse({"error": "unsupported_provider"}, status_code=400)


async def api_nocturne_reserved_operation(request: Request) -> Response:
    state = request.app.state.webui
    effective = state.resolver.effective()
    connector = NocturneReservedConnector(
        transport=effective["nocturne"]["transport"]["value"],
        url=effective["nocturne"]["url"]["value"],
        token=effective["nocturne"]["token"]["value"],
    )
    operation = request.path_params["operation"]
    if operation not in {"create", "search", "read", "update", "delete"}:
        return JSONResponse({"error": "unsupported_operation"}, status_code=400)
    return JSONResponse(getattr(connector, operation)())


async def api_vector_coverage(request: Request) -> Response:
    return JSONResponse(request.app.state.webui.backfills.coverage())


async def api_backfill_dry_run(request: Request) -> Response:
    return JSONResponse(
        request.app.state.webui.backfills.dry_run(validate_backfill_options(await request.json()))
    )


async def api_backfill_start(request: Request) -> Response:
    try:
        job = request.app.state.webui.backfills.start(validate_backfill_options(await request.json()))
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    return JSONResponse(job.as_dict(), status_code=202)


async def api_backfill_job(request: Request) -> Response:
    job = request.app.state.webui.backfills.jobs.get(request.path_params["job_id"])
    if job is None:
        return JSONResponse({"error": "not_found"}, status_code=404)
    return JSONResponse(job.as_dict())


async def api_backfill_cancel(request: Request) -> Response:
    return JSONResponse(request.app.state.webui.backfills.cancel(request.path_params["job_id"]).as_dict())


async def api_import_validate(request: Request) -> Response:
    payload = await request.json()
    result = import_validate(request.app.state.webui.service, payload)
    request.app.state.webui.service.append_audit_event("import.json_validated", metadata={"valid": result["valid"], "invalid": result["invalid"]})
    return JSONResponse(result)


async def api_import_execute(request: Request) -> Response:
    state = request.app.state.webui
    payload = await request.json()
    policy = payload.get("policy", "skip_existing")
    confirmed = bool(payload.get("confirmed"))
    try:
        result = import_execute(state.service, payload, policy=policy, confirmed=confirmed)
    except ImportValidationError as exc:
        return JSONResponse({"error": "invalid_import_payload", **exc.validation}, status_code=400)
    except UnsupportedImportPolicy:
        return JSONResponse({"error": "unsupported_import_policy"}, status_code=400)
    except ImportConfirmationRequired as exc:
        return JSONResponse({"error": "confirmation_required", "diffs": exc.diffs}, status_code=409)
    return JSONResponse(result)


async def api_export(request: Request) -> Response:
    payload = await request.json()
    result = request.app.state.webui.service.export_memories(
        ExportFormat(payload.get("format", "both")),
        include_archived=bool(payload.get("include_archived")),
        include_deleted=bool(payload.get("include_deleted")),
    )
    request.app.state.webui.service.append_audit_event("export.created", metadata={"format": payload.get("format", "both"), "count": result["count"]})
    return JSONResponse(result)


async def api_audit_events(request: Request) -> Response:
    events = request.app.state.webui.service.list_audit_events()
    limit = int(request.query_params.get("limit", "100"))
    offset = int(request.query_params.get("offset", "0"))
    event_type = request.query_params.get("event_type")
    if event_type:
        events = [e for e in events if e.get("event_type") == event_type]
    total = len(events)
    events = events[offset:offset + limit]
    return JSONResponse({"items": events, "total": total, "limit": limit, "offset": offset})


async def api_stats(request: Request) -> Response:
    state = request.app.state.webui
    all_memories = state.service.backend.list_all(statuses=[s.value for s in MemoryStatus])
    active = [m for m in all_memories if m.memory_status == MemoryStatus.active]
    archived = [m for m in all_memories if m.memory_status == MemoryStatus.archived]
    existing_embeddings = state.backfills.embedded_memory_ids()

    type_counts: dict[str, int] = {}
    for m in all_memories:
        t = m.memory_type.value
        type_counts[t] = type_counts.get(t, 0) + 1

    project_counts: dict[str, int] = {}
    for m in all_memories:
        project_counts[m.project] = project_counts.get(m.project, 0) + 1

    return JSONResponse({
        "total": len(all_memories),
        "active": len(active),
        "archived": len(archived),
        "deleted": len(all_memories) - len(active) - len(archived),
        "embedded": len(existing_embeddings),
        "vector_coverage": round(len(existing_embeddings) / max(len(all_memories), 1) * 100, 1),
        "type_distribution": type_counts,
        "project_distribution": project_counts,
    })


async def _memory_list(request: Request) -> list[ResearchMemory]:
    state = request.app.state.webui
    status = request.query_params.get("status", "active")
    statuses = _status_filter(status)
    query = request.query_params.get("query", "")
    project = request.query_params.get("project") or None
    memory_type = request.query_params.get("memory_type") or None
    if query:
        return [r.memory for r in state.service.backend.search(query, project=project, memory_type=memory_type, statuses=statuses, limit=50)]
    memories = state.service.backend.list_all(statuses=statuses)
    if project:
        memories = [m for m in memories if m.project == project]
    if memory_type:
        memories = [m for m in memories if m.memory_type.value == memory_type]
    topic = request.query_params.get("topic")
    if topic:
        memories = [m for m in memories if m.topic == topic]
    tag = request.query_params.get("tag")
    if tag:
        memories = [m for m in memories if tag in m.tags]
    return memories





def _status_filter(status: str) -> list[str]:
    if status == "all":
        return [s.value for s in MemoryStatus]
    return [MemoryStatus(status).value]


async def _safe_probe(url: str, token: str | None, *, reserved: bool = False) -> JSONResponse:
    headers = {"Authorization": "Bearer ***"} if token else {}
    real_headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(url, headers=real_headers)
        return JSONResponse({"ok": response.status_code < 500, "status_code": response.status_code, "reserved": reserved, "headers_sent": list(headers)})
    except httpx.HTTPError as exc:
        return JSONResponse({"ok": False, "error": exc.__class__.__name__, "reserved": reserved})


async def _safe_models_fetch(base_url: str, token: str | None) -> JSONResponse:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(_models_url(base_url), headers=headers)
        response.raise_for_status()
        models = _extract_model_ids(response.json())
        return JSONResponse({"ok": True, "models": models})
    except (httpx.HTTPError, ValueError) as exc:
        return JSONResponse({"ok": False, "error": exc.__class__.__name__, "models": []}, status_code=502)


def _models_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    if normalized.endswith("/models"):
        return normalized
    if normalized.endswith("/v1"):
        return f"{normalized}/models"
    return f"{normalized}/v1/models"


def _extract_model_ids(data: Any) -> list[str]:
    if not isinstance(data, dict):
        raise ValueError("models response is not an object")
    raw_models = data.get("data") or data.get("models") or []
    if not isinstance(raw_models, list):
        raise ValueError("models response does not contain a list")
    models: list[str] = []
    for item in raw_models:
        if isinstance(item, str):
            models.append(item)
        elif isinstance(item, dict) and item.get("id"):
            models.append(str(item["id"]))
        elif isinstance(item, dict) and item.get("name"):
            models.append(str(item["name"]))
    return sorted(dict.fromkeys(models))


async def _json_or_empty(request: Request) -> dict[str, Any]:
    try:
        return await request.json()
    except json.JSONDecodeError:
        return {}


def _redact_effective(value: dict[str, Any]) -> dict[str, Any]:
    cloned = json.loads(json.dumps(value, default=str))
    for section in ("embedding", "rerank"):
        if section in cloned and "api_key" in cloned[section]:
            cloned[section]["api_key"].pop("value", None)
    if "nocturne" in cloned and "token" in cloned["nocturne"]:
        cloned["nocturne"]["token"].pop("value", None)
    return cloned


def _esc(value: Any) -> str:
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
