from __future__ import annotations

import asyncio
import difflib
import hashlib
import hmac
import json
import os
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
from ..models import ExportFormat, MemoryStatus, MemoryType, ResearchMemory
from ..nocturne import NocturneReservedConnector
from ..service import ResearchMemoryService


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
        Route("/admin/login", login_get, methods=["GET"]),
        Route("/admin/login", login_post, methods=["POST"]),
        Route("/admin/logout", logout_post, methods=["POST"]),
        Route("/admin", dashboard, methods=["GET"]),
        Route("/admin/", dashboard, methods=["GET"]),
        Route("/admin/memories", memories_page, methods=["GET"]),
        Route("/admin/memories/new", memory_new_page, methods=["GET"]),
        Route("/admin/memories/{memory_id:str}", memory_detail_page, methods=["GET"]),
        Route("/admin/config", config_page, methods=["GET"]),
        Route("/admin/config/retrieval", config_page, methods=["GET"]),
        Route("/admin/config/nocturne", nocturne_page, methods=["GET"]),
        Route("/admin/security", security_page, methods=["GET"]),
        Route("/admin/audit", audit_page, methods=["GET"]),
        Route("/admin/import", import_page, methods=["GET"]),
        Route("/admin/exports", exports_page, methods=["GET"]),
        Route("/admin/api/memories", api_memories, methods=["GET", "POST"]),
        Route("/admin/api/memories/overlap-check", api_overlap, methods=["POST"]),
        Route("/admin/api/memories/diff", api_diff, methods=["POST"]),
        Route("/admin/api/memories/{memory_id:str}", api_memory_detail, methods=["GET", "PATCH"]),
        Route("/admin/api/memories/{memory_id:str}/archive", api_archive, methods=["POST"]),
        Route("/admin/api/memories/{memory_id:str}/restore", api_restore, methods=["POST"]),
        Route("/admin/api/memories/{memory_id:str}/soft-delete", api_soft_delete, methods=["POST"]),
        Route("/admin/api/memories/{memory_id:str}/hard-delete", api_hard_delete, methods=["DELETE"]),
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
        Mount("/admin/static", StaticFiles(directory=Path(__file__).parent / "static"), name="admin-static"),
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
        if request.url.path.startswith("/admin/static"):
            await self._send_with_headers(scope, receive, send)
            return
        if request.url.path.startswith("/admin") and request.url.path != "/admin/login":
            if not state.sessions.current(request):
                response: Response
                if request.url.path.startswith("/admin/api"):
                    response = JSONResponse({"error": "unauthorized"}, status_code=401)
                else:
                    response = RedirectResponse("/admin/login", status_code=303)
                await response(scope, receive, send)
                return
        if request.method in WRITE_METHODS and request.url.path.startswith("/admin"):
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
                    b"content-security-policy": b"default-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'",
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

    def create(self) -> dict[str, str]:
        now = time.time()
        sid = secrets.token_urlsafe(24)
        csrf = secrets.token_urlsafe(24)
        payload = json.dumps({"sid": sid, "csrf": csrf, "exp": now + self.max_age, "iat": now}, separators=(",", ":"))
        sig = hmac.new(self.signing_key.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
        return {"cookie": f"{payload}.{sig}", "csrf": csrf}

    def current(self, request: Request) -> dict[str, Any] | None:
        raw = request.cookies.get("webui_session")
        if not raw or "." not in raw:
            return None
        payload, sig = raw.rsplit(".", 1)
        expected = hmac.new(self.signing_key.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, sig):
            return None
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return None
        now = time.time()
        if data.get("exp", 0) < now or data.get("iat", 0) < self.invalid_after:
            return None
        return data

    def csrf(self, request: Request) -> str:
        session = self.current(request)
        return session.get("csrf", "") if session else ""

    def verify_csrf(self, request: Request) -> bool:
        if request.url.path == "/admin/login":
            return True
        session = self.current(request)
        if not session:
            return False
        token = request.headers.get("x-csrf-token") or request.query_params.get("csrf_token")
        return bool(token) and hmac.compare_digest(token, session.get("csrf", ""))

    def invalidate_all(self) -> None:
        self.invalid_after = time.time()


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


async def login_get(request: Request) -> Response:
    return HTMLResponse(_login_html(error=""))


async def login_post(request: Request) -> Response:
    state: WebState = request.app.state.webui
    form = await _form(request)
    password = form.get("password", "")
    if not state.auth_store.verify(password, state.config.webui):
        return HTMLResponse(_login_html(error="密码错误"), status_code=401)
    session = state.sessions.create()
    response = RedirectResponse("/admin", status_code=303)
    response.set_cookie(
        "webui_session",
        session["cookie"],
        httponly=True,
        samesite="lax",
        max_age=state.config.webui.session_max_age_seconds,
    )
    return response


async def logout_post(request: Request) -> Response:
    response = RedirectResponse("/admin/login", status_code=303)
    response.delete_cookie("webui_session")
    return response


async def dashboard(request: Request) -> Response:
    state = request.app.state.webui
    memories = state.service.backend.list_all(statuses=[MemoryStatus.active.value])
    body = f"""
    <section class="hero card"><h1>研究记忆管理台</h1><p>单管理员私有控制台。WebUI 用于管理和配置，MCP 仍是正式客户端接口。</p></section>
    <section class="grid two"><div class="card"><h2>活跃记忆</h2><p class="metric">{len(memories)}</p></div><div class="card"><h2>检索模式</h2><p>{_esc(state.resolver.effective()['retrieval']['mode']['value'])}</p></div></section>
    """
    return _page(request, "控制台", body)


async def memories_page(request: Request) -> Response:
    data = await _memory_list(request)
    rows = "".join(
        f"<tr><td><a href='/admin/memories/{m.memory_id}'>{_esc(m.title)}</a></td><td>{_esc(m.project)}</td><td>{_esc(m.topic)}</td><td><span class='chip'>{m.memory_type.value}</span></td><td><span class='chip status-{m.memory_status.value}'>{m.memory_status.value}</span></td></tr>"
        for m in data
    )
    cards = "".join(
        f"<article class='memory-card card'><a href='/admin/memories/{m.memory_id}'><h2>{_esc(m.title)}</h2></a><p>{_esc(m.summary[:160])}</p><span class='chip'>{m.memory_status.value}</span></article>"
        for m in data
    )
    body = f"""
    <div class="toolbar"><h1>记忆</h1><a class="button" href="/admin/memories/new">新建记忆</a></div>
    <form class="filters" method="get"><input name="query" placeholder="搜索" value="{_esc(request.query_params.get('query',''))}"><select name="status"><option>active</option><option>archived</option><option>deleted</option><option>all</option></select><button>筛选</button></form>
    <table class="data-table"><thead><tr><th>标题</th><th>项目</th><th>主题</th><th>类型</th><th>状态</th></tr></thead><tbody>{rows}</tbody></table>
    <div class="mobile-list">{cards}</div>
    """
    return _page(request, "记忆", body)


async def memory_new_page(request: Request) -> Response:
    csrf = request.app.state.webui.sessions.csrf(request)
    body = f"""
    <h1>新建记忆</h1>
    <form class="card form-grid" data-json-form method="post" action="/admin/api/memories" data-success="/admin/memories">
      <input type="hidden" name="confirmed" value="true"><input type="hidden" name="csrf_token" value="{csrf}">
      <label>项目<input name="project" required></label><label>主题<input name="topic" required></label>
      <label>记忆类型<select name="memory_type">{_memory_type_options()}</select></label><label>标题<input name="title" required></label>
      <label class="span-all">摘要<textarea name="summary" required></textarea></label>
      <label>标签，逗号分隔<input name="tags" data-list></label>
      <button>校验重叠并保存</button><output data-form-output></output>
    </form>
    <section class="card"><h2>高级 JSON</h2><textarea class="json-editor" data-memory-json placeholder='{{"project":"demo","topic":"..."}}'></textarea></section>
    """
    return _page(request, "新建记忆", body)


async def memory_detail_page(request: Request) -> Response:
    state = request.app.state.webui
    memory = state.service.get_research_memory(request.path_params["memory_id"])
    danger = ""
    if memory.memory_status == MemoryStatus.deleted:
        danger = f"""<section class='danger-zone'><h2>危险区域</h2><p>硬删除需要完整 memory_id、当前密码和原因。该操作不会清理备份、导出文件或 Nocturne。</p><form data-json-form method='delete' action='/admin/api/memories/{memory.memory_id}/hard-delete'><input name='confirm_memory_id' placeholder='输入 {memory.memory_id}'><input type='password' name='current_password' placeholder='当前密码'><input name='reason' placeholder='原因'><button>硬删除</button><output data-form-output></output></form></section>"""
    evidence_json = json.dumps([e.model_dump(mode="json") for e in memory.evidence], ensure_ascii=False, indent=2)
    edit_json = _esc(json.dumps(memory.model_dump(mode="json"), ensure_ascii=False, indent=2))
    body = f"""
    <article class="card detail"><h1>{_esc(memory.title)}</h1><p>{_esc(memory.summary)}</p><p><span class='chip status-{memory.memory_status.value}'>{memory.memory_status.value}</span><span class='chip'>{memory.memory_type.value}</span></p>
    <dl><dt>项目</dt><dd>{_esc(memory.project)}</dd><dt>主题</dt><dd>{_esc(memory.topic)}</dd><dt>创建时间</dt><dd>{_esc(memory.created_at)}</dd><dt>更新时间</dt><dd>{_esc(memory.updated_at)}</dd></dl>
    <h2>主张</h2><ul>{''.join(f'<li>{_esc(c.claim)} <span class="chip">{c.verification_status.value}</span></li>' for c in memory.claims)}</ul>
    <h2>证据</h2><pre>{_esc(evidence_json)}</pre></article>
    <section class="card"><h2>编辑</h2><form data-json-form method="patch" action="/admin/api/memories/{memory.memory_id}"><label class="span-all">记忆 JSON<textarea name="__json" class="json-editor">{edit_json}</textarea></label><button>预览差异并保存</button><output data-form-output></output></form></section>
    <section class="card"><h2>状态操作</h2><div class="actions"><form data-json-form method="post" action="/admin/api/memories/{memory.memory_id}/archive"><input name="reason" placeholder="原因"><button>归档</button></form><form data-json-form method="post" action="/admin/api/memories/{memory.memory_id}/soft-delete"><input name="reason" placeholder="原因"><button>软删除</button></form><form data-json-form method="post" action="/admin/api/memories/{memory.memory_id}/restore"><input name="reason" placeholder="原因"><button>恢复</button></form></div></section>{danger}
    """
    return _page(request, memory.title, body)


async def config_page(request: Request) -> Response:
    effective = _redact_effective(request.app.state.webui.resolver.effective())
    embedding = effective["embedding"]
    rerank = effective["rerank"]
    body = f"""
    <h1>配置</h1><section class='card'><p>运行时配置会热重载。环境变量优先级高于 WebUI 保存值。</p><pre>{_esc(json.dumps(effective, indent=2, ensure_ascii=False))}</pre></section>
    <form class="card form-grid" data-json-form method="patch" action="/admin/api/config/web-config">
      <label>检索模式<select name="retrieval.mode">{_selected_options(["keyword", "hybrid"], effective["retrieval"]["mode"]["value"])}</select></label>
      <label>启用 Embedding<select name="embedding.enabled" data-bool>{_selected_options(["false", "true"], str(embedding["enabled"]["value"]).lower())}</select></label>
      <label>Embedding Base URL<input name="embedding.base_url" value="{_esc(embedding['base_url']['value'] or '')}"></label>
      <label>Embedding 模型<input name="embedding.model" list="embedding-models" value="{_esc(embedding['model']['value'] or '')}"><datalist id="embedding-models"></datalist><button type="button" class="ghost" data-model-picker data-model-provider="embedding">获取模型列表</button><small data-model-status="embedding"></small></label>
      <label>Embedding 接口路径<input name="embedding.endpoint_path" value="{_esc(embedding['endpoint_path']['value'] or '/embeddings')}"></label><label>Embedding 超时秒数<input name="embedding.timeout_seconds" type="number" value="{_esc(embedding['timeout_seconds']['value'] or 30)}"></label>
      <label>Embedding 重试次数<input name="embedding.max_retries" type="number" value="{_esc(embedding['max_retries']['value'] or 1)}"></label>
      <label>启用 Rerank<select name="rerank.enabled" data-bool>{_selected_options(["false", "true"], str(rerank["enabled"]["value"]).lower())}</select></label>
      <label>Rerank Base URL<input name="rerank.base_url" value="{_esc(rerank['base_url']['value'] or '')}"></label>
      <label>Rerank 模型<input name="rerank.model" list="rerank-models" value="{_esc(rerank['model']['value'] or '')}"><datalist id="rerank-models"></datalist><button type="button" class="ghost" data-model-picker data-model-provider="rerank">获取模型列表</button><small data-model-status="rerank"></small></label>
      <label>Rerank 接口路径<input name="rerank.endpoint_path" value="{_esc(rerank['endpoint_path']['value'] or '/rerank')}"></label><label>Rerank 超时秒数<input name="rerank.timeout_seconds" type="number" value="{_esc(rerank['timeout_seconds']['value'] or 30)}"></label>
      <label>Rerank 重试次数<input name="rerank.max_retries" type="number" value="{_esc(rerank['max_retries']['value'] or 1)}"></label>
      <button>保存运行时配置</button><output data-form-output></output>
    </form>
    <form class="card form-grid" data-secret-form method="patch" action="/admin/api/config/secrets"><label>Embedding API Key<input type="password" name="embedding.api_key" autocomplete="off"></label><label>Rerank API Key<input type="password" name="rerank.api_key" autocomplete="off"></label><button>保存加密密钥</button><output data-form-output></output></form>
    """
    return _page(request, "配置", body)


async def nocturne_page(request: Request) -> Response:
    body = """<h1>Nocturne</h1><section class='card'><p>Nocturne v1 当前保留为测试配置：仅支持配置、令牌存储、状态和能力探测。同步、导入和写入 API 返回 not_implemented。</p></section><form class='card form-grid' data-json-form method='patch' action='/admin/api/config/web-config'><label>传输方式<select name='nocturne.transport'><option>unknown</option><option>rest</option><option>sse</option><option>streamable_http</option><option>stdio</option></select></label><label>URL<input name='nocturne.url'></label><button>保存 Nocturne 配置</button><output data-form-output></output></form><form class='card form-grid' data-secret-form method='patch' action='/admin/api/config/secrets'><label>令牌<input type='password' name='nocturne.token' autocomplete='off'></label><button>保存加密令牌</button><output data-form-output></output></form>"""
    return _page(request, "Nocturne", body)


async def security_page(request: Request) -> Response:
    csrf = request.app.state.webui.sessions.csrf(request)
    body = f"""
    <h1>安全</h1><section class="card"><form method="post" action="/admin/api/security/password?csrf_token={csrf}"><input name="current_password" type="password" placeholder="当前密码"><input name="new_password" type="password" placeholder="新密码"><button>修改密码</button></form></section>
    """
    return _page(request, "安全", body)


async def audit_page(request: Request) -> Response:
    events = request.app.state.webui.service.list_audit_events()
    return _page(request, "审计", f"<h1>审计</h1><pre>{_esc(json.dumps(events, indent=2, ensure_ascii=False))}</pre>")


async def import_page(request: Request) -> Response:
    body = """<h1>导入</h1><section class='card'><p>JSON 记忆导入支持 validate、skip_existing、overwrite_existing 和 import_as_new。Markdown 导入计划用于向量索引和 AI JSON 生成。</p></section><form class='card form-grid' data-json-import><label class='span-all'>JSON 导出内容<textarea name='memories' class='json-editor' placeholder='[{"project":"demo",...}]'></textarea></label><label>策略<select name='policy'><option>skip_existing</option><option>overwrite_existing</option><option>import_as_new</option></select></label><label>已确认<select name='confirmed' data-bool><option value='false'>false</option><option value='true'>true</option></select></label><button formaction='/admin/api/import/json/validate'>校验</button><button formaction='/admin/api/import/json/execute'>执行</button><output data-form-output></output></form>"""
    return _page(request, "导入", body)


async def exports_page(request: Request) -> Response:
    body = """<h1>导出</h1><section class='card'><p>导出 Markdown、JSON 或两者。默认只导出活跃记忆；归档和删除记忆需要显式包含。</p></section><form class='card form-grid' data-json-form method='post' action='/admin/api/export'><label>格式<select name='format'><option>both</option><option>json</option><option>markdown</option></select></label><label>包含归档<select name='include_archived' data-bool><option value='false'>false</option><option value='true'>true</option></select></label><label>包含删除<select name='include_deleted' data-bool><option value='false'>false</option><option value='true'>true</option></select></label><button>创建导出</button><output data-form-output></output></form>"""
    return _page(request, "导出", body)


async def api_memories(request: Request) -> Response:
    state = request.app.state.webui
    if request.method == "GET":
        memories = await _memory_list(request)
        return JSONResponse({"items": [m.model_dump(mode="json") for m in memories]})
    payload = await request.json()
    memory = ResearchMemory.model_validate(payload)
    overlaps = state.service.check_overlap(query=f"{memory.title} {memory.summary}", project=memory.project)
    if overlaps and not payload.get("confirmed"):
        return JSONResponse({"error": "overlap_confirmation_required", "overlap_candidates": overlaps}, status_code=409)
    saved = state.service.save_research_memory(user_confirmed=True, memory=payload)
    state.service.append_audit_event("memory.created", memory_id=saved.memory_id, metadata={"project": saved.project})
    return JSONResponse(saved.model_dump(mode="json"), status_code=201)


async def api_memory_detail(request: Request) -> Response:
    state = request.app.state.webui
    memory_id = request.path_params["memory_id"]
    if request.method == "GET":
        return JSONResponse(state.service.get_research_memory(memory_id).model_dump(mode="json"))
    payload = await request.json()
    updated = state.service.update_research_memory(memory_id, payload, user_confirmed=True)
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
    if policy == "overwrite_existing" and not confirmed:
        diffs: dict[str, str] = {}
        for item in payload.get("memories", []):
            incoming = ResearchMemory.model_validate(item)
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
        memory = ResearchMemory.model_validate(item)
        exists = state.service.backend.get(memory.memory_id) is not None
        if exists and policy == "skip_existing":
            skipped += 1
            continue
        data = memory.model_dump(mode="json")
        if policy == "import_as_new":
            data.pop("memory_id", None)
            data.setdefault("metadata", {})["imported_original_memory_id"] = memory.memory_id
        state.service.save_research_memory(user_confirmed=True, memory=data)
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


def _page(request: Request, title: str, body: str) -> HTMLResponse:
    csrf = request.app.state.webui.sessions.csrf(request)
    html = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><meta name="csrf-token" content="{csrf}"><title>{_esc(title)}</title><link rel="stylesheet" href="/admin/static/css/app.css"><script defer src="/admin/static/js/htmx.min.js"></script><script defer src="/admin/static/js/alpine.min.js"></script><script defer src="/admin/static/js/admin.js"></script></head><body><div class="bg"></div><div class="shell"><aside class="sidebar"><a class="brand" href="/admin">研究记忆</a><nav><a href="/admin/memories">记忆</a><a href="/admin/config">配置</a><a href="/admin/import">导入</a><a href="/admin/exports">导出</a><a href="/admin/audit">审计</a><a href="/admin/security">安全</a></nav><form method="post" action="/admin/logout?csrf_token={csrf}"><button class="ghost">退出</button></form></aside><main class="content"><header class="topbar"><span>{_esc(title)}</span><button class="ghost" data-theme-toggle>主题</button></header>{body}</main></div></body></html>"""
    return HTMLResponse(html)


def _login_html(error: str) -> str:
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><link rel="stylesheet" href="/admin/static/css/app.css"><title>登录</title></head><body class="login"><form class="login-card" method="post"><h1>管理员登录</h1><p class="error">{_esc(error)}</p><input type="password" name="password" required placeholder="密码"><button>登录</button></form></body></html>"""


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


def _memory_type_options() -> str:
    return "".join(f"<option value='{item.value}'>{item.value}</option>" for item in MemoryType)


def _selected_options(values: list[str], selected: Any) -> str:
    selected_value = str(selected)
    return "".join(
        f"<option value='{_esc(value)}'{' selected' if value == selected_value else ''}>{_esc(value)}</option>"
        for value in values
    )


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
            memory = ResearchMemory.model_validate(item)
            valid += 1
            if memory.memory_id in seen or service.backend.get(memory.memory_id) is not None:
                duplicate_ids.append(memory.memory_id)
            seen.add(memory.memory_id)
            overlaps.extend(service.check_overlap(query=f"{memory.title} {memory.summary}", project=memory.project, limit=3))
        except ValueError as exc:
            invalid += 1
            errors.append({"index": index, "error": str(exc)})
    return {"valid": valid, "invalid": invalid, "duplicate_memory_id": duplicate_ids, "overlap_candidates": overlaps, "conflicts": duplicate_ids, "errors": errors}


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
