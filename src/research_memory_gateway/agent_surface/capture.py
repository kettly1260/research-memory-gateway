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
from ..secret_scan import redact_secrets
from ..service import ResearchMemoryService

_RESEARCH_TERMS = (
    "实验",
    "测试",
    "样品",
    "测量",
    "测定",
    "表征",
    "性能",
    "数据",
    "结果",
    "观察",
    "分析",
    "计算",
    "标准差",
    "斜率",
    "回归",
    "线性",
    "配制",
    "配液",
    "母液",
    "浓度",
    "溶液",
    "溶剂",
    "合成",
    "反应",
    "产率",
    "光谱",
    "峰",
    "材料",
    "论文",
    "文献",
    "机理",
    "机制",
    "experiment",
    "measurement",
    "measured",
    "characterization",
    "property",
    "performance",
    "observation",
    "result",
    "analysis",
    "calculation",
    "standard deviation",
    "slope",
    "regression",
    "spectrum",
    "sample",
    "stock solution",
    "concentration",
    "reaction",
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
_OPERATIONAL_AMBIENT_TERMS = (
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
_TRANSIENT_MARKERS = (
    "哈哈",
    "谢谢",
    "有点累",
    "明天再说",
    "回头再说",
    "maybe tomorrow",
    "thanks",
    "thank you",
)
_SPECULATION_MARKERS = (
    "我猜",
    "可能是",
    "也许是",
    "还没检查",
    "尚未检查",
    "未经验证",
    "i guess",
    "maybe it is",
    "not checked",
)
_ONE_OFF_TASK_MARKERS = (
    "翻译",
    "润色",
    "改写",
    "格式化",
    "translate",
    "polish",
    "rewrite",
    "format this",
)
_REQUEST_PREFIXES = ("把", "帮我", "请", "please", "can you", "could you")
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
_OBSERVATION_MARKERS = (
    "显示",
    "表明",
    "观察到",
    "出现",
    "检测到",
    "说明",
    "shows",
    "showed",
    "indicates",
    "indicated",
    "observed",
    "detected",
    "appeared",
)
_METHOD_RE = re.compile(
    r"\b(?:XRD|XPS|FTIR|FT-IR|Raman|SEM|TEM|AFM|TGA|DSC|DMA|NMR|LC-MS|GC-MS|"
    r"HPLC|UV-Vis|PL|EIS|CV|GCD|XAS|SAXS|WAXS|BET|ICP-MS|ICP-OES)\b",
    flags=re.IGNORECASE,
)
_SCIENTIFIC_QUANTITY_RE = re.compile(
    r"(?:"
    r"\b[+-]?\d+(?:\.\d+)?(?:\s*±\s*\d+(?:\.\d+)?)?\s*(?:"
    r"mM|uM|µM|μM|nM|mol(?:\s*L-?1|/L)?|M|"
    r"mg(?:\s*/\s*cm2|\s*cm-?2)?|µg|μg|ng|kg|g(?:\s*/\s*cm3|\s*cm-?3)?|"
    r"mL|uL|µL|μL|L|nm|µm|μm|mm|cm|Å|min|h|s|"
    r"°C|℃|K|Pa|kPa|MPa|GPa|bar|Torr|psi|"
    r"mV|V|µA|μA|mA|A|Ω(?:·?m)?|ohm(?:\s*m)?|"
    r"Hz|kHz|MHz|GHz|rpm|eV|"
    r"J(?:\s*/\s*g|\s*g-?1)?|kJ(?:\s*/\s*mol|\s*mol-?1)?|"
    r"mAh(?:\s*/\s*g|\s*g-?1)?|Ah(?:\s*/\s*g|\s*g-?1)?|Wh(?:\s*/\s*kg|\s*kg-?1)?|"
    r"S(?:\s*/\s*m|\s*m-?1|\s*cm-?1)?|"
    r"W(?:\s*/\s*\(?m[·* ]?K\)?|\s*m-?1\s*K-?1)?|"
    r"m2\s*/\s*g|m²\s*/\s*g|cm-?1|%|wt%|mol%|°"
    r")(?=$|[\s,.;:，。；：])"
    r"|pH\s*[=:]?\s*\d+(?:\.\d+)?"
    r")",
    flags=re.IGNORECASE,
)
_DIMENSIONLESS_MEASUREMENT_RE = re.compile(
    r"(?:"
    r"[\u4e00-\u9fffA-Za-z0-9+()/-]{1,24}(?:常数|系数|比|率|因子|指数|效率|容量|强度|"
    r"模量|硬度|密度|温度|电位|电压|电流|电阻|导率|尺寸|粒径|面积|厚度)"
    r"\s*(?:为|=|达到|约为|is|was)?\s*[+-]?\d+(?:\.\d+)?"
    r"|(?:constant|coefficient|ratio|factor|index|efficiency|capacity|strength|modulus|hardness|"
    r"density|conductivity|permittivity|dielectric constant|particle size|surface area)"
    r"\s*(?:is|was|=|of)?\s*[+-]?\d+(?:\.\d+)?"
    r")",
    flags=re.IGNORECASE,
)
_ASSIGNMENT_RE = re.compile(
    r"(?:\b[A-Za-z][A-Za-z0-9_+/-]{1,20}\b|[\u4e00-\u9fff]{2,20})\s*(?:为|=|达到|约为|is|was)\s*[+-]?\d+(?:\.\d+)?",
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
    source_client: str | None = None,
    conversation_id: str | None = None,
    message_id: str | None = None,
    session_id: str | None = None,
    source_timestamp: str | None = None,
    importance: str = "auto",
    user_confirmed: bool = False,
) -> dict[str, Any]:
    """Classify and store/queue durable information without exposing the core schema to the agent."""
    sanitized_input, input_secret_report = redact_secrets(
        {
            "content": content,
            "source_context": source_context,
            "source_client": source_client,
            "conversation_id": conversation_id,
            "message_id": message_id,
            "session_id": session_id,
            "source_timestamp": source_timestamp,
        },
        path="$.capture",
    )
    normalized = " ".join(str(sanitized_input["content"]).strip().split())
    source_context = str(sanitized_input["source_context"] or "").strip()
    source_client = _optional_text(sanitized_input.get("source_client"))
    conversation_id = _optional_text(sanitized_input.get("conversation_id"))
    message_id = _optional_text(sanitized_input.get("message_id"))
    session_id = _optional_text(sanitized_input.get("session_id"))
    source_timestamp = _optional_text(sanitized_input.get("source_timestamp"))
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
        source_client=source_client,
        conversation_id=conversation_id,
        message_id=message_id,
        session_id=session_id,
        source_timestamp=source_timestamp,
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

        saved = service.save_ambient_memory(
            memory,
            audit_metadata={
                "source_context": source_context,
                "importance": importance,
                "potential_conflict": potential_conflict,
            },
        )
        payload = {
            "action": "saved",
            "memory_tier": tier.value,
            "memory_id": saved.memory_id,
            "project": saved.project,
            "title": saved.title,
            "verification": "unverified",
            "potential_conflict": potential_conflict,
            "overlap_count": len(overlaps),
        }
        if input_secret_report.detected:
            payload["secret_redaction"] = input_secret_report.as_dict()
        return payload

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
    payload = {
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
    if input_secret_report.detected:
        payload["secret_redaction"] = input_secret_report.as_dict()
    return payload


def _build_memory(
    service: ResearchMemoryService,
    *,
    content: str,
    project: str,
    source_context: str,
    source_client: str | None,
    conversation_id: str | None,
    message_id: str | None,
    session_id: str | None,
    source_timestamp: str | None,
    tier: MemoryTier,
    memory_type: MemoryType,
    fingerprint: str,
    importance: str,
    user_confirmed: bool,
) -> ResearchMemory:
    summary = content[: service.config.memory.max_summary_chars]
    evidence, source_ref = _source_records(
        content,
        source_context,
        source_client=source_client,
        conversation_id=conversation_id,
        message_id=message_id,
        session_id=session_id,
        source_timestamp=source_timestamp,
    )
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
        "capture_semantic_role": _semantic_role(content, tier=tier, memory_type=memory_type),
        "source_context": source_context,
    }
    if memory_type in {MemoryType.experiment_plan, MemoryType.workflow_plan}:
        metadata["plan_status"] = "accepted" if user_confirmed else _default_plan_status(content, tier)
    if memory_type == MemoryType.workflow_plan:
        metadata["plan_type"] = _workflow_plan_type(content)
    semantic_key = _ambient_semantic_key(content, project=project) if tier == MemoryTier.ambient else None
    if semantic_key:
        metadata["semantic_key"] = semantic_key

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


def _source_records(
    content: str,
    source_context: str,
    *,
    source_client: str | None,
    conversation_id: str | None,
    message_id: str | None,
    session_id: str | None,
    source_timestamp: str | None,
) -> tuple[Evidence | None, SourceRef | None]:
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
        anchor_metadata = {
            key: value
            for key, value in {
                "context": context,
                "client": source_client,
                "conversation_id": conversation_id,
                "message_id": message_id,
                "session_id": session_id,
                "timestamp": source_timestamp,
            }.items()
            if value
        }
        source_id = conversation_id or session_id or context
        return (
            Evidence(type="conversation_assertion", quote=content, metadata=anchor_metadata),
            SourceRef(
                source_type="conversation",
                source_id=source_id,
                timestamp=source_timestamp,
                message_range=message_id,
                excerpt=content,
                metadata=anchor_metadata,
            ),
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
    if (
        _contains_any(lowered, _OPERATIONAL_AMBIENT_TERMS)
        and not _is_scientific_measurement(content)
        and not _is_scientific_observation(content)
    ):
        return MemoryTier.ambient
    if _is_scientific_measurement(content):
        return MemoryTier.trusted
    if _is_scientific_observation(content):
        return MemoryTier.trusted
    research_hits = sum(term in lowered for term in _RESEARCH_TERMS)
    if research_hits and _METHOD_RE.search(content):
        return MemoryTier.trusted
    if research_hits and (
        _contains_any(lowered, _PLAN_MARKERS)
        or _contains_any(lowered, _DECISION_MARKERS)
        or _contains_any(lowered, _MECHANISM_MARKERS)
        or _contains_any(lowered, _PAPER_MARKERS)
        or _contains_any(lowered, _SYNTHESIS_MARKERS)
        or _contains_any(lowered, _CHANGE_MARKERS)
    ):
        return MemoryTier.trusted
    if research_hits >= 2 and not _contains_any(lowered, _AMBIENT_TERMS):
        return MemoryTier.trusted
    # Fail safe: reaching this point means _should_ignore already decided that the
    # content is durable, but we could not confidently prove it is low-risk Ambient
    # state. Queue it as Trusted instead of silently auto-saving an unknown research
    # fact as Ambient. A future semantic classifier can refine this boundary without
    # weakening the default write policy.
    return MemoryTier.trusted


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
    if importance == "auto" and _contains_any(lowered, _TRANSIENT_MARKERS):
        return True
    if importance == "auto" and _contains_any(lowered, _SPECULATION_MARKERS):
        return True
    if (
        importance == "auto"
        and _contains_any(lowered, _ONE_OFF_TASK_MARKERS)
        and any(lowered.startswith(prefix.lower()) for prefix in _REQUEST_PREFIXES)
    ):
        return True
    durable = (
        _contains_any(lowered, _RESEARCH_TERMS)
        or _contains_any(lowered, _AMBIENT_TERMS)
        or _WINDOWS_PATH_RE.search(content) is not None
        or _is_scientific_measurement(content)
        or _is_scientific_observation(content)
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
    for term in ("mcp", "agent", "git", "embedding", "rerank"):
        if term in lowered:
            tags.append(term)
    if _is_scientific_measurement(content):
        tags.append("measurement")
    if _is_scientific_observation(content):
        tags.append("observation")
    method_match = _METHOD_RE.search(content)
    if method_match:
        tags.append(method_match.group(0).lower())
    if _WINDOWS_PATH_RE.search(content):
        tags.append("project-path")
    return sorted(set(tags))


def _extract_entities(content: str) -> list[Entity]:
    entities: list[Entity] = []
    for match in _METHOD_RE.finditer(content):
        name = match.group(0)
        if not any(existing.name.lower() == name.lower() for existing in entities):
            entities.append(Entity(name=name, entity_type="characterization_method"))
    for match in _SCIENTIFIC_QUANTITY_RE.finditer(content):
        quantity = match.group(0).strip()
        if quantity and not any(existing.name == quantity for existing in entities):
            entities.append(Entity(name=quantity, entity_type="measurement_value"))
    return entities


def _is_scientific_measurement(content: str) -> bool:
    lowered = content.lower()
    if _is_ambient_context(lowered) and not _contains_any(lowered, _RESEARCH_TERMS):
        return False
    if _SCIENTIFIC_QUANTITY_RE.search(content):
        return True
    if _DIMENSIONLESS_MEASUREMENT_RE.search(content):
        return True
    return bool(_METHOD_RE.search(content) and _ASSIGNMENT_RE.search(content))


def _is_scientific_observation(content: str) -> bool:
    lowered = content.lower()
    if _METHOD_RE.search(content) and _contains_any(lowered, _OBSERVATION_MARKERS):
        return True
    return _contains_any(lowered, _RESEARCH_TERMS) and _contains_any(
        lowered,
        _OBSERVATION_MARKERS,
    )


def _is_research_context(content: str) -> bool:
    lowered = content.lower()
    return bool(
        _contains_any(lowered, _RESEARCH_TERMS)
        or _METHOD_RE.search(content)
        or _SCIENTIFIC_QUANTITY_RE.search(content)
        or _DIMENSIONLESS_MEASUREMENT_RE.search(content)
    )


def _is_ambient_context(lowered: str) -> bool:
    return _contains_any(lowered, _AMBIENT_TERMS)


def _ambient_semantic_key(content: str, *, project: str) -> str | None:
    lowered = content.lower()
    role: str | None = None
    if _WINDOWS_PATH_RE.search(content):
        if _contains_any(lowered, ("repository", "repo", "仓库")):
            role = "repository_path"
        elif _contains_any(lowered, ("workspace", "工作区")):
            role = "workspace_path"
        elif _contains_any(lowered, ("tool", "executable", "工具", "程序")):
            role = "tool_path"
        elif _contains_any(lowered, ("path", "目录", "路径")):
            role = "project_path"
    elif _contains_any(lowered, ("branch", "分支")):
        role = "current_branch"
    elif _contains_any(lowered, ("model", "模型")) and _contains_any(
        lowered,
        ("use", "using", "selected", "current", "采用", "使用", "当前"),
    ):
        role = "selected_model"
    elif _contains_any(lowered, ("config", "configuration", "配置")) and _contains_any(
        lowered,
        ("use", "using", "active", "current", "采用", "使用", "当前"),
    ):
        role = "active_config"
    elif _contains_any(lowered, ("workflow", "工作流", "流程")) and _contains_any(
        lowered,
        ("active", "current", "use", "采用", "当前", "执行中"),
    ):
        role = "current_workflow"
    elif _contains_any(lowered, ("project state", "project status", "项目状态", "当前进度")):
        role = "project_state"
    elif _contains_any(lowered, ("next step", "下一步")):
        role = "next_action"
    if role is None:
        return None
    subject = _ambient_state_subject(lowered)
    return f"{project}.{subject}.{role}" if subject else f"{project}.{role}"


def _semantic_role(content: str, *, tier: MemoryTier, memory_type: MemoryType) -> str:
    lowered = content.lower()
    if tier == MemoryTier.ambient:
        if _WINDOWS_PATH_RE.search(content):
            return "project_path"
        if _contains_any(lowered, ("branch", "分支")):
            return "project_state"
        if _contains_any(lowered, ("config", "configuration", "配置", "model", "模型")):
            return "configuration"
        if _contains_any(lowered, ("workflow", "工作流", "流程")):
            return "workflow"
        return "ambient_context"
    if _is_scientific_measurement(content):
        return "measurement"
    if _is_scientific_observation(content):
        return "observation"
    if memory_type == MemoryType.experiment_plan:
        return "experiment_plan"
    if memory_type == MemoryType.research_decision:
        return "research_decision"
    if memory_type == MemoryType.synthesis_route:
        return "synthesis"
    if memory_type == MemoryType.mechanism_hypothesis:
        return "mechanism"
    if memory_type == MemoryType.paper_note:
        return "literature_conclusion"
    return "research_fact"


def _ambient_state_subject(lowered: str) -> str | None:
    subjects = (
        "embedding",
        "rerank",
        "origin",
        "zotero",
        "paper-fulltext",
        "memory-gateway",
        "research-memory-gateway",
        "codex",
        "chatgpt",
        "kilo",
        "cherry",
        "mcp",
    )
    return next((subject for subject in subjects if subject in lowered), None)


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


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
