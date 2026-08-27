from __future__ import annotations

import re
from collections import Counter
from typing import Any

from ..models import Claim, ResearchMemory, SearchResult, VerificationStatus
from ..service import ResearchMemoryService

_VERIFICATION_PRIORITY = (
    VerificationStatus.conflicting,
    VerificationStatus.retracted,
    VerificationStatus.superseded,
    VerificationStatus.unverified,
    VerificationStatus.inferred,
    VerificationStatus.evidence_backed,
)

_HISTORY_FILLERS = (
    "之前",
    "上次",
    "以前",
    "先前",
    "原来",
    "还记得",
    "你还记得",
    "我们之前",
    "我们上次",
    "当时",
    "来着",
    "previously",
    "last time",
    "earlier",
    "before",
    "do you remember",
    "we previously",
)
_QUESTION_FILLERS = (
    "怎么配的",
    "怎么配",
    "如何配的",
    "如何配",
    "怎么做的",
    "怎么做",
    "是什么",
    "是多少",
    "吗",
    "呢",
    "how did we",
    "how was",
    "what was",
    "what did we",
)
_PREPARATION_MARKERS = ("怎么配", "如何配", "配制", "配液", "prepared", "preparation")
_PREPARATION_EXPANSION = ("配制", "配液", "储备液", "母液", "工作液", "浓度", "stock solution")
_CHEMISTRY_ALIASES: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("硝酸",), ("HNO3",)),
    (("盐酸",), ("HCl",)),
    (("硫酸",), ("H2SO4",)),
    (("氢氧化钠",), ("NaOH",)),
    (("三价铁", "Fe(III)", "Fe3+"), ("Fe3", "Fe3+")),
    (("二价铁", "Fe(II)", "Fe2+"), ("Fe2", "Fe2+")),
)
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9+()._-]*|[+-]?\d+(?:\.\d+)?|[\u4e00-\u9fff]{2,}")


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
    rewritten_query, query_terms = rewrite_recall_query(normalized_query)
    results = service.search_research_memory(
        query=rewritten_query,
        project=project_hint,
        include_archived=False,
        include_deleted=False,
        limit=safe_limit,
    )

    inferred_project = project_hint or _infer_project(results)
    return {
        "query": normalized_query,
        "rewritten_query": rewritten_query,
        "project": inferred_project,
        "context_mode": context_mode,
        "result_count": len(results),
        "results": [
            _serialize_result(item, context_mode=context_mode, query_terms=query_terms)
            for item in results
        ],
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


def _serialize_result(
    result: SearchResult,
    *,
    context_mode: str,
    query_terms: list[str] | None = None,
) -> dict[str, Any]:
    memory = result.memory
    matched_claims = _match_claims(memory.claims, query_terms or [])
    best_claim = matched_claims[0] if matched_claims else None
    item: dict[str, Any] = {
        "memory_id": memory.memory_id,
        "title": memory.title,
        "content": (best_claim["claim"] if best_claim else memory.summary)[:800],
        "summary": memory.summary[:800],
        "matched_claims": matched_claims,
        "verification": (
            best_claim["verification"] if best_claim else verification_summary(memory)
        ),
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


def rewrite_recall_query(query: str) -> tuple[str, list[str]]:
    original = " ".join(query.strip().split())
    cleaned = original
    lowered_original = original.lower()
    for filler in (*_HISTORY_FILLERS, *_QUESTION_FILLERS):
        cleaned = re.sub(re.escape(filler), " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"[？?！!，,。；;：:]", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    expansions: list[str] = []
    if any(marker.lower() in lowered_original for marker in _PREPARATION_MARKERS):
        expansions.extend(_PREPARATION_EXPANSION)
    for aliases, additions in _CHEMISTRY_ALIASES:
        if any(alias.lower() in lowered_original for alias in aliases):
            expansions.extend(additions)
    if re.search(r"(?<![A-Za-z0-9])Fe(?![A-Za-z0-9])", original, flags=re.IGNORECASE):
        expansions.extend(("Fe2", "Fe3", "Fe2+", "Fe3+"))

    terms = _dedupe_terms([*_tokenize(cleaned), *expansions])
    if not terms:
        terms = _dedupe_terms(_tokenize(original))
    rewritten = " ".join(terms) or original
    return rewritten, terms


def _match_claims(claims: list[Claim], query_terms: list[str], *, limit: int = 3) -> list[dict[str, Any]]:
    scored: list[tuple[float, Claim]] = []
    for claim in claims:
        score = _claim_match_score(claim.claim, query_terms)
        if score > 0:
            scored.append((score, claim))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [
        {
            "claim_id": claim.claim_id,
            "claim": claim.claim,
            "verification": claim.verification_status.value,
            "confidence": claim.confidence.value,
        }
        for _, claim in scored[:limit]
    ]


def _claim_match_score(claim_text: str, query_terms: list[str]) -> float:
    normalized_claim = claim_text.lower()
    score = 0.0
    for term in query_terms:
        normalized_term = term.lower().strip()
        if len(normalized_term) < 2:
            continue
        if normalized_term in normalized_claim:
            score += min(len(normalized_term), 12) / 4
    if score:
        return score
    query_chinese = "".join(term for term in query_terms if re.fullmatch(r"[\u4e00-\u9fff]+", term))
    claim_chinese = "".join(re.findall(r"[\u4e00-\u9fff]+", claim_text))
    if query_chinese and claim_chinese:
        query_bigrams = {query_chinese[index : index + 2] for index in range(len(query_chinese) - 1)}
        claim_bigrams = {claim_chinese[index : index + 2] for index in range(len(claim_chinese) - 1)}
        if query_bigrams:
            return len(query_bigrams & claim_bigrams) / len(query_bigrams)
    return 0.0


def _tokenize(value: str) -> list[str]:
    return [match.group(0) for match in _TOKEN_RE.finditer(value)]


def _dedupe_terms(terms: list[str] | tuple[str, ...]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for term in terms:
        cleaned = term.strip()
        key = cleaned.lower()
        if not cleaned or key in seen:
            continue
        seen.add(key)
        result.append(cleaned)
    return result


def _infer_project(results: list[SearchResult]) -> str | None:
    if not results:
        return None
    counts = Counter(item.memory.project for item in results)
    project, count = counts.most_common(1)[0]
    if len(counts) == 1 or count >= 2:
        return project
    return None
