from __future__ import annotations

from collections import Counter
from typing import Any

from ..models import ResearchMemory, SearchResult, VerificationStatus
from ..service import ResearchMemoryService

_VERIFICATION_PRIORITY = (
    VerificationStatus.conflicting,
    VerificationStatus.retracted,
    VerificationStatus.superseded,
    VerificationStatus.unverified,
    VerificationStatus.inferred,
    VerificationStatus.evidence_backed,
)


def recall_memory(
    service: ResearchMemoryService,
    *,
    query: str,
    project: str | None = None,
    limit: int | None = None,
    context_mode: str = "compact",
) -> dict[str, Any]:
    """Return a small, agent-friendly long-term-memory context."""
    normalized_query = query.strip()
    if not normalized_query:
        raise ValueError("query must not be empty")
    if context_mode not in {"compact", "standard"}:
        raise ValueError("context_mode must be compact or standard")

    requested_limit = limit if limit is not None else service.config.memory.recall_default_limit
    safe_limit = max(1, min(requested_limit, service.config.memory.recall_max_limit))
    project_hint = project.strip() if project and project.strip() else None
    results = service.search_research_memory(
        query=normalized_query,
        project=project_hint,
        include_archived=False,
        include_deleted=False,
        limit=safe_limit,
    )

    inferred_project = project_hint or _infer_project(results)
    return {
        "query": normalized_query,
        "project": inferred_project,
        "context_mode": context_mode,
        "result_count": len(results),
        "results": [_serialize_result(item, context_mode=context_mode) for item in results],
    }


def get_project_state(
    service: ResearchMemoryService,
    *,
    project: str,
    limit: int = 8,
) -> dict[str, Any]:
    """Return recent active memories and pending work for one project."""
    normalized_project = project.strip()
    if not normalized_project:
        raise ValueError("project must not be empty")
    safe_limit = max(1, min(limit, service.config.memory.recall_max_limit))
    results = service.search_research_memory(
        query="",
        project=normalized_project,
        include_archived=False,
        include_deleted=False,
        limit=safe_limit,
    )
    next_actions: list[str] = []
    for result in results:
        for action in result.memory.next_actions:
            if action and action not in next_actions:
                next_actions.append(action)

    pending = [
        proposal
        for proposal in service.list_memory_proposals(status="pending", limit=200)
        if proposal.suggested_memory.project == normalized_project
    ]
    return {
        "project": normalized_project,
        "memory_count": len(results),
        "recent": [_serialize_result(item, context_mode="standard") for item in results],
        "next_actions": next_actions[:10],
        "pending_proposals": [
            {
                "proposal_id": proposal.proposal_id,
                "title": proposal.suggested_memory.title,
                "updated_at": proposal.updated_at,
            }
            for proposal in pending[:10]
        ],
    }


def verification_summary(memory: ResearchMemory) -> str:
    if not memory.claims:
        return VerificationStatus.unverified.value
    statuses = {claim.verification_status for claim in memory.claims}
    for status in _VERIFICATION_PRIORITY:
        if status in statuses:
            return status.value
    return VerificationStatus.unverified.value


def _serialize_result(result: SearchResult, *, context_mode: str) -> dict[str, Any]:
    memory = result.memory
    item: dict[str, Any] = {
        "memory_id": memory.memory_id,
        "title": memory.title,
        "content": memory.summary[:800],
        "verification": verification_summary(memory),
        "project": memory.project,
        "updated_at": memory.updated_at,
        "source_available": bool(memory.source_refs or memory.evidence),
    }
    if context_mode == "standard":
        item.update(
            {
                "topic": memory.topic,
                "memory_tier": memory.memory_tier.value,
                "memory_type": memory.memory_type.value,
                "next_actions": memory.next_actions[:5],
                "match_reason": result.match_reason,
                "score": result.score,
            }
        )
    return item


def _infer_project(results: list[SearchResult]) -> str | None:
    if not results:
        return None
    counts = Counter(item.memory.project for item in results)
    project, count = counts.most_common(1)[0]
    if len(counts) == 1 or count >= 2:
        return project
    return None
