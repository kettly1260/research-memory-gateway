from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import AppConfig
from .models import ExportFormat, ResearchMemory


def export_memories(
    memories: list[ResearchMemory], config: AppConfig, export_format: ExportFormat
) -> dict[str, Any]:
    outputs: dict[str, Any] = {"count": len(memories), "files": []}
    if export_format in (ExportFormat.json, ExportFormat.both):
        outputs["files"].extend(_export_json(memories, Path(config.export.json_dir)))
    if export_format in (ExportFormat.markdown, ExportFormat.both):
        outputs["files"].extend(_export_markdown(memories, Path(config.export.markdown_dir)))
    return outputs


def _export_json(memories: list[ResearchMemory], output_dir: Path) -> list[str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "research_memories.json"
    payload = [memory.model_dump(mode="json") for memory in memories]
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return [str(output_path)]


def _export_markdown(memories: list[ResearchMemory], output_dir: Path) -> list[str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    files: list[str] = []
    for memory in memories:
        type_dir = output_dir / memory.memory_type.value
        type_dir.mkdir(parents=True, exist_ok=True)
        filename = _safe_filename(f"{memory.created_at[:10]}-{memory.title}-{memory.memory_id}.md")
        output_path = type_dir / filename
        output_path.write_text(_to_markdown(memory), encoding="utf-8")
        files.append(str(output_path))
    return files


def _safe_filename(value: str) -> str:
    allowed = []
    for char in value:
        if char.isalnum() or char in " ._-":
            allowed.append(char)
        else:
            allowed.append("-")
    filename = "".join(allowed).strip(" .-")
    return filename[:180] or "memory.md"


def _to_markdown(memory: ResearchMemory) -> str:
    lines = [
        "---",
        f"memory_id: {memory.memory_id}",
        f"project: {memory.project}",
        f"topic: {memory.topic}",
        f"memory_type: {memory.memory_type.value}",
        f"created_at: {memory.created_at}",
        f"updated_at: {memory.updated_at}",
        f"tags: {json.dumps(memory.tags, ensure_ascii=False)}",
        "---",
        "",
        f"# {memory.title}",
        "",
        "## Summary",
        "",
        memory.summary,
        "",
        "## Claims",
        "",
    ]
    for claim in memory.claims:
        lines.append(
            f"- [{claim.verification_status.value}/{claim.confidence.value}] {claim.claim} "
            f"(evidence: {', '.join(claim.evidence_ids) or 'none'})"
        )
    lines.extend(["", "## Evidence", ""])
    for evidence in memory.evidence:
        lines.append(f"### {evidence.evidence_id}")
        if evidence.paper_title:
            lines.append(f"Paper: {evidence.paper_title}")
        if evidence.doi:
            lines.append(f"DOI: {evidence.doi}")
        if evidence.url:
            lines.append(f"URL: {evidence.url}")
        if evidence.file_path:
            lines.append(f"File: {evidence.file_path}")
        if evidence.quote:
            lines.extend(["", f"> {evidence.quote}"])
        lines.append("")
    lines.extend(["## Source Refs", ""])
    for source_ref in memory.source_refs:
        target = source_ref.path or source_ref.url or source_ref.doi or source_ref.source_id or "unknown"
        lines.append(f"- {source_ref.source_type}: {target}")
    lines.extend(["", "## Entities", ""])
    for entity in memory.entities:
        lines.append(f"- {entity.name} ({entity.entity_type})")
    lines.extend(["", "## Relations", ""])
    for relation in memory.relations:
        lines.append(
            f"- {relation.source} --{relation.relation}--> {relation.target} "
            f"({relation.confidence.value})"
        )
    lines.extend(["", "## Next Actions", ""])
    for action in memory.next_actions:
        lines.append(f"- {action}")
    lines.append("")
    return "\n".join(lines)
