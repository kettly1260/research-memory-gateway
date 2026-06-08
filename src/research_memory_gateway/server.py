from __future__ import annotations

import argparse
import os
from typing import Any

from mcp.server.fastmcp import FastMCP

from .backends import build_backend
from .config import AppConfig, load_config
from .models import ExportFormat
from .service import ResearchMemoryService, serialize_results


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
        return saved.model_dump(mode="json")

    @mcp.tool()
    def search_research_memory(
        query: str,
        project: str | None = None,
        memory_type: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Search saved research memories by text, project, and memory type."""
        results = service.search_research_memory(
            query=query,
            project=project,
            memory_type=memory_type,
            limit=limit,
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
    def open_source_ref(source_ref: dict[str, Any], max_chars: int = 4000) -> dict[str, Any]:
        """Resolve a source reference from the read-only allowlist, DOI, or URL."""
        return service.open_source_ref(source_ref=source_ref, max_chars=max_chars)

    @mcp.tool()
    def audit_unverified(
        project: str | None = None,
        include_inferred: bool = True,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Find saved claims that are unverified, inferred, or missing evidence links."""
        return service.audit_unverified(
            project=project,
            include_inferred=include_inferred,
            limit=limit,
        )

    @mcp.tool()
    def export_memories(export_format: str = "both") -> dict[str, Any]:
        """Export memories to Markdown, JSON, or both."""
        parsed_format = ExportFormat(export_format)
        return service.export_memories(export_format=parsed_format)

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
    else:
        mcp.settings.host = config.server.host
        mcp.settings.port = config.server.port
        mcp.run(transport="sse")


if __name__ == "__main__":
    main()
