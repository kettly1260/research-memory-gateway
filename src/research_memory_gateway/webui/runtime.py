from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from difflib import unified_diff
from typing import Any
from uuid import uuid4

from ..backends import SQLiteMemoryBackend, memory_to_search_document
from ..models import MemoryStatus, ResearchMemory
from ..service import ResearchMemoryService


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def diff_memory(before: ResearchMemory, after_data: dict[str, Any]) -> str:
    before_text = json.dumps(before.model_dump(mode="json"), indent=2, ensure_ascii=False).splitlines()
    after = before.model_dump(mode="json")
    after.update(after_data)
    after["memory_id"] = before.memory_id
    after_text = json.dumps(after, indent=2, ensure_ascii=False).splitlines()
    return "\n".join(unified_diff(before_text, after_text, fromfile="before", tofile="after", lineterm=""))


def memory_filters(service: ResearchMemoryService) -> dict[str, list[str]]:
    memories = service.backend.list_all(statuses=[status.value for status in MemoryStatus])
    return {
        "projects": sorted({memory.project for memory in memories}),
        "topics": sorted({memory.topic for memory in memories}),
        "tags": sorted({tag for memory in memories for tag in memory.tags}),
        "memory_types": sorted({memory.memory_type.value for memory in memories}),
    }


def import_validate(service: ResearchMemoryService, payload: Any) -> dict[str, Any]:
    raw_items = payload if isinstance(payload, list) else payload.get("memories", []) if isinstance(payload, dict) else []
    valid: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    seen: set[str] = set()
    duplicate_ids: list[str] = []
    overlaps: dict[str, Any] = {}
    conflicts: list[str] = []
    for index, item in enumerate(raw_items):
        try:
            memory = ResearchMemory.model_validate(item)
        except ValueError as exc:
            invalid.append({"index": index, "error": str(exc)})
            continue
        if memory.memory_id in seen:
            duplicate_ids.append(memory.memory_id)
        seen.add(memory.memory_id)
        if service.backend.get(memory.memory_id):
            conflicts.append(memory.memory_id)
        overlap = service.check_overlap(query=f"{memory.title} {memory.summary}", project=memory.project, limit=3)
        if overlap:
            overlaps[memory.memory_id] = overlap
        valid.append(memory.model_dump(mode="json"))
    return {
        "valid": len(valid),
        "invalid": len(invalid),
        "items": valid,
        "errors": invalid,
        "duplicate_memory_ids": duplicate_ids,
        "overlap_candidates": overlaps,
        "conflicts": conflicts,
    }


def import_execute(service: ResearchMemoryService, payload: Any, policy: str) -> dict[str, Any]:
    validation = import_validate(service, payload)
    result = {"imported": 0, "skipped": 0, "overwritten": 0, "as_new": 0, "errors": validation["errors"]}
    for item in validation["items"]:
        existing = service.backend.get(item["memory_id"])
        if existing and policy == "skip_existing":
            result["skipped"] += 1
            continue
        if existing and policy == "overwrite_existing":
            service.update_research_memory(item["memory_id"], item, user_confirmed=True)
            result["overwritten"] += 1
            continue
        if policy == "import_as_new":
            original_id = item["memory_id"]
            item["memory_id"] = f"mem_{uuid4().hex}"
            metadata = dict(item.get("metadata") or {})
            metadata["imported_original_memory_id"] = original_id
            item["metadata"] = metadata
            result["as_new"] += 1
        service.save_research_memory(user_confirmed=True, memory=item)
        result["imported"] += 1
    service.append_audit_event("import.json_completed", metadata={k: v for k, v in result.items() if k != "errors"})
    return result


@dataclass
class BackfillJob:
    job_id: str
    status: str = "running"
    total: int = 0
    completed: int = 0
    failed: int = 0
    skipped: int = 0
    started_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    last_error: str | None = None
    cancel_requested: bool = False

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


