import pytest

from research_memory_gateway.backends import SQLiteMemoryBackend
from research_memory_gateway.config import RetrievalConfig
from research_memory_gateway.models import ResearchMemory


class FakeEmbeddingClient:
    enabled = True

    def embed(self, text: str) -> list[float] | None:
        if "photocatalysis" in text.lower():
            return [0.0, 1.0]
        return [1.0, 0.0]


class FakeRerankClient:
    enabled = True

    def rerank(self, query: str, documents: list[str], *, top_n: int) -> list[tuple[int, float]]:
        return [(index, 10.0 - index) for index in reversed(range(len(documents)))][:top_n]


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


def test_sqlite_hybrid_search_uses_vector_candidates(tmp_path) -> None:
    retrieval = RetrievalConfig(mode="hybrid", embedding={"enabled": True})
    backend = SQLiteMemoryBackend(str(tmp_path / "memory.db"), retrieval)
    backend.embedding_client = FakeEmbeddingClient()
    memory = ResearchMemory.model_validate(
        {
            "project": "demo",
            "topic": "Mercury ion probe",
            "memory_type": "material_system",
            "title": "Soft acid capture route",
            "summary": "Soft-acid binding route for mercury ions.",
        }
    )
    backend.save(memory)

    results = backend.search("Hg2+ affinity", project="demo")

    assert len(results) == 1
    assert results[0].memory.memory_id == memory.memory_id
    assert results[0].match_reason == "vector"


def test_sqlite_hybrid_search_can_rerank_candidates(tmp_path) -> None:
    retrieval = RetrievalConfig(
        mode="hybrid",
        embedding={"enabled": True},
        rerank={"enabled": True},
        rerank_candidate_limit=2,
    )
    backend = SQLiteMemoryBackend(str(tmp_path / "memory.db"), retrieval)
    backend.embedding_client = FakeEmbeddingClient()
    backend.rerank_client = FakeRerankClient()
    first = ResearchMemory.model_validate(
        {
            "project": "demo",
            "topic": "Mercury ion probe",
            "memory_type": "material_system",
            "title": "First mercury route",
            "summary": "Soft-acid binding route for mercury ions.",
        }
    )
    second = ResearchMemory.model_validate(
        {
            "project": "demo",
            "topic": "Mercury ion probe",
            "memory_type": "material_system",
            "title": "Second mercury route",
            "summary": "Alternative route for mercury ions.",
        }
    )
    backend.save(first)
    backend.save(second)

    results = backend.search("Hg2+ affinity", project="demo", limit=2)

    assert [result.memory.memory_id for result in results] == [second.memory_id, first.memory_id]
    assert all("rerank" in result.match_reason for result in results)
