from __future__ import annotations

import argparse
import asyncio
import logging
import os
from typing import Any

from mcp.server.fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
import uvicorn

from .backends import build_backend
from .config import AppConfig, load_config
from .models import ExportFormat
from .service import ResearchMemoryService, serialize_results
from .webui.app import build_webui_app


logger = logging.getLogger(__name__)


class BearerAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: Any, token: str) -> None:
        super().__init__(app)
        self.token = token

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        if request.url.path in {"/health", "/healthz"}:
            return await call_next(request)
        authorization = request.headers.get("Authorization", "")
        if authorization != f"Bearer {self.token}":
            logger.warning("Rejected unauthenticated request path=%s", request.url.path)
            return Response("Unauthorized", status_code=401)
        return await call_next(request)


def build_mcp(config: AppConfig) -> FastMCP:
    backend = build_backend(config)
    service = ResearchMemoryService(config, backend)
    mcp = FastMCP(config.server.name)

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
    parser.add_argument("--transport", choices=["stdio", "sse"], default="stdio")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    if args.host is not None:
        config.server.host = args.host
    if args.port is not None:
        config.server.port = args.port

    auth_token = os.getenv(config.server.auth_token_env)
    if args.transport == "sse" and not auth_token:
        # FastMCP itself does not enforce bearer auth here. Keep the deployment default explicit.
        print(
            f"Warning: {config.server.auth_token_env} is not set. "
            "Put this behind Tailscale/WireGuard or an authenticated reverse proxy."
        )

    mcp = build_mcp(config)
    if args.transport == "stdio":
        mcp.run(transport="stdio")
    elif auth_token:
        app = mcp.sse_app()
        app.add_middleware(BearerAuthMiddleware, token=auth_token)
        _run_http_apps(config, app)
    else:
        mcp.settings.host = config.server.host
        mcp.settings.port = config.server.port
        if config.webui.enabled:
            app = mcp.sse_app()
            _run_http_apps(config, app)
        else:
            mcp.run(transport="sse")


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
