from __future__ import annotations

import hashlib
import re
from typing import Any

from ..models import (
    Claim,
    Confidence,
    Entity,
    Evidence,
    MemoryTier,
    MemoryType,
    ResearchMemory,
    SourceRef,
    VerificationStatus,
)
from ..service import ResearchMemoryService

_RESEARCH_TERMS = (
    "实验",
    "测试",
    "样品",
    "配制",
    "配液",
    "母液",
    "浓度",
    "溶液",
    "溶剂",
    "合成",
    "反应",
    "产率",
    "发射",
    "激发",
    "峰位",
    "光谱",
    "lod",
    "hepes",
    "dmso",
    "fe3+",
    "hno3",
    "uv-vis",
    "xps",
    "lc-ms",
    "nmr",
    "photoluminescence",
    "fluorescence",
    "spectrum",
    "sample",
    "stock solution",
    "concentration",
    "reaction",
)
_STRONG_RESEARCH_TERMS = (
    "配制",
    "配液",
    "母液",
    "发射",
    "激发",
    "峰位",
    "lod",
    "dmso",
    "hepes",
    "fe3+",
    "hno3",
    "uv-vis",
    "xps",
    "lc-ms",
    "nmr",
    "荧光",
    "滤光片",
    "发光强度",
    "pl ",
)
_AMBIENT_TERMS = (
    "路径",
    "目录",
    "仓库",
    "工作区",
    "配置",
    "设置",
    "mcp",
    "agent",
    "workflow",
    "repository",
    "workspace",
    "config",
    "branch",
    "分支",
    "下一步",
    "已完成",
    "偏好",
    "prefer",
    "memory",
    "proposal",
    "记忆",
    "提案",
    "retrieval",
    "embedding",
    "rerank",
    "sqlite",
    "fts",
    "streamable http",
    "sse",
    "zotero",
    "playwright",
    "git",
)
_QUESTION_PREFIXES = (
    "什么是",
    "为什么",
    "怎么理解",
    "如何理解",
    "what is",
    "why ",
    "how does",
    "could you",
    "can you",
    "请问",
    "是否",
    "能否",
)
_CHANGE_MARKERS = (
    "改为",
    "更换",
    "现在使用",
    "之前",
    "原来",
    "instead",
    "changed to",
    "previously",
)
_PLAN_MARKERS = ("计划", "下一步", "明天", "后续", "准备", "plan", "next step", "will ")
_DECISION_MARKERS = ("决定", "确认", "采用", "选择", "不再", "decided", "use ", "chosen")
_MECHANISM_MARKERS = ("机理", "机制", "假设", "推测", "mechanism", "hypothesis")
_SYNTHESIS_MARKERS = ("合成", "反应条件", "前驱体", "synthesis", "reaction condition", "precursor")
_PAPER_MARKERS = ("doi", "论文", "文献", "paper", "article")
_QUANTITY_RE = re.compile(
    r"(?:\b\d+(?:\.\d+)?\s*(?:mM|uM|µM|nM|M|mg|g|mL|uL|µL|nm|min|h|%|°C)\b|pH\s*\d)",
    flags=re.IGNORECASE,
)
_WINDOWS_PATH_RE = re.compile(r"\b[A-Za-z]:[\\/][^\s]+")
_URL_RE = re.compile(r"https?://\S+", flags=re.IGNORECASE)


