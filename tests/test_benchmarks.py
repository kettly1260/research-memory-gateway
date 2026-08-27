import importlib.util
from pathlib import Path

from research_memory_gateway.agent_surface.capture import capture_memory
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


def test_recall_benchmark_has_at_least_30_balanced_cases() -> None:
    cases = load_jsonl(str(ROOT / "benchmarks" / "recall_cases.jsonl"))

    assert len(cases) >= 30
    assert any(case["should_recall"] for case in cases)
    assert any(not case["should_recall"] for case in cases)
    assert len({case["case_id"] for case in cases}) == len(cases)


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
