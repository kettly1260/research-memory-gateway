from __future__ import annotations

import asyncio
import difflib
import hashlib
import hmac
import json
import os
import base64
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from ..backends import SQLiteMemoryBackend, memory_to_search_document
from ..config import (
    AppConfig,
    AuthStore,
    RuntimeConfigResolver,
    SecretStore,
    WebConfigStore,
    utc_now,
)
from ..models import ExportFormat, MemoryStatus, ResearchMemory
from ..nocturne import NocturneReservedConnector
from ..service import ResearchMemoryService


def b64_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode('utf-8').rstrip('=')

def b64_decode(data: str) -> bytes:
    padding = '=' * (4 - len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)

def generate_jwt(payload: dict, secret: str) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    header_part = b64_encode(json.dumps(header, separators=(',', ':')).encode('utf-8'))
    payload_part = b64_encode(json.dumps(payload, separators=(',', ':')).encode('utf-8'))
    
    signature_base = f"{header_part}.{payload_part}".encode('utf-8')
    signature = hmac.new(secret.encode('utf-8'), signature_base, hashlib.sha256).digest()
    signature_part = b64_encode(signature)
    
    return f"{header_part}.{payload_part}.{signature_part}"

def verify_jwt(token: str, secret: str) -> dict | None:
    try:
        parts = token.split('.')
        if len(parts) != 3:
            return None
        header_part, payload_part, signature_part = parts
        
        signature_base = f"{header_part}.{payload_part}".encode('utf-8')
        expected_signature = hmac.new(secret.encode('utf-8'), signature_base, hashlib.sha256).digest()
        
        if not hmac.compare_digest(b64_decode(signature_part), expected_signature):
            return None
            
        payload = json.loads(b64_decode(payload_part).decode('utf-8'))
        
        if payload.get("exp", 0) < time.time():
            return None
            
        return payload
    except Exception:
        return None


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
    backfills = BackfillJobManager(service)
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
        Route("/admin/api/security/password", api_security_password, methods=["POST"]),
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
        Route("/admin/api/auth/login", api_login, methods=["POST"]),
        Route("/admin/api/auth/refresh", api_refresh, methods=["POST"]),
        Route("/admin/api/auth/logout", api_logout, methods=["POST"]),
        Route("/admin/api/security/api-keys", api_list_keys, methods=["GET"]),
        Route("/admin/api/security/api-keys", api_create_key, methods=["POST"]),
        Route("/admin/api/security/api-keys/{key_id:str}", api_delete_key, methods=["DELETE"]),
        Route("/admin/api/security/api-keys/{key_id:str}/usage", api_key_usage, methods=["GET"]),
        Route("/admin/api/security/connections", api_list_connections, methods=["GET"]),
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
    backfills: "BackfillJobManager"


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