def capture_memory(
    service: ResearchMemoryService,
    *,
    content: str,
    project: str | None = None,
    source_context: str = "current conversation",
    importance: str = "auto",
    user_confirmed: bool = False,
) -> dict[str, Any]:
    """Classify and store/queue durable information without exposing the core schema to the agent."""
    normalized = " ".join(content.strip().split())
    if not normalized:
        raise ValueError("content must not be empty")
    if importance not in {"auto", "low", "normal", "high"}:
        raise ValueError("importance must be auto, low, normal, or high")

    if _should_ignore(
        normalized,
        importance=importance,
        min_chars=service.config.memory.capture_min_chars,
    ):
        return {
            "action": "ignored",
            "reason": "content does not look like a durable reusable memory",
        }

    resolved_project = _resolve_project(service, normalized, project)
    tier = _classify_tier(normalized)
    memory_type = _classify_memory_type(normalized, tier=tier)
    fingerprint = _fingerprint(resolved_project, normalized)
    duplicate = _find_duplicate(service, normalized, resolved_project, fingerprint)
    if duplicate is not None:
        return {
            "action": "duplicate",
            "memory_tier": duplicate.memory_tier.value,
            "memory_id": duplicate.memory_id,
            "project": duplicate.project,
            "title": duplicate.title,
        }
    pending_duplicate = _find_pending_duplicate(
        service,
        normalized,
        resolved_project,
        fingerprint,
    )
    if pending_duplicate is not None:
        return {
            "action": "duplicate",
            "memory_tier": pending_duplicate.suggested_memory.memory_tier.value,
            "proposal_id": pending_duplicate.proposal_id,
            "project": pending_duplicate.suggested_memory.project,
            "title": pending_duplicate.suggested_memory.title,
        }

    memory = _build_memory(
        service,
        content=normalized,
        project=resolved_project,
        source_context=source_context,
        tier=tier,
        memory_type=memory_type,
        fingerprint=fingerprint,
        importance=importance,
        user_confirmed=user_confirmed,
    )

    overlaps = service.check_overlap(
        query=normalized,
        project=resolved_project,
        limit=service.config.memory.overlap_limit,
    )
    potential_conflict = bool(overlaps and _contains_any(normalized, _CHANGE_MARKERS))

    if tier == MemoryTier.ambient:
        if not service.config.memory.ambient_auto_save:
            proposal = service.propose_save(
                reason="capture_memory classified this as ambient; ambient_auto_save is disabled",
                suggested_memory=memory.model_dump(mode="json"),
                check_overlap=True,
            )
            return _queued_payload(proposal, tier=tier, potential_conflict=potential_conflict)

        saved = service.backend.save(memory)
        service.append_audit_event(
            "memory.capture.ambient_saved",
            memory_id=saved.memory_id,
            metadata={
                "source_context": source_context,
                "importance": importance,
                "potential_conflict": potential_conflict,
            },
        )
        return {
            "action": "saved",
            "memory_tier": tier.value,
            "memory_id": saved.memory_id,
            "project": saved.project,
            "title": saved.title,
            "verification": "unverified",
            "potential_conflict": potential_conflict,
            "overlap_count": len(overlaps),
        }

    proposal = service.propose_save(
        reason="capture_memory classified this as trusted research memory",
        suggested_memory=memory.model_dump(mode="json"),
        check_overlap=True,
    )
    should_queue = service.config.memory.trusted_capture_requires_review or (
        service.config.memory.require_user_confirmation and not user_confirmed
    )
    if should_queue and not user_confirmed:
        return _queued_payload(proposal, tier=tier, potential_conflict=potential_conflict)

    saved = service.save_research_memory(
        user_confirmed=user_confirmed,
        proposal_id=proposal.proposal_id,
        confirmation=(
            {
                "source": "capture_memory",
                "text": content,
                "confirmed_by": "user",
            }
            if user_confirmed
            else None
        ),
    )
    return {
        "action": "saved",
        "memory_tier": tier.value,
        "memory_id": saved.memory_id,
        "proposal_id": proposal.proposal_id,
        "project": saved.project,
        "title": saved.title,
        "verification": "unverified",
        "potential_conflict": potential_conflict,
        "overlap_count": len(proposal.overlap_candidates),
    }


def _build_memory(
    service: ResearchMemoryService,
    *,
    content: str,
    project: str,
    source_context: str,
    tier: MemoryTier,
    memory_type: MemoryType,
    fingerprint: str,
    importance: str,
    user_confirmed: bool,
) -> ResearchMemory:
    summary = content[: service.config.memory.max_summary_chars]
    evidence, source_ref = _source_records(content, source_context)
    claim = Claim(
        claim=summary,
        confidence=Confidence.medium,
        verification_status=VerificationStatus.unverified,
        evidence_ids=[evidence.evidence_id] if evidence is not None else [],
    )
    metadata: dict[str, Any] = {
        "capture_origin": "agent_surface",
        "capture_fingerprint": fingerprint,
        "capture_importance": importance,
        "source_context": source_context,
    }
    if memory_type in {MemoryType.experiment_plan, MemoryType.workflow_plan}:
        metadata["plan_status"] = "accepted" if user_confirmed else _default_plan_status(content, tier)
    if memory_type == MemoryType.workflow_plan:
        metadata["plan_type"] = _workflow_plan_type(content)

    return ResearchMemory(
        project=project,
        topic=_topic(content, project),
        memory_type=memory_type,
        memory_tier=tier,
        title=_title(content),
        summary=summary,
        claims=[claim],
        evidence=[evidence] if evidence is not None else [],
        source_refs=[source_ref] if source_ref is not None else [],
        entities=_extract_entities(content),
        tags=_extract_tags(content, tier=tier),
        metadata=metadata,
    )


