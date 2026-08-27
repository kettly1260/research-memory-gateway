import importlib.util
from pathlib import Path

import pytest

from research_memory_gateway.agent_surface.capture import capture_memory
from research_memory_gateway.agent_surface.recall import recall_memory
from research_memory_gateway.backends import SQLiteMemoryBackend
from research_memory_gateway.config import AppConfig
from research_memory_gateway.service import ResearchMemoryService

ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location(
    "score_invocations",
    ROOT / "benchmarks" / "score_invocations.py",
)
assert SPEC is not None and SPEC.loader is not None
SCORE_INVOCATIONS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SCORE_INVOCATIONS)
load_jsonl = SCORE_INVOCATIONS.load_jsonl
score_capture = SCORE_INVOCATIONS.score_capture
score_recall = SCORE_INVOCATIONS.score_recall

SEED_SPEC = importlib.util.spec_from_file_location(
    "seed_recall_corpus",
    ROOT / "benchmarks" / "seed_recall_corpus.py",
)
assert SEED_SPEC is not None and SEED_SPEC.loader is not None
SEED_RECALL_CORPUS = importlib.util.module_from_spec(SEED_SPEC)
SEED_SPEC.loader.exec_module(SEED_RECALL_CORPUS)
MEMORIES = SEED_RECALL_CORPUS.MEMORIES
seed_corpus = SEED_RECALL_CORPUS.seed_corpus


def test_recall_benchmark_has_at_least_30_balanced_cases() -> None:
    cases = load_jsonl(str(ROOT / "benchmarks" / "recall_cases.jsonl"))

    assert len(cases) >= 30
    assert any(case["should_recall"] for case in cases)
    assert any(not case["should_recall"] for case in cases)
    assert len({case["case_id"] for case in cases}) == len(cases)


def test_recall_benchmark_seed_corpus_is_stable_and_retrievable(tmp_path) -> None:
    db_path = tmp_path / "recall-corpus.db"
    memory_ids = seed_corpus(db_path)
    repeated_memory_ids = seed_corpus(db_path)
    config = AppConfig()
    config.backend.sqlite_path = str(db_path)
    service = ResearchMemoryService(config, SQLiteMemoryBackend(str(db_path)))

    assert len(memory_ids) == len(MEMORIES)
    assert len(set(memory_ids)) == len(memory_ids)
    assert repeated_memory_ids == memory_ids
    result = recall_memory(
        service,
        query="之前 Fe 的硝酸溶液怎么配的？",
        project="Fe3-probe",
    )
    assert result["results"][0]["memory_id"] == "mem_benchmark_fe_conditions"


def test_capture_benchmark_has_broad_cross_domain_coverage() -> None:
    cases = load_jsonl(str(ROOT / "benchmarks" / "capture_cases.jsonl"))

    assert len(cases) >= 60
    assert any(case["should_capture"] for case in cases)
    assert any(not case["should_capture"] for case in cases)
    assert len({case["case_id"] for case in cases}) == len(cases)
    categories = {case["category"] for case in cases}
    assert {
        "polymer_thermal",
        "ceramic_property",
        "electrochemistry",
        "spectroscopy",
        "thermal_analysis",
        "mechanical_property",
        "electrical_property",
        "surface_characterization",
        "microstructure",
        "literature_conclusion",
        "software_state",
        "sensitive_configuration",
        "casual_chat",
        "unsupported_speculation",
    }.issubset(categories)


def test_capture_benchmark_matches_gateway_default_policy(tmp_path) -> None:
    cases = load_jsonl(str(ROOT / "benchmarks" / "capture_cases.jsonl"))
    config = AppConfig()
    config.backend.sqlite_path = str(tmp_path / "capture-benchmark.db")
    service = ResearchMemoryService(config, SQLiteMemoryBackend(config.backend.sqlite_path))

    for case in cases:
        result = capture_memory(
            service,
            content=case["content"],
            project=f"benchmark-{case['case_id']}",
            user_confirmed=bool(case.get("user_confirmed", False)),
        )
        if case["should_capture"]:
            assert result["action"] in {"saved", "queued"}, case["case_id"]
            assert result["memory_tier"] == case["expected_tier"], case["case_id"]
        else:
            assert result["action"] == "ignored", case["case_id"]


def test_benchmark_scorers_compute_expected_rates() -> None:
    recall_cases = [
        {"case_id": "R1", "should_recall": True},
        {"case_id": "R2", "should_recall": False},
    ]
    recall_results = [
        {"case_id": "R1", "did_recall": True, "correct_memory": True},
        {"case_id": "R2", "did_recall": False},
    ]
    capture_cases = [
        {"case_id": "C1", "should_capture": True, "expected_tier": "trusted"},
        {"case_id": "C2", "should_capture": False, "expected_tier": None},
    ]
    capture_results = [
        {"case_id": "C1", "did_capture": True, "actual_tier": "trusted"},
        {"case_id": "C2", "did_capture": False},
    ]

    assert score_recall(recall_cases, recall_results)["recall_rate"] == 1.0
    assert score_recall(recall_cases, recall_results)["false_positive_rate"] == 0.0
    assert score_capture(capture_cases, capture_results)["capture_rate"] == 1.0
    assert score_capture(capture_cases, capture_results)["correct_tier_rate"] == 1.0


def test_recall_scorer_requires_complete_results_and_reports_operations() -> None:
    cases = [
        {"case_id": "R1", "should_recall": True, "category": "explicit_history"},
        {"case_id": "R2", "should_recall": False, "category": "negative_general_knowledge"},
    ]
    incomplete = score_recall(
        cases,
        [
            {
                "case_id": "R1",
                "did_recall": True,
                "correct_memory": True,
                "latency_ms": 120,
                "token_usage": 500,
            }
        ],
    )
    assert incomplete["complete"] is False
    assert incomplete["missing_case_ids"] == ["R2"]
    assert incomplete["passed"] is False

    complete = score_recall(
        cases,
        [
            {
                "case_id": "R1",
                "did_recall": True,
                "correct_memory": True,
                "latency_ms": 120,
                "token_usage": 500,
            },
            {"case_id": "R2", "did_recall": False, "latency_ms": 80, "token_usage": 300},
        ],
    )
    assert complete["complete"] is True
    assert complete["passed"] is True
    assert complete["operational"]["average_latency_ms"] == 100.0
    assert complete["operational"]["total_token_usage"] == 800


def test_benchmark_scorer_rejects_duplicate_and_unknown_case_ids() -> None:
    cases = [{"case_id": "R1", "should_recall": True, "category": "explicit_history"}]
    with pytest.raises(ValueError, match="duplicate"):
        score_recall(
            cases,
            [
                {"case_id": "R1", "did_recall": True},
                {"case_id": "R1", "did_recall": True},
            ],
        )
    with pytest.raises(ValueError, match="unknown"):
        score_recall(cases, [{"case_id": "R2", "did_recall": True}])