class BackfillManager:
    def __init__(self, service: ResearchMemoryService) -> None:
        self.service = service
        self.jobs: dict[str, BackfillJob] = {}
        self.active_job_id: str | None = None

    def coverage(self) -> dict[str, Any]:
        backend = self.service.backend
        if not isinstance(backend, SQLiteMemoryBackend):
            return {"supported": False}
        with backend._connect() as connection:
            total = connection.execute("SELECT COUNT(*) FROM memories WHERE memory_status = 'active'").fetchone()[0]
            embedded = connection.execute(
                """
                SELECT COUNT(*) FROM memories m JOIN memory_embeddings e ON e.memory_id = m.memory_id
                WHERE m.memory_status = 'active'
                """
            ).fetchone()[0]
        return {"supported": True, "active_total": total, "active_embedded": embedded, "missing": max(0, total - embedded)}

    def dry_run(self, options: dict[str, Any]) -> dict[str, Any]:
        targets = self._targets(options)
        result = {"total": len(targets), "would_backfill": len(targets), "dry_run": True}
        self.service.append_audit_event("retrieval.backfill_dry_run", metadata=result)
        return result

    def start(self, options: dict[str, Any]) -> BackfillJob:
        if self.active_job_id and self.jobs[self.active_job_id].status == "running":
            raise RuntimeError("A backfill job is already running")
        job = BackfillJob(job_id=f"job_{uuid4().hex[:12]}")
        self.jobs[job.job_id] = job
        self.active_job_id = job.job_id
        self.service.append_audit_event("retrieval.backfill_started", metadata={"job_id": job.job_id})
        asyncio.create_task(self._run(job, options))
        return job

    def cancel(self, job_id: str) -> BackfillJob:
        job = self.jobs[job_id]
        job.cancel_requested = True
        job.updated_at = utc_now()
        self.service.append_audit_event("retrieval.backfill_cancelled", metadata={"job_id": job_id})
        return job

    async def _run(self, job: BackfillJob, options: dict[str, Any]) -> None:
        started = time.monotonic()
        targets = self._targets(options)
        job.total = len(targets)
        concurrency = max(1, min(int(options.get("concurrency", 2)), 4))
        timeout = max(60, min(int(options.get("job_timeout_seconds", 1800)), 86400))
        semaphore = asyncio.Semaphore(concurrency)

        async def one(memory: ResearchMemory) -> None:
            async with semaphore:
                if job.cancel_requested or time.monotonic() - started > timeout:
                    job.skipped += 1
                    return
                backend = self.service.backend
                try:
                    if not isinstance(backend, SQLiteMemoryBackend):
                        job.failed += 1
                        return
                    backend._refresh_retrieval_clients()
                    embedding = await asyncio.to_thread(backend.embedding_client.embed, memory_to_search_document(memory))
                    if not embedding:
                        job.failed += 1
                        job.last_error = getattr(backend.embedding_client, "last_error", None)
                        return
                    with backend._connect() as connection:
                        connection.execute("DELETE FROM memory_embeddings WHERE memory_id = ?", (memory.memory_id,))
                        connection.execute(
                            "INSERT INTO memory_embeddings(memory_id, embedding, updated_at) VALUES (?, ?, ?)",
                            (memory.memory_id, json.dumps(embedding), utc_now()),
                        )
                    job.completed += 1
                except Exception as exc:  # noqa: BLE001 - job status should capture failures, not crash task.
                    job.failed += 1
                    job.last_error = exc.__class__.__name__
                finally:
                    job.updated_at = utc_now()

        await asyncio.gather(*(one(memory) for memory in targets))
        if job.cancel_requested:
            job.status = "cancelled"
        elif job.failed:
            job.status = "failed" if job.completed == 0 else "completed_with_errors"
        else:
            job.status = "completed"
        job.updated_at = utc_now()
        if self.active_job_id == job.job_id:
            self.active_job_id = None
        self.service.append_audit_event(f"retrieval.backfill_{'failed' if job.failed and not job.completed else 'completed'}", metadata=job.as_dict())

    def _targets(self, options: dict[str, Any]) -> list[ResearchMemory]:
        scope = options.get("scope", "active")
        statuses = [MemoryStatus.active.value]
        if scope == "active_archived":
            statuses.append(MemoryStatus.archived.value)
        elif scope == "all":
            statuses = [status.value for status in MemoryStatus]
        memories = self.service.backend.list_all(statuses=statuses)
        if options.get("project"):
            memories = [memory for memory in memories if memory.project == options["project"]]
        if options.get("memory_type"):
            memories = [memory for memory in memories if memory.memory_type.value == options["memory_type"]]
        limit = options.get("limit", 100)
        if limit != "all":
            memories = memories[: max(0, min(int(limit), 1000))]
        return memories