def _source_records(content: str, source_context: str) -> tuple[Evidence | None, SourceRef | None]:
    context = source_context.strip()
    if not context:
        return None, None
    url_match = _URL_RE.search(context)
    path_match = _WINDOWS_PATH_RE.search(context)
    lowered = context.lower()
    if url_match:
        url = url_match.group(0)
        return (
            Evidence(type="url", quote=content, url=url),
            SourceRef(source_type="url", url=url, excerpt=content),
        )
    if path_match:
        path = path_match.group(0)
        return (
            Evidence(type="file", quote=content, file_path=path),
            SourceRef(source_type="file", path=path, excerpt=content),
        )
    if "conversation" in lowered or "chat" in lowered or "session" in lowered or "对话" in context:
        return (
            Evidence(type="conversation_assertion", quote=content, metadata={"context": context}),
            SourceRef(source_type="conversation", source_id=context, excerpt=content),
        )
    return (
        Evidence(type="capture_context", quote=content, metadata={"context": context}),
        SourceRef(source_type="capture_context", source_id=context, excerpt=content),
    )


def _resolve_project(service: ResearchMemoryService, content: str, project: str | None) -> str:
    if project and project.strip():
        return project.strip()
    candidates = service.search_research_memory(query=content, limit=3)
    if candidates:
        projects = [item.memory.project for item in candidates]
        if len(set(projects)) == 1 or projects.count(projects[0]) >= 2:
            return projects[0]
    return "default"


def _find_duplicate(
    service: ResearchMemoryService,
    content: str,
    project: str,
    fingerprint: str,
) -> ResearchMemory | None:
    normalized = _normalized_key(content)
    for result in service.search_research_memory(query=content, project=project, limit=5):
        memory = result.memory
        if memory.metadata.get("capture_fingerprint") == fingerprint:
            return memory
        if _normalized_key(memory.summary) == normalized:
            return memory
    return None


def _find_pending_duplicate(
    service: ResearchMemoryService,
    content: str,
    project: str,
    fingerprint: str,
) -> Any | None:
    normalized = _normalized_key(content)
    for proposal in service.list_memory_proposals(status="pending", limit=200):
        memory = proposal.suggested_memory
        if memory.project != project:
            continue
        if memory.metadata.get("capture_fingerprint") == fingerprint:
            return proposal
        if _normalized_key(memory.summary) == normalized:
            return proposal
    return None


def _classify_tier(content: str) -> MemoryTier:
    lowered = content.lower()
    research_hits = sum(term in lowered for term in _RESEARCH_TERMS)
    if _QUANTITY_RE.search(content) and research_hits:
        return MemoryTier.trusted
    if _contains_any(lowered, _STRONG_RESEARCH_TERMS):
        return MemoryTier.trusted
    if research_hits and (
        _contains_any(lowered, _PLAN_MARKERS)
        or _contains_any(lowered, _DECISION_MARKERS)
        or _contains_any(lowered, _MECHANISM_MARKERS)
        or _contains_any(lowered, _PAPER_MARKERS)
        or _contains_any(lowered, _SYNTHESIS_MARKERS)
    ):
        return MemoryTier.trusted
    if research_hits >= 2 and not _contains_any(lowered, _AMBIENT_TERMS):
        return MemoryTier.trusted
    return MemoryTier.ambient


