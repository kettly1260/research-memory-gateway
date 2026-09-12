from __future__ import annotations

from typing import Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from ..service import ResearchMemoryService
from .capture import capture_memory as capture_memory_impl
from .recall import get_project_state as get_project_state_impl
from .recall import recall_memory as recall_memory_impl
from .verify import verify_memory as verify_memory_impl

CORE_AGENT_TOOL_NAMES = (
    "recall_memory",
    "capture_memory",
    "verify_memory",
    "get_project_state",
)
CONVERSATION_TOOL_NAMES = (
    "conversation_search",
    "conversation_read",
    "conversation_recall",
)
AGENT_TOOL_NAMES = CORE_AGENT_TOOL_NAMES


def register_agent_tools(mcp: FastMCP, service: ResearchMemoryService) -> None:
    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
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
        Only query is required; keep context_mode=compact unless more context is needed. Pass
        project only when you know the exact gateway project label. Do not use the current working
        directory or repository path as project; omit it when uncertain.
        """
        return recall_memory_impl(
            service,
            query=query,
            project=project,
            limit=limit,
            context_mode=context_mode,
        )

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
    def capture_memory(
        content: str,
        project: str | None = None,
        source_context: str = "current conversation",
        source_client: str | None = None,
        conversation_id: str | None = None,
        message_id: str | None = None,
        session_id: str | None = None,
        source_timestamp: str | None = None,
        importance: Literal["auto", "low", "normal", "high"] = "auto",
        user_confirmed: bool = False,
    ) -> dict[str, Any]:
        """Capture durable reusable information with a low-cost agent-facing schema.

        Call this when the current interaction produces information worth reusing in future
        sessions: project paths or state, stable configurations, workflows, decisions,
        experiment conditions/results, generic material measurements/properties, characterization
        observations, solution preparation details, literature conclusions, or durable preferences.
        The gateway classifies Ambient versus Trusted Research Memory, builds the internal
        schema, checks overlap, and either saves ambient memory or queues trusted research
        memory for review. Set user_confirmed=true only when the user explicitly asked to
        remember/save this specific information. Do not intentionally send credentials. When
        available, pass the client/conversation/message/session/timestamp fields so provenance
        can be anchored beyond the generic "current conversation" label. Leave importance=auto
        unless priority is known; the other allowed values are low, normal, and high.
        """
        return capture_memory_impl(
            service,
            content=content,
            project=project,
            source_context=source_context,
            source_client=source_client,
            conversation_id=conversation_id,
            message_id=message_id,
            session_id=session_id,
            source_timestamp=source_timestamp,
            importance=importance,
            user_confirmed=user_confirmed,
        )

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def verify_memory(memory_id: str, claim_id: str | None = None) -> dict[str, Any]:
        """Verify the provenance and scientific reliability of a recalled memory.

        Use after recall_memory when the user asks whether a historical fact is certain,
        where a number came from, which source supports a conclusion, or when an experimental
        decision depends on provenance. Returns claims, verification status, evidence,
        source references, and conflict/superseded information.
        """
        return verify_memory_impl(service, memory_id=memory_id, claim_id=claim_id)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def get_project_state(project: str, limit: int = 8) -> dict[str, Any]:
        """Return the recent active state and pending memory proposals for one project.

        Use this when resuming a known project and a concise checkpoint is more useful than
        a free-text recall query.
        """
        return get_project_state_impl(service, project=project, limit=limit)

    if getattr(service.config, "conversation_archive", None) and service.config.conversation_archive.enabled:
        register_conversation_tools(mcp, service)


def register_conversation_tools(mcp: FastMCP, service: ResearchMemoryService) -> None:
    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def conversation_search(
        query: str,
        project: str | None = None,
        conversation_id: str | None = None,
        parent_thread_id: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Search across conversation Markdown archives using hybrid lexical and vector ranking.

        Returns path, title, heading, excerpt, scores, date, project, conversation ID, and parent thread ID.
        """
        return service.conversation_retrieval.search(
            query=query,
            project=project,
            conversation_id=conversation_id,
            parent_thread_id=parent_thread_id,
            limit=limit,
        )

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def conversation_read(file_path: str, heading: str | None = None) -> dict[str, Any]:
        """Read a verified conversation archive note or specific heading section.

        Path must be strictly within configured staging or canonical vault roots.
        """
        return service.conversation_retrieval.read(file_path=file_path, heading=heading)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def conversation_recall(
        query: str,
        token_budget: int = 1500,
        project: str | None = None,
        conversation_id: str | None = None,
        parent_thread_id: str | None = None,
    ) -> dict[str, Any]:
        """Retrieve and format relevant conversation excerpts for direct agent context.

        Respects token budget and preserves source anchors and provenance links.
        """
        return service.conversation_retrieval.recall(
            query=query,
            token_budget=token_budget,
            project=project,
            conversation_id=conversation_id,
            parent_thread_id=parent_thread_id,
        )
