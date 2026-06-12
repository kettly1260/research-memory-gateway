from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any

from .backends import MemoryBackend
from .config import AppConfig
from .exporters import export_memories
from .models import (
    ExportFormat,
    MemoryStatus,
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
        include_archived: bool = False,
        include_deleted: bool = False,
        limit: int = 10,
    ) -> list[SearchResult]:
        return self.backend.search(
            query,
            project=project,
            memory_type=memory_type,
            statuses=_status_scope(include_archived=include_archived, include_deleted=include_deleted),
            limit=limit,
        )

    def check_overlap(
        self,
        *,
        query: str,
        project: str | None = None,
        memory_type: str | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        results = self.backend.search(
            query,
            project=project,
            memory_type=memory_type,
            statuses=[status.value for status in MemoryStatus],
            limit=limit,
        )
        return [self._overlap_summary(result) for result in results]

    def open_source_ref(self, source_ref: dict[str, Any], max_chars: int = 4000) -> dict[str, Any]:
        parsed = SourceRef.model_validate(source_ref)
        return self.source_resolver.resolve(parsed, max_chars=max_chars)

    def audit_unverified(
        self,
        *,
        project: str | None = None,
        include_inferred: bool = True,
        include_archived: bool = False,
        include_deleted: bool = False,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        memories = self.backend.list_all(
            statuses=_status_scope(include_archived=include_archived, include_deleted=include_deleted)
        )
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

    def export_memories(
        self,
        export_format: ExportFormat = ExportFormat.both,
        *,
        include_archived: bool = False,
        include_deleted: bool = False,
    ) -> dict[str, Any]:
        return export_memories(
            self.backend.list_all(
                statuses=_status_scope(include_archived=include_archived, include_deleted=include_deleted)
            ),
            self.config,
            export_format,
        )

    def retrieval_health(self) -> dict[str, Any]:
        return self.backend.retrieval_health()

    def health(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "backend": self.backend.health(),
            "retrieval": self.backend.retrieval_health(),
            "memory_policy": {
                "require_user_confirmation": self.config.memory.require_user_confirmation,
                "require_evidence_for_claims": self.config.memory.require_evidence_for_claims,
            },
        }

    def audit_database_integrity(
        self, *, repair_fts: bool = False, repair_orphan_embeddings: bool = False
    ) -> dict[str, Any]:
        audit = getattr(self.backend, "audit_integrity", None)
        if audit is None:
            return {"supported": False, "backend": self.backend.__class__.__name__}
        return audit(repair_fts=repair_fts, repair_orphans=repair_orphan_embeddings)

    def get_research_memory(self, memory_id: str) -> ResearchMemory:
        memory = self.backend.get(memory_id)
        if memory is None:
            raise KeyError(f"Unknown memory_id: {memory_id}")
        return memory

    def update_research_memory(self, memory_id: str, updates: dict[str, Any], user_confirmed: bool) -> ResearchMemory:
        if self.config.memory.require_user_confirmation and not user_confirmed:
            raise PermissionError("Updating research memory requires user_confirmed=true")
        current = self.get_research_memory(memory_id)
        data = current.model_dump(mode="json")
        updates = dict(updates)
        updates.pop("memory_id", None)
        data.update(updates)
        data["memory_id"] = memory_id
        data["updated_at"] = _utc_now()
        updated = ResearchMemory.model_validate(data)
        self._apply_evidence_policy(updated)
        return self.backend.save(updated)

    def delete_research_memory(self, memory_id: str, user_confirmed: bool) -> dict[str, Any]:
        if self.config.memory.require_user_confirmation and not user_confirmed:
            raise PermissionError("Deleting research memory requires user_confirmed=true")
        return {"memory_id": memory_id, "deleted": self.backend.delete(memory_id)}

    def archive_memory(self, memory_id: str, reason: str = "", user_confirmed: bool = True) -> ResearchMemory:
        return self._set_memory_lifecycle(memory_id, MemoryStatus.archived, reason, user_confirmed)

    def restore_memory(self, memory_id: str, reason: str = "", user_confirmed: bool = True) -> ResearchMemory:
        return self._set_memory_lifecycle(memory_id, MemoryStatus.active, reason, user_confirmed)

    def soft_delete_memory(self, memory_id: str, reason: str = "", user_confirmed: bool = True) -> ResearchMemory:
        return self._set_memory_lifecycle(memory_id, MemoryStatus.deleted, reason, user_confirmed)

    def hard_delete_memory(
        self,
        memory_id: str,
        *,
        confirm_memory_id: str,
        current_password_valid: bool = True,
        reason: str = "",
        user_confirmed: bool = True,
    ) -> dict[str, Any]:
        if self.config.memory.require_user_confirmation and not user_confirmed:
            raise PermissionError("Hard deleting research memory requires user_confirmed=true")
        if confirm_memory_id != memory_id:
            raise PermissionError("Hard delete requires entering the full memory_id")
        if not current_password_valid:
            raise PermissionError("Hard delete requires current password")
        memory = self.get_research_memory(memory_id)
        if memory.memory_status != MemoryStatus.deleted:
            raise PermissionError("Hard delete is only allowed for deleted memories")
        deleted = self.backend.delete(memory_id)
        self.append_audit_event(
            "memory.hard_deleted",
            memory_id=memory_id,
            metadata={"reason": reason, "previous_status": memory.memory_status.value},
        )
        return {"memory_id": memory_id, "deleted": deleted}

    def mark_memory_status(
        self,
        memory_id: str,
        status: str,
        reason: str,
        user_confirmed: bool,
    ) -> ResearchMemory:
        if self.config.memory.require_user_confirmation and not user_confirmed:
            raise PermissionError("Marking memory status requires user_confirmed=true")
        parsed_status = VerificationStatus(status)
        if parsed_status not in {
            VerificationStatus.superseded,
            VerificationStatus.retracted,
            VerificationStatus.conflicting,
        }:
            raise ValueError("status must be superseded, retracted, or conflicting")
        memory = self.get_research_memory(memory_id)
        for claim in memory.claims:
            claim.verification_status = parsed_status
        memory.tags = sorted(set(memory.tags + [parsed_status.value]))
        memory.source_refs.append(
            SourceRef(
                source_type="memory_status",
                excerpt=reason,
                metadata={"status": parsed_status.value, "reason": reason},
            )
        )
        memory.updated_at = _utc_now()
        return self.backend.save(memory)

    def append_audit_event(self, event_type: str, *, memory_id: str | None = None, metadata: dict[str, Any] | None = None) -> None:
        append = getattr(self.backend, "append_audit_event", None)
        if append is not None:
            append(event_type, memory_id=memory_id, metadata=metadata or {})

    def list_audit_events(self, limit: int = 100) -> list[dict[str, Any]]:
        list_events = getattr(self.backend, "list_audit_events", None)
        if list_events is None:
            return []
        return list_events(limit=limit)

    def merge_research_memories(
        self,
        source_memory_ids: list[str],
        merged_memory: dict[str, Any],
        reason: str,
        user_confirmed: bool,
    ) -> ResearchMemory:
        if self.config.memory.require_user_confirmation and not user_confirmed:
            raise PermissionError("Merging research memories requires user_confirmed=true")
        sources = [self.get_research_memory(memory_id) for memory_id in source_memory_ids]
        merged_data = dict(merged_memory)
        source_refs = list(merged_data.get("source_refs", []))
        evidence = list(merged_data.get("evidence", []))
        for source in sources:
            source_refs.extend(item.model_dump(mode="json") for item in source.source_refs)
            evidence.extend(item.model_dump(mode="json") for item in source.evidence)
        source_refs.append(
            SourceRef(
                source_type="memory_merge",
                excerpt=reason,
                metadata={"source_memory_ids": source_memory_ids, "reason": reason},
            ).model_dump(mode="json")
        )
        merged_data["source_refs"] = _dedupe_dicts(source_refs)
        merged_data["evidence"] = _dedupe_dicts(evidence)
        merged_data["updated_at"] = _utc_now()
        merged = ResearchMemory.model_validate(merged_data)
        self._apply_evidence_policy(merged)
        saved = self.backend.save(merged)
        for source in sources:
            self.mark_memory_status(
                source.memory_id,
                VerificationStatus.superseded.value,
                f"Merged into {saved.memory_id}: {reason}",
                user_confirmed=True,
            )
        return saved

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
            if claim.verification_status == VerificationStatus.unverified and "verification_status" not in claim.model_fields_set:
                claim.verification_status = VerificationStatus.evidence_backed

    def _overlap_summary(self, result: SearchResult) -> dict[str, Any]:
        memory = result.memory
        return {
            "memory_id": memory.memory_id,
            "title": memory.title,
            "project": memory.project,
            "topic": memory.topic,
            "memory_type": memory.memory_type.value,
            "memory_status": memory.memory_status.value,
            "score": result.score,
            "match_reason": result.match_reason,
            "summary_preview": memory.summary[:500],
            "verification_statuses": sorted(
                {claim.verification_status.value for claim in memory.claims}
            ),
            "source_ref_count": len(memory.source_refs),
        }

    def _set_memory_lifecycle(
        self,
        memory_id: str,
        status: MemoryStatus,
        reason: str,
        user_confirmed: bool,
    ) -> ResearchMemory:
        if self.config.memory.require_user_confirmation and not user_confirmed:
            raise PermissionError("Changing memory lifecycle requires user_confirmed=true")
        memory = self.get_research_memory(memory_id)
        memory.memory_status = status
        memory.status_changed_at = _utc_now()
        memory.status_change_reason = reason or None
        memory.updated_at = _utc_now()
        saved = self.backend.save(memory)
        self.append_audit_event(
            f"memory.{status.value}",
            memory_id=memory_id,
            metadata={"reason": reason, "memory_status": status.value},
        )
        return saved


def serialize_results(items: Iterable[Any]) -> list[Any]:
    serialized = []
    for item in items:
        if hasattr(item, "model_dump"):
            serialized.append(item.model_dump(mode="json"))
        else:
            serialized.append(item)
    return serialized


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _status_scope(*, include_archived: bool, include_deleted: bool) -> list[str]:
    statuses = [MemoryStatus.active.value]
    if include_archived:
        statuses.append(MemoryStatus.archived.value)
    if include_deleted:
        statuses.append(MemoryStatus.deleted.value)
    return statuses


def _dedupe_dicts(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for item in items:
        key = item.get("evidence_id") or item.get("content_hash") or item.get("excerpt") or repr(item)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped
