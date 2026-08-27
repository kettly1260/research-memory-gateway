from __future__ import annotations

import json
import sys
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


def score_recall(cases: list[dict[str, Any]], results: list[dict[str, Any]]) -> dict[str, Any]:
    result_by_id = {row["case_id"]: row for row in results}
    positive = [case for case in cases if case["should_recall"]]
    negative = [case for case in cases if not case["should_recall"]]
    recalled = sum(bool(result_by_id.get(case["case_id"], {}).get("did_recall")) for case in positive)
    correct = sum(bool(result_by_id.get(case["case_id"], {}).get("correct_memory")) for case in positive)
    false_positive = sum(bool(result_by_id.get(case["case_id"], {}).get("did_recall")) for case in negative)
    return {
        "cases": len(cases),
        "results_received": len(result_by_id),
        "positive_cases": len(positive),
        "negative_cases": len(negative),
        "recall_rate": ratio(recalled, len(positive)),
        "correct_memory_rate": ratio(correct, len(positive)),
        "false_positive_rate": ratio(false_positive, len(negative)),
        "targets": {
            "recall_rate": ">=0.90",
            "correct_memory_rate": ">=0.90",
            "false_positive_rate": "<0.10",
        },
    }


def score_capture(cases: list[dict[str, Any]], results: list[dict[str, Any]]) -> dict[str, Any]:
    result_by_id = {row["case_id"]: row for row in results}
    positive = [case for case in cases if case["should_capture"]]
    negative = [case for case in cases if not case["should_capture"]]
    captured = sum(bool(result_by_id.get(case["case_id"], {}).get("did_capture")) for case in positive)
    correct_tier = sum(
        result_by_id.get(case["case_id"], {}).get("actual_tier") == case.get("expected_tier")
        for case in positive
    )
    false_capture = sum(bool(result_by_id.get(case["case_id"], {}).get("did_capture")) for case in negative)
    return {
        "cases": len(cases),
        "results_received": len(result_by_id),
        "positive_cases": len(positive),
        "negative_cases": len(negative),
        "capture_rate": ratio(captured, len(positive)),
        "correct_tier_rate": ratio(correct_tier, len(positive)),
        "false_capture_rate": ratio(false_capture, len(negative)),
        "targets": {
            "capture_rate": ">=0.80",
            "false_capture_rate": "<0.10",
        },
    }


def main() -> None:
    if len(sys.argv) != 4 or sys.argv[1] not in {"recall", "capture"}:
        raise SystemExit(
            "usage: score_invocations.py <recall|capture> <cases.jsonl> <results.jsonl>"
        )
    mode, cases_path, results_path = sys.argv[1:]
    cases = load_jsonl(cases_path)
    results = load_jsonl(results_path)
    score = score_recall(cases, results) if mode == "recall" else score_capture(cases, results)
    print(json.dumps(score, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