def _classify_memory_type(content: str, *, tier: MemoryTier) -> MemoryType:
    lowered = content.lower()
    if tier == MemoryTier.ambient:
        return MemoryType.workflow_plan
    if _contains_any(lowered, _PAPER_MARKERS):
        return MemoryType.paper_note
    if _contains_any(lowered, _SYNTHESIS_MARKERS):
        return MemoryType.synthesis_route
    if _contains_any(lowered, _MECHANISM_MARKERS):
        return MemoryType.mechanism_hypothesis
    if _contains_any(lowered, _PLAN_MARKERS):
        return MemoryType.experiment_plan
    if _contains_any(lowered, _DECISION_MARKERS):
        return MemoryType.research_decision
    return MemoryType.material_system


def _should_ignore(content: str, *, importance: str, min_chars: int) -> bool:
    lowered = content.lower()
    if len(content) < 4:
        return True
    if content.endswith(("?", "？")) and _contains_any(lowered, _QUESTION_PREFIXES):
        return True
    durable = (
        _contains_any(lowered, _RESEARCH_TERMS)
        or _contains_any(lowered, _STRONG_RESEARCH_TERMS)
        or _contains_any(lowered, _AMBIENT_TERMS)
        or _WINDOWS_PATH_RE.search(content) is not None
        or _QUANTITY_RE.search(content) is not None
        or _contains_any(lowered, _DECISION_MARKERS)
    )
    if importance == "high":
        return False
    if importance == "low" and not durable:
        return True
    if importance == "auto" and not durable:
        return True
    return len(content) < max(1, min_chars) and not durable


def _default_plan_status(content: str, tier: MemoryTier) -> str:
    lowered = content.lower()
    if tier == MemoryTier.ambient and not _contains_any(lowered, _PLAN_MARKERS):
        return "active"
    return "draft"


def _workflow_plan_type(content: str) -> str:
    lowered = content.lower()
    if "mcp" in lowered:
        return "mcp_setup"
    if "deploy" in lowered or "部署" in content:
        return "deployment_workflow"
    if "agent" in lowered or "memory" in lowered or "记忆" in content:
        return "agent_memory_policy"
    if "写作" in content or "writing" in lowered:
        return "writing_workflow"
    if "分支" in content or "branch" in lowered or "治理" in content:
        return "project_governance"
    return "research_workflow"


def _extract_tags(content: str, *, tier: MemoryTier) -> list[str]:
    lowered = content.lower()
    tags = [tier.value, "agent-captured"]
    for term in ("dmso", "hepes", "fe3+", "hno3", "uv-vis", "xps", "lc-ms", "mcp", "agent"):
        if term in lowered:
            tags.append(term)
    if _WINDOWS_PATH_RE.search(content):
        tags.append("project-path")
    return sorted(set(tags))


def _extract_entities(content: str) -> list[Entity]:
    lowered = content.lower()
    entities: list[Entity] = []
    mapping = {
        "dmso": "solvent",
        "hepes": "buffer",
        "fe3+": "ion",
        "hno3": "reagent",
        "uv-vis": "method",
        "xps": "method",
        "lc-ms": "method",
    }
    for name, entity_type in mapping.items():
        if name in lowered:
            entities.append(Entity(name=name.upper() if name != "fe3+" else "Fe3+", entity_type=entity_type))
    return entities


def _title(content: str) -> str:
    first = re.split(r"[。.!?！？\n]", content, maxsplit=1)[0].strip()
    return (first or content)[:96]


def _topic(content: str, project: str) -> str:
    first = _title(content)
    if len(first) >= 8:
        return first[:80]
    return project


def _fingerprint(project: str, content: str) -> str:
    payload = f"{project}\n{_normalized_key(content)}".encode()
    return hashlib.sha256(payload).hexdigest()


def _normalized_key(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


def _contains_any(value: str, terms: tuple[str, ...]) -> bool:
    return any(term.lower() in value.lower() for term in terms)


def _queued_payload(proposal: Any, *, tier: MemoryTier, potential_conflict: bool) -> dict[str, Any]:
    return {
        "action": "queued",
        "memory_tier": tier.value,
        "proposal_id": proposal.proposal_id,
        "project": proposal.suggested_memory.project,
        "title": proposal.suggested_memory.title,
        "verification": "unverified",
        "potential_conflict": potential_conflict,
        "overlap_count": len(proposal.overlap_candidates),
        "review_required": True,
    }
