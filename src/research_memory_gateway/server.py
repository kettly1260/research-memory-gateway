from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sqlite3
import hashlib
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.responses import Response
from starlette.routing import Mount, Route
from starlette.types import ASGIApp, Receive, Scope, Send
import uvicorn

from .backends import build_backend
from .config import AppConfig, load_config
from .models import ExportFormat
from .service import ResearchMemoryService, serialize_results
from .webui.app import build_webui_app


logger = logging.getLogger(__name__)


class BearerAuthMiddleware:
    def __init__(self, app: ASGIApp, token: str | None = None, sqlite_path: str = "") -> None:
        self.app = app
        self.token = token
        self.sqlite_path = sqlite_path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") in {"/health", "/healthz"}:
            await self.app(scope, receive, send)
            return

        authorization = ""
        for raw_name, raw_value in scope.get("headers", []):
            if raw_name.lower() == b"authorization":
                authorization = raw_value.decode("latin-1")
                break

        if not authorization.startswith("Bearer "):
            logger.warning("Rejected unauthenticated request path=%s: Missing Bearer token", scope.get("path"))
            response = Response("Unauthorized", status_code=401)
            await response(scope, receive, send)
            return

        token_val = authorization[7:]
        authenticated = False
        key_id = None

        # 1. 优先校验内置 master token
        if self.token and token_val == self.token:
            authenticated = True
        elif os.path.exists(self.sqlite_path):
            # 2. 从数据库中进行 API Key 匹配 (SHA-256)
            token_hash = hashlib.sha256(token_val.encode("utf-8")).hexdigest()
            try:
                with sqlite3.connect(self.sqlite_path) as conn:
                    conn.row_factory = sqlite3.Row
                    row = conn.execute(
                        "SELECT key_id, name FROM api_keys WHERE key_hash = ? AND status = 'active'",
                        (token_hash,)
                    ).fetchone()
                    if row:
                        authenticated = True
                        key_id = row["key_id"]
            except Exception as e:
                logger.error("Database error in BearerAuthMiddleware: %s", e)

        if not authenticated:
            logger.warning("Rejected unauthorized request path=%s", scope.get("path"))
            response = Response("Unauthorized", status_code=401)
            await response(scope, receive, send)
            return

        # 3. 校验通过，如果是 API Key，则记录活跃连接
        if key_id:
            client = scope.get("client")
            client_ip = client[0] if client else "unknown"

            user_agent = "unknown"
            for raw_name, raw_value in scope.get("headers", []):
                if raw_name.lower() == b"user-agent":
                    user_agent = raw_value.decode("latin-1")
                    break

            try:
                with sqlite3.connect(self.sqlite_path) as conn:
                    conn.execute(
                        """
                        INSERT INTO active_connections(key_id, client_ip, client_info, request_count, last_request_at)
                        VALUES (?, ?, ?, 1, ?)
                        ON CONFLICT(key_id, client_ip) DO UPDATE SET
                            request_count = request_count + 1,
                            last_request_at = excluded.last_request_at,
                            client_info = excluded.client_info
                        """,
                        (key_id, client_ip, user_agent, datetime.now(timezone.utc).isoformat())
                    )
                    conn.execute(
                        "UPDATE api_keys SET last_used_at = ? WHERE key_id = ?",
                        (datetime.now(timezone.utc).isoformat(), key_id)
                    )
            except Exception as e:
                logger.error("Database error recording active connection: %s", e)

        await self.app(scope, receive, send)


