from __future__ import annotations

import argparse
from pathlib import Path

from research_memory_gateway.backends import SQLiteMemoryBackend
from research_memory_gateway.models import ResearchMemory

ROOT = Path(__file__).parents[1]
DEFAULT_DB = ROOT / "data" / "benchmark_memory.db"

MEMORIES = (
    {
        "memory_id": "mem_benchmark_fe_conditions",
        "project": "Fe3-probe",
        "topic": "Fe3+ stock and buffer conditions",
        "memory_type": "material_system",
        "title": "Fe3+ stock preparation and HEPES condition",
        "summary": (
            "Fe3+ stock was prepared at 10 mM in 0.1 M HNO3. "
            "The working HEPES concentration was 20 mM."
        ),
        "claims": [
            {"claim": "Fe3+ stock concentration was 10 mM."},
            {"claim": "Fe3+ stock used 0.1 M HNO3 as the acidic medium."},
            {"claim": "The working HEPES concentration was 20 mM."},
        ],
        "tags": ["Fe3+", "HNO3", "HEPES", "solution preparation"],
    },
    {
        "memory_id": "mem_benchmark_dmso",
        "project": "Fe3-probe",
        "topic": "DMSO grade change",
        "memory_type": "research_decision",
        "title": "DMSO grade used for Pyr PL testing",
        "summary": (
            "The latest Pyr PL test used newly purchased HPLC-grade DMSO; "
            "the earlier solvent was chemical-purity DMSO."
        ),
        "claims": [
            {
                "claim": (
                    "The latest Pyr PL test used HPLC-grade DMSO instead of the earlier "
                    "chemical-purity DMSO."
                )
            }
        ],
        "tags": ["Pyr", "PL", "DMSO"],
    },
    {
        "memory_id": "mem_benchmark_pl_state",
        "project": "Fe3-probe",
        "topic": "PL experiment state",
        "memory_type": "experiment_plan",
        "title": "Pyr PL retest state and next step",
        "summary": (
            "The Pyr 0Fe 1-60 minute full-spectrum retest was completed after reinstalling "
            "the dropped optical filter. Continue with the remaining PL test schedule."
        ),
        "claims": [
            {"claim": "The Pyr 0Fe 1-60 minute full-spectrum retest was completed."}
        ],
        "next_actions": ["Continue the remaining PL test schedule."],
        "metadata": {"plan_status": "active"},
        "tags": ["Pyr", "PL", "retest"],
    },
    {
        "memory_id": "mem_benchmark_pdf_workflow",
        "project": "paper-fulltext-mcp",
        "topic": "PDF acquisition troubleshooting",
        "memory_type": "workflow_plan",
        "memory_tier": "ambient",
        "title": "PDF download issue resolution",
        "summary": (
            "The PDF acquisition issue was resolved by preferring Zotero Desktop native "
            "acquisition before launching browser automation."
        ),
        "claims": [
            {
                "claim": (
                    "Use Zotero Desktop native acquisition first when resolving PDF downloads."
                )
            }
        ],
        "tags": ["PDF", "Zotero", "workflow"],
    },
    {
        "memory_id": "mem_benchmark_browser_decision",
        "project": "paper-fulltext-mcp",
        "topic": "browser debugging decision",
        "memory_type": "research_decision",
        "memory_tier": "ambient",
        "title": "Playwright and Edge debugging decision",
        "summary": (
            "Use Playwright for repeatable browser automation and Edge debugging only for "
            "interactive inspection of failures."
        ),
        "claims": [
            {
                "claim": (
                    "Playwright is the repeatable automation path; Edge debugging is for "
                    "interactive failure inspection."
                )
            }
        ],
        "tags": ["Playwright", "Edge", "debugging"],
    },
    {
        "memory_id": "mem_benchmark_origin_path",
        "project": "origin-mcp",
        "topic": "repository path",
        "memory_type": "workflow_plan",
        "memory_tier": "ambient",
        "title": "Origin MCP repository path",
        "summary": "The Origin MCP repository is located at G:\\LLM\\originlab-jx.",
        "claims": [
            {"claim": "The Origin MCP repository path is G:\\LLM\\originlab-jx."}
        ],
        "next_actions": ["Resume the pending Origin MCP implementation work."],
        "tags": ["Origin MCP", "repository path"],
    },
    {
        "memory_id": "mem_benchmark_gateway_strategy",
        "project": "research-memory-gateway",
        "topic": "V2 branch and dependency decisions",
        "memory_type": "research_decision",
        "memory_tier": "ambient",
        "title": "Research Memory Gateway V2 strategy",
        "summary": (
            "The frozen V1 branch is legacy/research-memory-gateway-v1. EverOS was deferred "
            "because V2 first needs to prove that agents autonomously use recall, capture, "
            "and verify. Continue V2 work on the hardening line until client acceptance."
        ),
        "claims": [
            {
                "claim": (
                    "The frozen V1 branch is legacy/research-memory-gateway-v1."
                )
            },
            {
                "claim": (
                    "EverOS was deferred until autonomous memory-tool invocation is proven."
                )
            },
        ],
        "tags": ["V2", "legacy branch", "EverOS"],
    },
    {
        "memory_id": "mem_benchmark_data_path",
        "project": "research-data-organization",
        "topic": "final data organization path",
        "memory_type": "workflow_plan",
        "memory_tier": "ambient",
        "title": "Research data organization directory",
        "summary": "Final organized research data should be stored under G:\\Research\\organized-data.",
        "claims": [
            {
                "claim": (
                    "The final research data organization directory is "
                    "G:\\Research\\organized-data."
                )
            }
        ],
        "tags": ["data organization", "path"],
    },
)


def seed_corpus(db_path: Path) -> list[str]:
    backend = SQLiteMemoryBackend(str(db_path))
    memory_ids: list[str] = []
    for payload in MEMORIES:
        memory = ResearchMemory.model_validate(payload)
        backend.save(memory)
        memory_ids.append(memory.memory_id)
    return memory_ids


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed the deterministic Recall benchmark corpus")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    args = parser.parse_args()
    memory_ids = seed_corpus(args.db.resolve())
    print(f"Seeded {len(memory_ids)} benchmark memories into {args.db.resolve()}")


if __name__ == "__main__":
    main()
