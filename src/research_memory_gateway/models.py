from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator, model_validator


class MemoryType(str, Enum):
    literature_review = "literature_review"
    paper_note = "paper_note"
    synthesis_route = "synthesis_route"
    experiment_plan = "experiment_plan"
    mechanism_hypothesis = "mechanism_hypothesis"
    material_system = "material_system"
    presentation_outline = "presentation_outline"
    research_decision = "research_decision"
    workflow_plan = "workflow_plan"


class VerificationStatus(str, Enum):
    evidence_backed = "evidence_backed"
    inferred = "inferred"
    unverified = "unverified"
    conflicting = "conflicting"
    superseded = "superseded"
    retracted = "retracted"


class MemoryStatus(str, Enum):
    active = "active"
    archived = "archived"
    deleted = "deleted"


class ProposalStatus(str, Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"
    needs_edit = "needs_edit"
    saved = "saved"
    expired = "expired"


class Confidence(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"


class Evidence(BaseModel):
    evidence_id: str = Field(default_factory=lambda: f"ev_{uuid4().hex[:8]}")
    type: str = "note"
    quote: str = ""
    doi: str | None = None
    url: str | None = None
    paper_title: str | None = None
    file_path: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class Claim(BaseModel):
    claim: str
    confidence: Confidence = Confidence.medium
    verification_status: VerificationStatus = VerificationStatus.unverified
    evidence_ids: list[str] = Field(default_factory=list)


class SourceRef(BaseModel):
    source_type: str
    tool: str | None = None
    source_id: str | None = None
    path: str | None = None
    url: str | None = None
    doi: str | None = None
    timestamp: str | None = None
    content_hash: str | None = None
    message_range: str | None = None
    excerpt: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class Entity(BaseModel):
    name: str
    entity_type: str
    aliases: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class Relation(BaseModel):
    source: str
    relation: str
    target: str
    confidence: Confidence = Confidence.medium
    evidence_ids: list[str] = Field(default_factory=list)


class ResearchMemory(BaseModel):
    memory_id: str = Field(default_factory=lambda: f"mem_{uuid4().hex}")
    project: str = "default"
    topic: str
    memory_type: MemoryType
    memory_status: MemoryStatus = MemoryStatus.active
    status_changed_at: str | None = None
    status_change_reason: str | None = None
    title: str
    summary: str
    claims: list[Claim] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    source_refs: list[SourceRef] = Field(default_factory=list)
    entities: list[Entity] = Field(default_factory=list)
    relations: list[Relation] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @field_validator("summary")
    @classmethod
    def summary_not_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("summary must not be empty")
        return value.strip()

    @model_validator(mode="after")
    def validate_claim_evidence_links(self) -> "ResearchMemory":
        evidence_ids = {item.evidence_id for item in self.evidence}
        for claim in self.claims:
            missing = [evidence_id for evidence_id in claim.evidence_ids if evidence_id not in evidence_ids]
            if missing:
                raise ValueError(f"claim references missing evidence ids: {missing}")
            if not claim.evidence_ids and claim.verification_status == VerificationStatus.evidence_backed:
                raise ValueError("evidence_backed claims must reference at least one evidence_id")
        return self


class SaveProposal(BaseModel):
    proposal_id: str = Field(default_factory=lambda: f"prop_{uuid4().hex}")
    reason: str
    suggested_memory: ResearchMemory
    overlap_candidates: list[dict[str, Any]] = Field(default_factory=list)
    requires_confirmation: bool = True
    proposal_status: ProposalStatus = ProposalStatus.pending
    current_version: int = 1
    saved_memory_id: str | None = None
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class SearchResult(BaseModel):
    memory: ResearchMemory
    score: float = 0.0
    match_reason: str = ""


class ExportFormat(str, Enum):
    markdown = "markdown"
    json = "json"
    both = "both"


Transport = Literal["stdio", "sse"]
