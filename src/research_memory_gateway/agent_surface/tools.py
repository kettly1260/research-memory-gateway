from __future__ import annotations

from typing import Any, Literal

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from ..service import ResearchMemoryService
from .capture import capture_memory as capture_memory_impl
from .recall import get_project_state as get_project_state_impl
from .recall import recall_memory as recall_memory_impl
from .upload_tools import (
    conversation_ingest_snapshot as conversation_ingest_snapshot_impl,
    conversation_ingest_turn as conversation_ingest_turn_impl,
    conversation_upload_abort as conversation_upload_abort_impl,
    conversation_upload_commit as conversation_upload_commit_impl,
    conversation_upload_create as conversation_upload_create_impl,
    conversation_upload_status as conversation_upload_status_impl,
)
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
CONVERSATION_UPLOAD_TOOL_NAMES = (
    "conversation_upload_create",
    "conversation_upload_status",
    "conversation_upload_abort",
    "conversation_upload_commit",
    "conversation_ingest_turn",
    "conversation_ingest_snapshot",
)
AGENT_TOOL_NAMES = CORE_AGENT_TOOL_NAMES


def register_agent_tools(mcp: MCPServer, service: ResearchMemoryService) -> None:
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
        if getattr(service.config, "upload", None) and service.config.upload.enabled:
            register_upload_tools(mcp, service)


def register_conversation_tools(mcp: MCPServer, service: ResearchMemoryService) -> None:
    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def conversation_search(
        query: str,
        project: str | None = None,
        conversation_id: str | None = None,
        parent_thread_id: str | None = None,
        source_system: str | None = None,
        thread_source: str | None = None,
        canonical_conversation_id: str | None = None,
        source_key: str | None = None,
        source_conversation_id: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Search across conversation Markdown archives using hybrid lexical and vector ranking.

        Returns path, title, heading, excerpt, scores, date, project, conversation ID,
        parent thread ID, plus v0.2.4 source identity fields (source_system,
        source_key, canonical_conversation_id, source_conversation_id).
        A bare conversation_id shared by several source systems returns an
        explicit ambiguous_conversation_id result; disambiguate with
        source_system / source_key / canonical_conversation_id.
        """
        return service.conversation_retrieval.search(
            query=query,
            project=project,
            conversation_id=conversation_id,
            parent_thread_id=parent_thread_id,
            source_system=source_system,
            thread_source=thread_source,
            canonical_conversation_id=canonical_conversation_id,
            source_key=source_key,
            source_conversation_id=source_conversation_id,
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
        source_system: str | None = None,
        thread_source: str | None = None,
        canonical_conversation_id: str | None = None,
        source_key: str | None = None,
        source_conversation_id: str | None = None,
        collapse_canonical: bool = True,
    ) -> dict[str, Any]:
        """Retrieve and format relevant conversation excerpts for direct agent context.

        Respects token budget and preserves source anchors and provenance links.
        By default (collapse_canonical=true) confirmed duplicates of the same
        canonical conversation from different sources contribute only their
        best section, so duplicated exports do not fill the context twice.
        Pass collapse_canonical=false for source-level audit output.
        """
        return service.conversation_retrieval.recall(
            query=query,
            token_budget=token_budget,
            project=project,
            conversation_id=conversation_id,
            parent_thread_id=parent_thread_id,
            source_system=source_system,
            thread_source=thread_source,
            canonical_conversation_id=canonical_conversation_id,
            source_key=source_key,
            source_conversation_id=source_conversation_id,
            collapse_canonical=collapse_canonical,
        )


def register_upload_tools(mcp: MCPServer, service: ResearchMemoryService) -> None:
    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
    def conversation_upload_create(
        filename: str,
        size_bytes: int,
        sha256: str,
        content_type: str = "application/zip",
        source_hint: str | None = None,
        expiry_hours: int | None = None,
    ) -> dict[str, Any]:
        """Create a resumable upload session for conversation exports (ZIP).

        Returns upload_id and standard tus 1.0.0 upload_url (/uploads/{upload_id}).
        Upload chunks directly to upload_url using standard tus protocol.
        """
        return conversation_upload_create_impl(
            service,
            filename=filename,
            size_bytes=size_bytes,
            sha256=sha256,
            content_type=content_type,
            source_hint=source_hint,
            expiry_hours=expiry_hours,
        )

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def conversation_upload_status(upload_id: str) -> dict[str, Any]:
        """Query real-time status of an in-flight or completed upload session."""
        return conversation_upload_status_impl(service, upload_id=upload_id)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
    def conversation_upload_abort(upload_id: str) -> dict[str, Any]:
        """Cancel and clean up an in-flight upload session."""
        return conversation_upload_abort_impl(service, upload_id=upload_id)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
    def conversation_upload_commit(
        upload_id: str,
        vault_confirmed: bool = False,
        target_vault: str | None = None,
        format_hint: str = "auto",
        account_namespace: str = "",
        use_default_account_namespace: bool = False,
        dry_run: bool = False,
        resume: bool = True,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Finalize and commit an uploaded conversation export into the archive.

        Verifies SHA-256 integrity, detects format (ChatGPT or Codex), processes
        conversation graph, generates Markdown notes, and updates the search index.
        """
        return conversation_upload_commit_impl(
            service,
            upload_id=upload_id,
            vault_confirmed=vault_confirmed,
            target_vault=target_vault,
            format_hint=format_hint,
            account_namespace=account_namespace,
            use_default_account_namespace=use_default_account_namespace,
            dry_run=dry_run,
            resume=resume,
            limit=limit,
        )

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
    def conversation_ingest_turn(
        session_id: str,
        role: str,
        content: str,
        title: str | None = None,
        model: str | None = None,
        timestamp: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Directly ingest a conversation turn into the archive and index for instant recall."""
        return conversation_ingest_turn_impl(
            service,
            session_id=session_id,
            role=role,
            content=content,
            title=title,
            model=model,
            timestamp=timestamp,
            metadata=metadata,
        )

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
    def conversation_ingest_snapshot(
        session_id: str,
        messages: list[dict[str, Any]],
        title: str | None = None,
        model: str | None = None,
        metadata: dict[str, Any] | None = None,
        overwrite: bool = True,
    ) -> dict[str, Any]:
        """Directly ingest a full conversation snapshot (multiple messages) into the archive and index."""
        return conversation_ingest_snapshot_impl(
            service,
            session_id=session_id,
            messages=messages,
            title=title,
            model=model,
            metadata=metadata,
            overwrite=overwrite,
        )