def build_mcp(config: AppConfig) -> FastMCP:
    backend = build_backend(config)
    service = ResearchMemoryService(config, backend)
    mcp = FastMCP(config.server.name, host=config.server.host, port=config.server.port)

    @mcp.tool()
    def propose_save(
        reason: str,
        suggested_memory: dict[str, Any],
        check_overlap: bool = True,
    ) -> dict[str, Any]:
        """Propose saving a durable research memory without writing it.

        Use this only for reusable scientific assets. The user must confirm before save.
        """
        proposal = service.propose_save(
            reason=reason,
            suggested_memory=suggested_memory,
            check_overlap=check_overlap,
        )
        logger.info("Proposed memory save project=%s", proposal.suggested_memory.project)
        return proposal.model_dump(mode="json")

    @mcp.tool()
    def save_research_memory(
        user_confirmed: bool,
        proposal_id: str | None = None,
        memory: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Save a research memory after explicit user confirmation.

        Provide either a proposal_id returned by propose_save or a full memory object.
        """
        saved = service.save_research_memory(
            user_confirmed=user_confirmed,
            proposal_id=proposal_id,
            memory=memory,
        )
        logger.info("Saved research memory memory_id=%s project=%s", saved.memory_id, saved.project)
        return saved.model_dump(mode="json")

    @mcp.tool()
    def search_research_memory(
        query: str,
        project: str | None = None,
        memory_type: str | None = None,
        include_archived: bool = False,
        include_deleted: bool = False,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Search saved research memories by text, project, and memory type."""
        results = service.search_research_memory(
            query=query,
            project=project,
            memory_type=memory_type,
            include_archived=include_archived,
            include_deleted=include_deleted,
            limit=limit,
        )
        logger.info(
            "Searched research memory project=%s memory_type=%s result_count=%s",
            project,
            memory_type,
            len(results),
        )
        return serialize_results(results)

    @mcp.tool()
    def check_overlap(
        query: str,
        project: str | None = None,
        memory_type: str | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Check for duplicate, similar, or conflicting existing memories."""
        return service.check_overlap(
            query=query,
            project=project,
            memory_type=memory_type,
            limit=limit,
        )

    @mcp.tool()
    def get_research_memory(memory_id: str) -> dict[str, Any]:
        """Get one saved research memory by id."""
        return service.get_research_memory(memory_id).model_dump(mode="json")

    @mcp.tool()
    def update_research_memory(
        memory_id: str,
        updates: dict[str, Any],
        user_confirmed: bool,
    ) -> dict[str, Any]:
        """Update a saved research memory after explicit confirmation."""
        updated = service.update_research_memory(
            memory_id=memory_id,
            updates=updates,
            user_confirmed=user_confirmed,
        )
        return updated.model_dump(mode="json")

    @mcp.tool()
    def delete_research_memory(memory_id: str, user_confirmed: bool) -> dict[str, Any]:
        """Delete a research memory after explicit confirmation."""
        return service.delete_research_memory(memory_id=memory_id, user_confirmed=user_confirmed)

    @mcp.tool()
    def mark_memory_status(
        memory_id: str,
        status: str,
        reason: str,
        user_confirmed: bool,
    ) -> dict[str, Any]:
        """Mark all claims in a memory as superseded, retracted, or conflicting."""
        marked = service.mark_memory_status(
            memory_id=memory_id,
            status=status,
            reason=reason,
            user_confirmed=user_confirmed,
        )
        return marked.model_dump(mode="json")

    @mcp.tool()
    def merge_research_memories(
        source_memory_ids: list[str],
        merged_memory: dict[str, Any],
        reason: str,
        user_confirmed: bool,
    ) -> dict[str, Any]:
        """Save a merged memory and mark source memories superseded."""
        merged = service.merge_research_memories(
            source_memory_ids=source_memory_ids,
            merged_memory=merged_memory,
            reason=reason,
            user_confirmed=user_confirmed,
        )
        return merged.model_dump(mode="json")

    @mcp.tool()
    def open_source_ref(source_ref: dict[str, Any], max_chars: int = 4000) -> dict[str, Any]:
        """Resolve a source reference from the read-only allowlist, DOI, or URL."""
        return service.open_source_ref(source_ref=source_ref, max_chars=max_chars)

    @mcp.tool()
    def audit_unverified(
        project: str | None = None,
        include_inferred: bool = True,
        include_archived: bool = False,
        include_deleted: bool = False,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Find saved claims that are unverified, inferred, or missing evidence links."""
        return service.audit_unverified(
            project=project,
            include_inferred=include_inferred,
            include_archived=include_archived,
            include_deleted=include_deleted,
            limit=limit,
        )

    @mcp.tool()
    def health() -> dict[str, Any]:
        """Report service, backend, and retrieval health diagnostics."""
        return service.health()

    @mcp.tool()
    def audit_database_integrity(
        repair_fts: bool = False,
        repair_orphan_embeddings: bool = False,
    ) -> dict[str, Any]:
        """Audit SQLite memory, FTS, embeddings, and source-ref integrity."""
        return service.audit_database_integrity(
            repair_fts=repair_fts,
            repair_orphan_embeddings=repair_orphan_embeddings,
        )

    @mcp.tool()
    def retrieval_health() -> dict[str, Any]:
        """Report retrieval backend, embedding, rerank, and vector diagnostics."""
        return service.retrieval_health()

    @mcp.tool()
    def export_memories(
        export_format: str = "both",
        include_archived: bool = False,
        include_deleted: bool = False,
    ) -> dict[str, Any]:
        """Export memories to Markdown, JSON, or both."""
        parsed_format = ExportFormat(export_format)
        return service.export_memories(
            export_format=parsed_format,
            include_archived=include_archived,
            include_deleted=include_deleted,
        )

    return mcp


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Research Memory Gateway MCP server")
    parser.add_argument("--config", default="config.yaml", help="Path to config YAML")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "streamable-http", "both"],
        default="stdio",
        help=(
            "MCP transport. 'streamable-http' serves POST /mcp (recommended for "
            "Codex / Antigravity). 'sse' serves legacy GET /sse + POST /messages/. "
            "'both' mounts both transports on the same port."
        ),
    )
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    return parser.parse_args()


