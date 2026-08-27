from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from ..service import ResearchMemoryService
from .capture import capture_memory as capture_memory_impl
from .recall import get_project_state as get_project_state_impl
from .recall import recall_memory as recall_memory_impl
from .verify import verify_memory as verify_memory_impl

AGENT_TOOL_NAMES = (
    "recall_memory",
    "capture_memory",
    "verify_memory",
    "get_project_state",
)


def register_agent_tools(mcp: FastMCP, service: ResearchMemoryService) -> None:
    @mcp.tool()
    def recall_memory(
        query: str,
        project: str | None = None,
        limit: int = 5,
        context_mode: str = "compact",
    ) -> dict[str, Any]:
        """Retrieve relevant long-term memory from the user's previous research and projects.

        Use this whenever previous user-specific or project-specific context may materially
        affect the answer. Strong triggers include previous/earlier/last time/before,
        continue/resume, what we decided, prior experiment conditions, established files,
        paths, tools, settings or preferences, and 之前/上次/以前/继续/还记得/原来/我们做过/怎么配的.
        Do not guess historical user-specific facts when this tool can retrieve them.
        Only query is required; keep context_mode=compact unless more context is needed.
        """
        return recall_memory_impl(
            service,
            query=query,
            project=project,
            limit=limit,
            context_mode=context_mode,
        )

    @mcp.tool()
    def capture_memory(
        content: str,
        project: str | None = None,
        source_context: str = "current conversation",
        importance: str = "auto",
        user_confirmed: bool = False,
    ) -> dict[str, Any]:
        """Capture durable reusable information with a low-cost agent-facing schema.

        Call this when the current interaction produces information worth reusing in future
        sessions: project paths or state, stable configurations, workflows, decisions,
        experiment conditions/results, solution preparation details, or durable preferences.
        The gateway classifies Ambient versus Trusted Research Memory, builds the internal
        schema, checks overlap, and either saves ambient memory or queues trusted research
        memory for review. Set user_confirmed=true only when the user explicitly asked to
        remember/save this specific information.
        """
        return capture_memory_impl(
            service,
            content=content,
            project=project,
            source_context=source_context,
            importance=importance,
            user_confirmed=user_confirmed,
        )

    @mcp.tool()
    def verify_memory(memory_id: str, claim_id: str | None = None) -> dict[str, Any]:
        """Verify the provenance and scientific reliability of a recalled memory.

        Use after recall_memory when the user asks whether a historical fact is certain,
        where a number came from, which source supports a conclusion, or when an experimental
        decision depends on provenance. Returns claims, verification status, evidence,
        source references, and conflict/superseded information.
        """
        return verify_memory_impl(service, memory_id=memory_id, claim_id=claim_id)

    @mcp.tool()
    def get_project_state(project: str, limit: int = 8) -> dict[str, Any]:
        """Return the recent active state and pending memory proposals for one project.

        Use this when resuming a known project and a concise checkpoint is more useful than
        a free-text recall query.
        """
        return get_project_state_impl(service, project=project, limit=limit)
