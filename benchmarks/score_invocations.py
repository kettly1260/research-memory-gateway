from __future__ import annotations

import json
import sys
from math import ceil
from pathlib import Path
from typing import Any


def load_jsonl(path: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
    return rows


def ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def index_results(
    cases: list[dict[str, Any]], results: list[dict[str, Any]]
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    case_ids = {row["case_id"] for row in cases}
    result_by_id: dict[str, dict[str, Any]] = {}
    for row in results:
        case_id = row.get("case_id")
        if not isinstance(case_id, str) or not case_id:
            raise ValueError("every result must contain a non-empty string case_id")
        if case_id not in case_ids:
            raise ValueError(f"unknown benchmark case_id: {case_id}")
        if case_id in result_by_id:
            raise ValueError(f"duplicate benchmark result for case_id: {case_id}")
        result_by_id[case_id] = row
    missing = sorted(case_ids - result_by_id.keys())
    return result_by_id, missing


def operational_metrics(results: list[dict[str, Any]]) -> dict[str, Any]:
    latencies = sorted(
        float(row["latency_ms"])
        for row in results
        if isinstance(row.get("latency_ms"), int | float)
    )
    token_usage = [
        int(row["token_usage"])
        for row in results
        if isinstance(row.get("token_usage"), int) and row["token_usage"] >= 0
    ]
    return {
        "latency_samples": len(latencies),
        "average_latency_ms": round(sum(latencies) / len(latencies), 2) if latencies else None,
        "p95_latency_ms": latencies[max(0, ceil(len(latencies) * 0.95) - 1)]
        if latencies
        else None,
        "token_samples": len(token_usage),
        "total_token_usage": sum(token_usage) if token_usage else None,
        "average_token_usage": round(sum(token_usage) / len(token_usage), 2)
        if token_usage
        else None,
    }


def score_recall(cases: list[dict[str, Any]], results: list[dict[str, Any]]) -> dict[str, Any]:
    result_by_id, missing = index_results(cases, results)
    positive = [case for case in cases if case["should_recall"]]
    negative = [case for case in cases if not case["should_recall"]]
    explicit = [case for case in positive if case.get("category") == "explicit_history"]
    recalled = sum(bool(result_by_id.get(case["case_id"], {}).get("did_recall")) for case in positive)
    explicit_recalled = sum(
        bool(result_by_id.get(case["case_id"], {}).get("did_recall")) for case in explicit
    )
    correct = sum(bool(result_by_id.get(case["case_id"], {}).get("correct_memory")) for case in positive)
    false_positive = sum(bool(result_by_id.get(case["case_id"], {}).get("did_recall")) for case in negative)
    explicit_recall_rate = ratio(explicit_recalled, len(explicit))
    correct_memory_rate = ratio(correct, len(positive))
    false_positive_rate = ratio(false_positive, len(negative))
    gates = {
        "explicit_recall_rate": explicit_recall_rate >= 0.90,
        "correct_memory_rate": correct_memory_rate >= 0.90,
        "false_positive_rate": false_positive_rate < 0.10,
    }
    return {
        "cases": len(cases),
        "results_received": len(result_by_id),
        "complete": not missing,
        "missing_case_ids": missing,
        "positive_cases": len(positive),
        "negative_cases": len(negative),
        "recall_rate": ratio(recalled, len(positive)),
        "explicit_recall_rate": explicit_recall_rate,
        "correct_memory_rate": correct_memory_rate,
        "false_positive_rate": false_positive_rate,
        "operational": operational_metrics(list(result_by_id.values())),
        "targets": {
            "explicit_recall_rate": ">=0.90",
            "correct_memory_rate": ">=0.90",
            "false_positive_rate": "<0.10",
        },
        "gates": gates,
        "passed": not missing and all(gates.values()),
    }


def score_capture(cases: list[dict[str, Any]], results: list[dict[str, Any]]) -> dict[str, Any]:
    result_by_id, missing = index_results(cases, results)
    positive = [case for case in cases if case["should_capture"]]
    negative = [case for case in cases if not case["should_capture"]]
    captured = sum(bool(result_by_id.get(case["case_id"], {}).get("did_capture")) for case in positive)
    correct_tier = sum(
        result_by_id.get(case["case_id"], {}).get("actual_tier") == case.get("expected_tier")
        for case in positive
    )
    false_capture = sum(bool(result_by_id.get(case["case_id"], {}).get("did_capture")) for case in negative)
    capture_rate = ratio(captured, len(positive))
    correct_tier_rate = ratio(correct_tier, len(positive))
    false_capture_rate = ratio(false_capture, len(negative))
    gates = {
        "capture_rate": capture_rate >= 0.80,
        "false_capture_rate": false_capture_rate < 0.10,
    }
    return {
        "cases": len(cases),
        "results_received": len(result_by_id),
        "complete": not missing,
        "missing_case_ids": missing,
        "positive_cases": len(positive),
        "negative_cases": len(negative),
        "capture_rate": capture_rate,
        "correct_tier_rate": correct_tier_rate,
        "false_capture_rate": false_capture_rate,
        "operational": operational_metrics(list(result_by_id.values())),
        "targets": {
            "capture_rate": ">=0.80",
            "false_capture_rate": "<0.10",
        },
        "gates": gates,
        "passed": not missing and all(gates.values()),
    }


def main() -> None:
    require_pass = "--require-pass" in sys.argv
    args = [arg for arg in sys.argv[1:] if arg != "--require-pass"]
    if len(args) != 3 or args[0] not in {"recall", "capture"}:
        raise SystemExit(
            "usage: score_invocations.py <recall|capture> <cases.jsonl> <results.jsonl> "
            "[--require-pass]"
        )
    mode, cases_path, results_path = args
    cases = load_jsonl(cases_path)
    results = load_jsonl(results_path)
    score = score_recall(cases, results) if mode == "recall" else score_capture(cases, results)
    print(json.dumps(score, ensure_ascii=False, indent=2))
    if require_pass and not score["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