def _build_streamable_http_app(mcp: FastMCP, auth_token: str | None, config: AppConfig) -> Starlette:
    """Build a Starlette app serving only Streamable HTTP at /mcp."""
    app = mcp.streamable_http_app()
    if auth_token or config.backend.type == "sqlite":
        app.add_middleware(BearerAuthMiddleware, token=auth_token, sqlite_path=config.backend.sqlite_path)
    return app


def _build_sse_app(mcp: FastMCP, auth_token: str | None, config: AppConfig) -> Starlette:
    """Build a Starlette app serving only legacy SSE at /sse + /messages/."""
    app = mcp.sse_app()
    if auth_token or config.backend.type == "sqlite":
        app.add_middleware(BearerAuthMiddleware, token=auth_token, sqlite_path=config.backend.sqlite_path)
    return app


def _build_combined_app(mcp: FastMCP, auth_token: str | None, config: AppConfig) -> Starlette:
    """Build a Starlette app serving both Streamable HTTP (/mcp) and legacy SSE (/sse).

    We mount both transports on the same port so that:
      - Codex / Antigravity ``type = "remote"`` + ``url = ".../mcp"`` works.
      - Legacy SSE clients (``GET /sse``, ``POST /messages/``) still work.
    """
    # Get the two sub-apps. Both share the same underlying mcp._mcp_server.
    shttp_app = mcp.streamable_http_app()
    sse_app = mcp.sse_app()

    # Extract the session_manager lifespan from the streamable HTTP app.
    shttp_lifespan = shttp_app.router.lifespan_context

    @asynccontextmanager
    async def combined_lifespan(app: Starlette):
        async with shttp_lifespan(app):
            yield

    # Collect routes from both apps.
    # Streamable HTTP: Route("/mcp", ...)
    # SSE: Route("/sse", GET), Mount("/messages/", POST)
    combined_routes: list[Route | Mount] = []
    combined_routes.extend(shttp_app.routes)  # /mcp
    combined_routes.extend(sse_app.routes)     # /sse + /messages/

    app = Starlette(
        debug=mcp.settings.debug,
        routes=combined_routes,
        lifespan=combined_lifespan,
    )
    if auth_token or config.backend.type == "sqlite":
        app.add_middleware(BearerAuthMiddleware, token=auth_token, sqlite_path=config.backend.sqlite_path)
    return app


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    if args.host is not None:
        config.server.host = args.host
    if args.port is not None:
        config.server.port = args.port

    auth_token = os.getenv(config.server.auth_token_env)
    transport = args.transport

    if transport != "stdio" and not auth_token:
        print(
            f"Warning: {config.server.auth_token_env} is not set. "
            "Put this behind Tailscale/WireGuard or an authenticated reverse proxy."
        )

    mcp = build_mcp(config)

    if transport == "stdio":
        mcp.run(transport="stdio")
        return

    # Build the appropriate ASGI app based on transport choice.
    if transport == "streamable-http":
        app = _build_streamable_http_app(mcp, auth_token, config)
    elif transport == "sse":
        app = _build_sse_app(mcp, auth_token, config)
    elif transport == "both":
        app = _build_combined_app(mcp, auth_token, config)
    else:
        raise ValueError(f"Unknown transport: {transport}")

    logger.info(
        "Starting Research Memory Gateway transport=%s host=%s port=%s",
        transport,
        config.server.host,
        config.server.port,
    )
    _run_http_apps(config, app)


def _run_http_apps(config: AppConfig, mcp_app: Any) -> None:
    if not config.webui.enabled:
        uvicorn.run(mcp_app, host=config.server.host, port=config.server.port)
        return

    async def runner() -> None:
        webui_app = build_webui_app(config)
        mcp_server = uvicorn.Server(
            uvicorn.Config(mcp_app, host=config.server.host, port=config.server.port, log_level="info")
        )
        webui_server = uvicorn.Server(
            uvicorn.Config(webui_app, host=config.webui.host, port=config.webui.port, log_level="info")
        )
        await asyncio.gather(mcp_server.serve(), webui_server.serve())

    asyncio.run(runner())


if __name__ == "__main__":
    main()