class SessionManager:
    def __init__(self, config: AppConfig) -> None:
        self.max_age = config.webui.session_max_age_seconds
        self.signing_key = os.getenv("WEBUI_SESSION_KEY") or hashlib.sha256(
            f"{config.webui.auth_store_path}:{config.webui.port}".encode("utf-8")
        ).hexdigest()
        self.invalid_after = 0.0
        self.active_refresh_jtis: set[str] = set()

    def create(self) -> dict[str, str]:
        # 回退兼容
        now = time.time()
        sid = secrets.token_urlsafe(24)
        csrf = secrets.token_urlsafe(24)
        payload = json.dumps({"sid": sid, "csrf": csrf, "exp": now + self.max_age, "iat": now}, separators=(",", ":"))
        sig = hmac.new(self.signing_key.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
        return {"cookie": f"{payload}.{sig}", "csrf": csrf}

    def current(self, request: Request) -> dict[str, Any] | None:
        raw = None
        auth_header = request.headers.get("authorization")
        if auth_header and auth_header.startswith("Bearer "):
            raw = auth_header[7:]
        else:
            raw = request.cookies.get("webui_session")

        if not raw:
            return None

        payload = verify_jwt(raw, self.signing_key)
        if not payload:
            if "." in raw:
                try:
                    payload_part, sig = raw.rsplit(".", 1)
                    expected = hmac.new(self.signing_key.encode("utf-8"), payload_part.encode("utf-8"), hashlib.sha256).hexdigest()
                    if hmac.compare_digest(expected, sig):
                        payload = json.loads(payload_part)
                except Exception:
                    pass

        if not payload:
            return None

        now = time.time()
        if payload.get("exp", 0) < now or payload.get("iat", 0) < self.invalid_after:
            return None
        return payload

    def csrf(self, request: Request) -> str:
        session = self.current(request)
        return session.get("csrf", "") if session else ""

    def verify_csrf(self, request: Request) -> bool:
        if request.url.path in {"/admin/login", "/admin/api/auth/login"}:
            return True
        auth_header = request.headers.get("authorization")
        if auth_header and auth_header.startswith("Bearer "):
            return True

        session = self.current(request)
        if not session:
            return False
        token = request.headers.get("x-csrf-token") or request.query_params.get("csrf_token")
        return bool(token) and hmac.compare_digest(token, session.get("csrf", ""))

    def invalidate_all(self) -> None:
        self.invalid_after = time.time()
        self.active_refresh_jtis.clear()


@dataclass
class BackfillJob:
    job_id: str
    status: str = "running"
    total: int = 0
    completed: int = 0
    failed: int = 0
    skipped: int = 0
    started_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    last_error: str | None = None
    cancel_requested: bool = False
    batch_size: int = 8
    concurrency: int = 2
    request_timeout_seconds: int = 30
    job_timeout_seconds: int = 1800

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


class BackfillJobManager:
    def __init__(self, service: ResearchMemoryService) -> None:
        self.service = service
        self.jobs: dict[str, BackfillJob] = {}
        self.running_job_id: str | None = None

    def dry_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        targets = self._targets(payload)
        existing = self._existing_embeddings()
        candidates = [m for m in targets if payload.get("force") or m.memory_id not in existing]
        self.service.append_audit_event("retrieval.backfill_dry_run", metadata={"total": len(candidates)})
        return {"total": len(candidates), "memory_ids": [m.memory_id for m in candidates]}

    def start(self, payload: dict[str, Any]) -> BackfillJob:
        if self.running_job_id and self.jobs[self.running_job_id].status == "running":
            raise RuntimeError("Only one backfill job can run at a time")
        job = BackfillJob(
            job_id=f"bf_{secrets.token_hex(8)}",
            batch_size=_bounded_int(payload.get("batch_size"), 1, 32, 8),
            concurrency=_bounded_int(payload.get("concurrency"), 1, 4, 2),
            request_timeout_seconds=_bounded_int(payload.get("request_timeout_seconds"), 5, 120, 30),
            job_timeout_seconds=_bounded_int(payload.get("job_timeout_seconds"), 60, 86400, 1800),
        )
        self.jobs[job.job_id] = job
        self.running_job_id = job.job_id
        self.service.append_audit_event("retrieval.backfill_started", metadata={"job_id": job.job_id})
        asyncio.create_task(self._run(job, payload))
        return job

    def cancel(self, job_id: str) -> BackfillJob:
        job = self.jobs[job_id]
        job.cancel_requested = True
        job.updated_at = utc_now()
        self.service.append_audit_event("retrieval.backfill_cancelled", metadata={"job_id": job_id})
        return job

    async def _run(self, job: BackfillJob, payload: dict[str, Any]) -> None:
        try:
            targets = self._targets(payload)
            existing = self._existing_embeddings()
            targets = [m for m in targets if payload.get("force") or m.memory_id not in existing]
            job.total = len(targets)
            if hasattr(self.service.backend, "embedding_client"):
                self.service.backend.embedding_client.config.timeout_seconds = job.request_timeout_seconds
            started = time.monotonic()
            for batch_start in range(0, len(targets), job.batch_size):
                if job.cancel_requested:
                    job.status = "cancelled"
                    break
                if time.monotonic() - started > job.job_timeout_seconds:
                    job.status = "failed"
                    job.last_error = "job_timeout"
                    break
                batch = targets[batch_start : batch_start + job.batch_size]
                semaphore = asyncio.Semaphore(job.concurrency)
                results = await asyncio.gather(*(self._backfill_one(job, memory, semaphore) for memory in batch))
                for result, error in results:
                    if result == "completed":
                        job.completed += 1
                    elif result == "skipped":
                        job.skipped += 1
                    else:
                        job.failed += 1
                        job.last_error = error or "embedding_failed"
                job.updated_at = utc_now()
            if job.status == "running":
                job.status = "completed"
                self.service.append_audit_event("retrieval.backfill_completed", metadata={"job_id": job.job_id})
            elif job.status == "failed":
                self.service.append_audit_event("retrieval.backfill_failed", metadata={"job_id": job.job_id, "error": job.last_error})
        except Exception as exc:  # pragma: no cover - defensive safety for background task
            job.status = "failed"
            job.last_error = exc.__class__.__name__
            self.service.append_audit_event("retrieval.backfill_failed", metadata={"job_id": job.job_id, "error": job.last_error})
        finally:
            job.updated_at = utc_now()
            if self.running_job_id == job.job_id:
                self.running_job_id = None

    async def _backfill_one(
        self,
        job: BackfillJob,
        memory: ResearchMemory,
        semaphore: asyncio.Semaphore,
    ) -> tuple[str, str | None]:
        async with semaphore:
            if job.cancel_requested:
                return "skipped", "cancelled"
            embedding_client = getattr(self.service.backend, "embedding_client", None)
            if embedding_client is None or not embedding_client.enabled:
                return "skipped", None
            vector = await asyncio.to_thread(embedding_client.embed, memory_to_search_document(memory))
            if not vector:
                return "failed", getattr(embedding_client, "last_error", "embedding_failed")
            with self.service.backend._connect() as connection:
                connection.execute("DELETE FROM memory_embeddings WHERE memory_id = ?", (memory.memory_id,))
                connection.execute(
                    "INSERT INTO memory_embeddings(memory_id, embedding, updated_at) VALUES (?, ?, ?)",
                    (memory.memory_id, json.dumps(vector), utc_now()),
                )
            return "completed", None

    def _targets(self, payload: dict[str, Any]) -> list[ResearchMemory]:
        statuses = _scope_to_statuses(payload.get("scope", "active"))
        memories = self.service.backend.list_all(statuses=statuses)
        if payload.get("project"):
            memories = [m for m in memories if m.project == payload["project"]]
        if payload.get("memory_type"):
            memories = [m for m in memories if m.memory_type.value == payload["memory_type"]]
        limit = payload.get("limit")
        if limit not in (None, "all"):
            memories = memories[: max(0, min(int(limit), 1000))]
        return memories

    def _existing_embeddings(self) -> set[str]:
        if not hasattr(self.service.backend, "_connect"):
            return set()
        with self.service.backend._connect() as connection:
            return {row["memory_id"] for row in connection.execute("SELECT memory_id FROM memory_embeddings").fetchall()}


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
    limit = _bounded_int(request.query_params.get("limit"), 1, 200, 50)
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
    before = json.dumps(payload.get("before", {}), ensure_ascii=False, indent=2, sort_keys=True).splitlines()
    after = json.dumps(payload.get("after", {}), ensure_ascii=False, indent=2, sort_keys=True).splitlines()
    return JSONResponse({"diff": "\n".join(difflib.unified_diff(before, after, lineterm=""))})


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


async def api_security_password(request: Request) -> Response:
    state = request.app.state.webui
    content_type = request.headers.get("content-type", "")
    payload = await _form(request) if content_type.startswith("application/x-www-form-urlencoded") else await request.json()
    try:
        state.auth_store.change_password(payload.get("current_password", ""), payload.get("new_password", ""))
    except (PermissionError, RuntimeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    state.sessions.invalidate_all()
    state.service.append_audit_event("security.password_changed")
    response = JSONResponse({"changed": True})
    response.delete_cookie("webui_session")
    return response


async def api_vector_coverage(request: Request) -> Response:
    backend = request.app.state.webui.service.backend
    memories = backend.list_all(statuses=[s.value for s in MemoryStatus])
    existing = request.app.state.webui.backfills._existing_embeddings()
    return JSONResponse({"total": len(memories), "embedded": len(existing), "missing": max(0, len(memories) - len(existing))})


async def api_backfill_dry_run(request: Request) -> Response:
    return JSONResponse(request.app.state.webui.backfills.dry_run(_validate_backfill(await request.json())))


async def api_backfill_start(request: Request) -> Response:
    try:
        job = request.app.state.webui.backfills.start(_validate_backfill(await request.json()))
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
    result = _validate_import_payload(request.app.state.webui.service, payload.get("memories", payload))
    request.app.state.webui.service.append_audit_event("import.json_validated", metadata={"valid": result["valid"], "invalid": result["invalid"]})
    return JSONResponse(result)


async def api_import_execute(request: Request) -> Response:
    state = request.app.state.webui
    payload = await request.json()
    policy = payload.get("policy", "skip_existing")
    confirmed = bool(payload.get("confirmed"))
    validation = _validate_import_payload(state.service, payload.get("memories", []))
    if validation["invalid"]:
        return JSONResponse({"error": "invalid_import_payload", **validation}, status_code=400)
    if policy == "overwrite_existing" and not confirmed:
        diffs: dict[str, str] = {}
        for item in payload.get("memories", []):
            incoming = state.service.validate_research_memory_for_write(item)
            existing = state.service.backend.get(incoming.memory_id)
            if existing is None:
                continue
            before = json.dumps(existing.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True).splitlines()
            after = json.dumps(incoming.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True).splitlines()
            diffs[incoming.memory_id] = "\n".join(difflib.unified_diff(before, after, lineterm=""))
        return JSONResponse({"error": "confirmation_required", "diffs": diffs}, status_code=409)
    memories = payload.get("memories", [])
    imported = 0
    skipped = 0
    for item in memories:
        memory = state.service.validate_research_memory_for_write(item)
        exists = state.service.backend.get(memory.memory_id) is not None
        if exists and policy == "skip_existing":
            skipped += 1
            continue
        data = dict(item)
        if policy == "import_as_new":
            data.pop("memory_id", None)
            data.setdefault("metadata", {})["imported_original_memory_id"] = memory.memory_id
        state.service.save_research_memory(
            user_confirmed=True,
            memory=data,
            confirmation={
                "source": "webui_import",
                "text": f"JSON import policy={policy}",
                "confirmed_by": "webui_user",
            },
        )
        imported += 1
    state.service.append_audit_event("import.json_completed", metadata={"imported": imported, "skipped": skipped, "policy": policy})
    return JSONResponse({"imported": imported, "skipped": skipped})


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
    existing_embeddings = state.backfills._existing_embeddings()

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


def _scope_to_statuses(scope: str) -> list[str]:
    if scope == "all":
        return [s.value for s in MemoryStatus]
    if scope == "active_archived":
        return [MemoryStatus.active.value, MemoryStatus.archived.value]
    return [MemoryStatus.active.value]





def _validate_backfill(payload: dict[str, Any]) -> dict[str, Any]:
    data = dict(payload)
    data["concurrency"] = _bounded_int(data.get("concurrency"), 1, 4, 2)
    data["batch_size"] = _bounded_int(data.get("batch_size"), 1, 32, 8)
    data["request_timeout_seconds"] = _bounded_int(data.get("request_timeout_seconds"), 5, 120, 30)
    data["job_timeout_seconds"] = _bounded_int(data.get("job_timeout_seconds"), 60, 86400, 1800)
    return data


def _bounded_int(value: Any, low: int, high: int, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(low, min(parsed, high))


def _validate_import_payload(service: ResearchMemoryService, raw: Any) -> dict[str, Any]:
    items = raw if isinstance(raw, list) else []
    seen: set[str] = set()
    valid = 0
    invalid = 0
    duplicate_ids: list[str] = []
    errors: list[dict[str, Any]] = []
    overlaps: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        try:
            memory = service.validate_research_memory_for_write(item)
            valid += 1
            if memory.memory_id in seen or service.backend.get(memory.memory_id) is not None:
                duplicate_ids.append(memory.memory_id)
            seen.add(memory.memory_id)
            overlaps.extend(service.check_overlap(query=f"{memory.title} {memory.summary}", project=memory.project, limit=3))
        except ValueError as exc:
            invalid += 1
            errors.append({"index": index, "error": str(exc)})
    return {
        "valid": valid,
        "invalid": invalid,
        "duplicates": len(duplicate_ids),
        "duplicate_memory_id": duplicate_ids,
        "overlap_candidates": overlaps,
        "conflicts": duplicate_ids,
        "errors": errors,
    }


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


async def _form(request: Request) -> dict[str, str]:
    body = (await request.body()).decode("utf-8")
    return {key: values[-1] for key, values in parse_qs(body).items()}


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


# ─── Auth / JWT APIs ───

async def api_login(request: Request) -> Response:
    state = request.app.state.webui
    payload = await request.json()
    password = payload.get("password", "")
    if not state.auth_store.verify(password, state.config.webui):
        return JSONResponse({"error": "invalid_password"}, status_code=401)
        
    now = time.time()
    access_jti = secrets.token_urlsafe(16)
    refresh_jti = secrets.token_urlsafe(16)
    csrf_token = secrets.token_urlsafe(24)
    
    access_token = generate_jwt({
        "sub": "admin",
        "iat": now,
        "exp": now + 1800, # 30 mins
        "jti": access_jti,
        "csrf": csrf_token
    }, state.sessions.signing_key)
    
    refresh_token = generate_jwt({
        "sub": "admin",
        "iat": now,
        "exp": now + state.config.webui.session_max_age_seconds,
        "jti": refresh_jti
    }, state.sessions.signing_key)
    
    state.sessions.active_refresh_jtis.add(refresh_jti)
    
    response = JSONResponse({
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_in": 1800
    })
    response.set_cookie(
        "webui_session",
        access_token,
        httponly=True,
        samesite="lax",
        max_age=1800,
    )
    return response


async def api_refresh(request: Request) -> Response:
    state = request.app.state.webui
    payload = await request.json()
    refresh_token = payload.get("refresh_token", "")
    
    token_data = verify_jwt(refresh_token, state.sessions.signing_key)
    if not token_data or token_data.get("jti") not in state.sessions.active_refresh_jtis:
        return JSONResponse({"error": "invalid_refresh_token"}, status_code=401)
        
    now = time.time()
    access_jti = secrets.token_urlsafe(16)
    csrf_token = secrets.token_urlsafe(24)
    
    access_token = generate_jwt({
        "sub": "admin",
        "iat": now,
        "exp": now + 1800,
        "jti": access_jti,
        "csrf": csrf_token
    }, state.sessions.signing_key)
    
    response = JSONResponse({
        "access_token": access_token,
        "expires_in": 1800
    })
    response.set_cookie(
        "webui_session",
        access_token,
        httponly=True,
        samesite="lax",
        max_age=1800,
    )
    return response


async def api_logout(request: Request) -> Response:
    state = request.app.state.webui
    payload = await _json_or_empty(request)
    refresh_token = payload.get("refresh_token", "")
    
    if refresh_token:
        token_data = verify_jwt(refresh_token, state.sessions.signing_key)
        if token_data:
            state.sessions.active_refresh_jtis.discard(token_data.get("jti"))
            
    response = JSONResponse({"logged_out": True})
    response.delete_cookie("webui_session")
    return response


# ─── API Keys CRUD APIs ───

async def api_list_keys(request: Request) -> Response:
    state = request.app.state.webui
    with state.service.backend._connect() as conn:
        rows = conn.execute("SELECT key_id, name, created_at, last_used_at, status FROM api_keys ORDER BY created_at DESC").fetchall()
    return JSONResponse({"items": [dict(r) for r in rows]})


async def api_create_key(request: Request) -> Response:
    state = request.app.state.webui
    payload = await request.json()
    name = payload.get("name", "").strip()
    if not name:
        return JSONResponse({"error": "name_required"}, status_code=400)
        
    custom_key = payload.get("custom_key", "").strip()
    key_id = f"key_{secrets.token_hex(8)}"
    
    if custom_key:
        raw_key = custom_key
    else:
        raw_key = f"rmg_{secrets.token_hex(16)}"
        
    key_hash = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
    created_at = datetime.now(timezone.utc).isoformat()
    
    with state.service.backend._connect() as conn:
        conn.execute(
            "INSERT INTO api_keys(key_id, name, key_hash, created_at, status) VALUES (?, ?, ?, ?, 'active')",
            (key_id, name, key_hash, created_at)
        )
    state.service.append_audit_event("security.api_key_created", metadata={"key_id": key_id, "name": name})
    return JSONResponse({
        "key_id": key_id,
        "name": name,
        "api_key": raw_key,  # 仅在创建时返回一次明文
        "created_at": created_at,
        "status": "active"
    }, status_code=201)


async def api_delete_key(request: Request) -> Response:
    state = request.app.state.webui
    key_id = request.path_params["key_id"]
    
    with state.service.backend._connect() as conn:
        cursor = conn.execute("DELETE FROM api_keys WHERE key_id = ?", (key_id,))
        deleted = cursor.rowcount > 0
        
    if not deleted:
        return JSONResponse({"error": "not_found"}, status_code=404)
        
    state.service.append_audit_event("security.api_key_revoked", metadata={"key_id": key_id})
    return JSONResponse({"deleted": True})


async def api_key_usage(request: Request) -> Response:
    state = request.app.state.webui
    key_id = request.path_params["key_id"]
    with state.service.backend._connect() as conn:
        rows = conn.execute("SELECT client_ip, client_info, request_count, last_request_at FROM active_connections WHERE key_id = ?", (key_id,)).fetchall()
    return JSONResponse({"connections": [dict(r) for r in rows]})


# ─── Connections APIs ───

async def api_list_connections(request: Request) -> Response:
    state = request.app.state.webui
    sql = """
        SELECT c.key_id, k.name as key_name, c.client_ip, c.client_info, c.request_count, c.last_request_at
        FROM active_connections c
        LEFT JOIN api_keys k ON c.key_id = k.key_id
        ORDER BY c.last_request_at DESC
    """
    with state.service.backend._connect() as conn:
        rows = conn.execute(sql).fetchall()
    return JSONResponse({"items": [dict(r) for r in rows]})
