import pytest

from research_memory_gateway.backends import SQLiteMemoryBackend
from research_memory_gateway.models import ResearchMemory


def test_evidence_backed_claim_requires_evidence_id() -> None:
    with pytest.raises(ValueError):
        ResearchMemory.model_validate(
            {
                "topic": "Hg2+ fluorescence probe",
                "memory_type": "literature_review",
                "title": "Hg2+ probe route",
                "summary": "Short summary",
                "claims": [
                    {
                        "claim": "Sulfur doping improves Hg2+ affinity.",
                        "verification_status": "evidence_backed",
                        "evidence_ids": [],
                    }
                ],
            }
        )


def test_memory_accepts_unverified_claim_without_evidence() -> None:
    memory = ResearchMemory.model_validate(
        {
            "topic": "Hg2+ fluorescence probe",
            "memory_type": "literature_review",
            "title": "Hg2+ probe route",
            "summary": "Short summary",
            "claims": [
                {
                    "claim": "Sulfur doping may improve Hg2+ affinity.",
                    "verification_status": "unverified",
                    "evidence_ids": [],
                }
            ],
        }
    )
    assert memory.claims[0].verification_status == "unverified"


def test_sqlite_search_handles_hyphenated_scientific_terms(tmp_path) -> None:
    backend = SQLiteMemoryBackend(str(tmp_path / "memory.db"))
    memory = ResearchMemory.model_validate(
        {
            "project": "demo",
            "topic": "Hg2+ fluorescence probe",
            "memory_type": "literature_review",
            "title": "Sulfur-doped carbon dots",
            "summary": "Sulfur-doped carbon dots may detect Hg2+.",
        }
    )
    backend.save(memory)

    results = backend.search("sulfur-doped Hg2+", project="demo")

    assert len(results) == 1
    assert results[0].memory.memory_id == memory.memory_id
