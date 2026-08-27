from __future__ import annotations

from typing import Any

from ..models import VerificationStatus
from ..service import ResearchMemoryService
from .recall import verification_summary


def verify_memory(
    service: ResearchMemoryService,
    *,
    memory_id: str,
    claim_id: str | None = None,
) -> dict[str, Any]:
    """Expand one recalled memory into claims, evidence, and provenance anchors."""
    memory = service.get_research_memory(memory_id)
    evidence_by_id = {item.evidence_id: item for item in memory.evidence}
    claims = memory.claims
    if claim_id:
        claims = [claim for claim in claims if claim.claim_id == claim_id]
        if not claims:
            raise KeyError(f"Unknown claim_id for {memory_id}: {claim_id}")

    claim_payloads = []
    for claim in claims:
        claim_payloads.append(
            {
                "claim_id": claim.claim_id,
                "claim": claim.claim,
                "confidence": claim.confidence.value,
                "verification_status": claim.verification_status.value,
                "evidence": [
                    evidence_by_id[evidence_id].model_dump(mode="json")
                    for evidence_id in claim.evidence_ids
                    if evidence_id in evidence_by_id
                ],
            }
        )

    conflicting_claims = [
        item["claim_id"]
        for item in claim_payloads
        if item["verification_status"] == VerificationStatus.conflicting.value
    ]
    superseded_claims = [
        item["claim_id"]
        for item in claim_payloads
        if item["verification_status"] == VerificationStatus.superseded.value
    ]

    return {
        "memory_id": memory.memory_id,
        "title": memory.title,
        "project": memory.project,
        "topic": memory.topic,
        "memory_tier": memory.memory_tier.value,
        "verification": verification_summary(memory),
        "claims": claim_payloads,
        "source_refs": [item.model_dump(mode="json") for item in memory.source_refs],
        "lifecycle": {
            "memory_status": memory.memory_status.value,
            "status_changed_at": memory.status_changed_at,
            "status_change_reason": memory.status_change_reason,
        },
        "conflict_information": {
            "has_conflict": bool(conflicting_claims),
            "claim_ids": conflicting_claims,
        },
        "superseded_information": {
            "has_superseded_claims": bool(superseded_claims),
            "claim_ids": superseded_claims,
        },
        "updated_at": memory.updated_at,
    }
