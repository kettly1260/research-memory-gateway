from __future__ import annotations

from .config import MemoryConfig
from .models import ResearchMemory, VerificationStatus
from .taxonomy import validate_plan_metadata


class MemoryWritePolicy:
    def __init__(self, config: MemoryConfig) -> None:
        self.config = config

    def validate(self, memory: ResearchMemory) -> None:
        self._validate_summary(memory)
        validate_plan_metadata(memory.memory_type.value, memory.metadata)
        self._apply_evidence_policy(memory)

    def _validate_summary(self, memory: ResearchMemory) -> None:
        if len(memory.summary) > self.config.max_summary_chars:
            raise ValueError(f"summary exceeds max_summary_chars={self.config.max_summary_chars}")

    def _apply_evidence_policy(self, memory: ResearchMemory) -> None:
        if not self.config.require_evidence_for_claims:
            return
        evidence_ids = {item.evidence_id for item in memory.evidence}
        for claim in memory.claims:
            if not claim.evidence_ids:
                claim.verification_status = VerificationStatus.unverified
                continue
            if any(evidence_id not in evidence_ids for evidence_id in claim.evidence_ids):
                raise ValueError("claim references evidence_id that does not exist")
            if (
                claim.verification_status == VerificationStatus.unverified
                and "verification_status" not in claim.model_fields_set
            ):
                claim.verification_status = VerificationStatus.evidence_backed
