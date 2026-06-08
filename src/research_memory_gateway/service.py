from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .backends import MemoryBackend
from .config import AppConfig
from .exporters import export_memories
from .models import (
    ExportFormat,
    ResearchMemory,
    SaveProposal,
    SearchResult,
    SourceRef,
    VerificationStatus,
)
from .source_refs import SourceResolver


class ResearchMemoryService:
    def __init__(self, config: AppConfig, backend: MemoryBackend) -> None:
        self.config = config
        self.backend = backend
        self.source_resolver = SourceResolver(config)
        self.proposals: dict[str, SaveProposal] = {}

    def propose_save(
        self,
        *,
        reason: str,
        suggested_memory: dict[str, Any],
        check_overlap: bool = True,
    ) -> SaveProposal:
        memory = ResearchMemory.model_validate(suggested_memory)
        self._apply_evidence_policy(memory)
        overlaps = []
        if check_overlap:
            overlaps = self.check_overlap(
                query=f"{memory.title} {memory.topic} {memory.summary}",
                project=memory.project,
                limit=self.config.memory.overlap_limit,
            )
        proposal = SaveProposal(
            reason=reason,
            suggested_memory=memory,
            overlap_candidates=overlaps,
            requires_confirmation=self.config.memory.require_user_confirmation,
        )
        self.proposals[proposal.proposal_id] = proposal
        return proposal

    def save_research_memory(
        self,
        *,
        user_confirmed: bool,
        proposal_id: str | None = None,
        memory: dict[str, Any] | None = None,
    ) -> ResearchMemory:
        if self.config.memory.require_user_confirmation and not user_confirmed:
            raise PermissionError("Saving long-term research memory requires user_confirmed=true")

        if proposal_id:
            proposal = self.proposals.get(proposal_id)
            if proposal is None:
                raise KeyError(f"Unknown proposal_id: {proposal_id}")
            research_memory = proposal.suggested_memory
        elif memory is not None:
            research_memory = ResearchMemory.model_validate(memory)
        else:
            raise ValueError("Provide either proposal_id or memory")

        self._apply_evidence_policy(research_memory)
        saved = self.backend.save(research_memory)
        if proposal_id:
            self.proposals.pop(proposal_id, None)
        return saved

    def search_research_memory(
        self,
        *,
        query: str,
        project: str | None = None,
        memory_type: str | None = None,
        limit: int = 10,
    ) -> list[SearchResult]:
        return self.backend.search(query, project=project, memory_type=memory_type, limit=limit)

    def check_overlap(
        self,
        *,
        query: str,
        project: str | None = None,
        memory_type: str | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        results = self.backend.search(query, project=project, memory_type=memory_type, limit=limit)
        return [self._overlap_summary(result) for result in results]

    def open_source_ref(self, source_ref: dict[str, Any], max_chars: int = 4000) -> dict[str, Any]:
        parsed = SourceRef.model_validate(source_ref)
        return self.source_resolver.resolve(parsed, max_chars=max_chars)

    def audit_unverified(
        self,
        *,
        project: str | None = None,
        include_inferred: bool = True,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        memories = self.backend.list_all()
        if project:
            memories = [memory for memory in memories if memory.project == project]

        statuses = {VerificationStatus.unverified}
        if include_inferred:
            statuses.add(VerificationStatus.inferred)

        findings: list[dict[str, Any]] = []
        for memory in memories:
            weak_claims = [
                claim
                for claim in memory.claims
                if claim.verification_status in statuses or not claim.evidence_ids
            ]
            if weak_claims:
                findings.append(
                    {
                        "memory_id": memory.memory_id,
                        "title": memory.title,
                        "project": memory.project,
                        "memory_type": memory.memory_type.value,
                        "weak_claims": [claim.model_dump(mode="json") for claim in weak_claims],
                        "source_ref_count": len(memory.source_refs),
                    }
                )
            if len(findings) >= limit:
                break
        return findings

    def export_memories(self, export_format: ExportFormat = ExportFormat.both) -> dict[str, Any]:
        return export_memories(self.backend.list_all(), self.config, export_format)

    def _apply_evidence_policy(self, memory: ResearchMemory) -> None:
        if len(memory.summary) > self.config.memory.max_summary_chars:
            raise ValueError(
                f"summary exceeds max_summary_chars={self.config.memory.max_summary_chars}"
            )
        if not self.config.memory.require_evidence_for_claims:
            return
        evidence_ids = {item.evidence_id for item in memory.evidence}
        for claim in memory.claims:
            if not claim.evidence_ids:
                claim.verification_status = VerificationStatus.unverified
                continue
            if any(evidence_id not in evidence_ids for evidence_id in claim.evidence_ids):
                raise ValueError("claim references evidence_id that does not exist")

    def _overlap_summary(self, result: SearchResult) -> dict[str, Any]:
        memory = result.memory
        return {
            "memory_id": memory.memory_id,
            "title": memory.title,
            "project": memory.project,
            "topic": memory.topic,
            "memory_type": memory.memory_type.value,
            "score": result.score,
            "match_reason": result.match_reason,
            "summary_preview": memory.summary[:500],
            "verification_statuses": sorted(
                {claim.verification_status.value for claim in memory.claims}
            ),
            "source_ref_count": len(memory.source_refs),
        }


def serialize_results(items: Iterable[Any]) -> list[Any]:
    serialized = []
    for item in items:
        if hasattr(item, "model_dump"):
            serialized.append(item.model_dump(mode="json"))
        else:
            serialized.append(item)
    return serialized
