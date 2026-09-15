from __future__ import annotations

import asyncio
import json
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from difflib import unified_diff
from typing import Any

from ..backends import SQLiteMemoryBackend, memory_to_search_document
from ..models import MemoryStatus, ResearchMemory
from ..service import ResearchMemoryService


IMPORT_POLICIES = {"skip_existing", "overwrite_existing", "import_as_new"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def diff_json(before: Any, after: Any) -> str:
    before_text = json.dumps(before, ensure_ascii=False, indent=2, sort_keys=True).splitlines()
    after_text = json.dumps(after, ensure_ascii=False, indent=2, sort_keys=True).splitlines()
    return "\n".join(unified_diff(before_text, after_text, lineterm=""))


def diff_memory(before: ResearchMemory, after_data: dict[str, Any]) -> str:
    after = before.model_dump(mode="json")
    after.update(after_data)
    after["memory_id"] = before.memory_id
    return diff_json(before.model_dump(mode="json"), after)


def memory_filters(service: ResearchMemoryService) -> dict[str, list[str]]:
    memories = service.backend.list_all(statuses=[status.value for status in MemoryStatus])
    return {
        "projects": sorted({memory.project for memory in memories}),
        "topics": sorted({memory.topic for memory in memories}),
        "tags": sorted({tag for memory in memories for tag in memory.tags}),
        "memory_types": sorted({memory.memory_type.value for memory in memories}),
    }


class ImportValidationError(ValueError):
    def __init__(self, validation: dict[str, Any]) -> None:
        super().__init__("invalid_import_payload")
        self.validation = validation


class ImportConfirmationRequired(PermissionError):
    def __init__(self, diffs: dict[str, str]) -> None:
        super().__init__("confirmation_required")
        self.diffs = diffs


class UnsupportedImportPolicy(ValueError):
    def __init__(self, policy: str) -> None:
        super().__init__("unsupported_import_policy")
        self.policy = policy


def import_items(payload: Any) -> list[Any]:
    if isinstance(payload, dict):
        items = payload.get("memories", [])
    else:
        items = payload
    return items if isinstance(items, list) else []


def import_validate(service: ResearchMemoryService, payload: Any) -> dict[str, Any]:
    items = import_items(payload)
    seen: set[str] = set()
    valid = 0
    invalid = 0
    duplicate_ids: list[str] = []
    errors: list[dict[str, Any]] = []
    overlaps: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        try:
            memory = service.validate_research_memory_for_write(item)
            valid += 1
            if memory.memory_id in seen or service.backend.get(memory.memory_id) is not None:
                duplicate_ids.append(memory.memory_id)
            seen.add(memory.memory_id)
            overlaps.extend(
                service.check_overlap(
                    query=f"{memory.title} {memory.summary}",
                    project=memory.project,
                    limit=3,
                )
            )
        except ValueError as exc:
            invalid += 1
            errors.append({"index": index, "error": str(exc)})
    return {
        "valid": valid,
        "invalid": invalid,
        "duplicates": len(duplicate_ids),
        "duplicate_memory_id": duplicate_ids,
        "overlap_candidates": overlaps,
        "conflicts": duplicate_ids,
        "errors": errors,
    }


def import_execute(
    service: ResearchMemoryService,
    payload: Any,
    *,
    policy: str = "skip_existing",
    confirmed: bool = False,
) -> dict[str, Any]:
    if policy not in IMPORT_POLICIES:
        raise UnsupportedImportPolicy(policy)

    items = import_items(payload)
    validation = import_validate(service, payload)
    if validation["invalid"]:
        raise ImportValidationError(validation)
    if policy == "overwrite_existing" and not confirmed:
        diffs: dict[str, str] = {}
        for item in items:
            incoming = service.validate_research_memory_for_write(item)
            existing = service.backend.get(incoming.memory_id)
            if existing is None:
                continue
            diffs[incoming.memory_id] = diff_json(
                existing.model_dump(mode="json"),
                incoming.model_dump(mode="json"),
            )
        if diffs:
            raise ImportConfirmationRequired(diffs)

    imported = 0
    skipped = 0
    for item in items:
        memory = service.validate_research_memory_for_write(item)
        exists = service.backend.get(memory.memory_id) is not None
        if exists and policy == "skip_existing":
            skipped += 1
            continue
        data = dict(item)
        if policy == "import_as_new":
            data.pop("memory_id", None)
            metadata = dict(data.get("metadata") or {})
            original_id = memory.memory_id
            metadata["imported_original_memory_id"] = original_id
            data["metadata"] = metadata
        service.save_research_memory(
            user_confirmed=True,
            memory=data,
            confirmation={
                "source": "webui_import",
                "text": f"JSON import policy={policy}",
                "confirmed_by": "webui_user",
            },
        )
        imported += 1

    result = {"imported": imported, "skipped": skipped}
    service.append_audit_event(
        "import.json_completed",
        metadata={"imported": imported, "skipped": skipped, "policy": policy},
    )
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
    batch_size: int = 8
    concurrency: int = 2
    request_timeout_seconds: int = 30
    job_timeout_seconds: int = 1800

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


class BackfillManager:
    def __init__(self, service: ResearchMemoryService) -> None:
        self.service = service
        self.jobs: dict[str, BackfillJob] = {}
        self.running_job_id: str | None = None

    def coverage(self) -> dict[str, Any]:
        memories = self.service.backend.list_all(statuses=[status.value for status in MemoryStatus])
        existing = self.embedded_memory_ids()
        return {
            "total": len(memories),
            "embedded": len(existing),
            "missing": max(0, len(memories) - len(existing)),
        }

    def dry_run(self, options: dict[str, Any]) -> dict[str, Any]:
        targets = self._targets(options)
        existing = self.embedded_memory_ids()
        candidates = [
            memory for memory in targets if options.get("force") or memory.memory_id not in existing
        ]
        self.service.append_audit_event(
            "retrieval.backfill_dry_run",
            metadata={"total": len(candidates)},
        )
        return {"total": len(candidates), "memory_ids": [memory.memory_id for memory in candidates]}

    def start(self, options: dict[str, Any]) -> BackfillJob:
        if self.running_job_id and self.jobs[self.running_job_id].status == "running":
            raise RuntimeError("Only one backfill job can run at a time")
        job = BackfillJob(
            job_id=f"bf_{secrets.token_hex(8)}",
            batch_size=bounded_int(options.get("batch_size"), 1, 32, 8),
            concurrency=bounded_int(options.get("concurrency"), 1, 4, 2),
            request_timeout_seconds=bounded_int(options.get("request_timeout_seconds"), 5, 120, 30),
            job_timeout_seconds=bounded_int(options.get("job_timeout_seconds"), 60, 86400, 1800),
        )
        self.jobs[job.job_id] = job
        self.running_job_id = job.job_id
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
        try:
            targets = self._targets(options)
            existing = self.embedded_memory_ids()
            targets = [
                memory for memory in targets if options.get("force") or memory.memory_id not in existing
            ]
            job.total = len(targets)
            backend = self.service.backend
            if hasattr(backend, "embedding_client"):
                backend.embedding_client.config.timeout_seconds = job.request_timeout_seconds
            started = time.monotonic()
            for batch_start in range(0, len(targets), job.batch_size):
                if job.cancel_requested:
                    job.status = "cancelled"
                    break
                if time.monotonic() - started > job.job_timeout_seconds:
                    job.status = "failed"
                    job.last_error = "job_timeout"
                    break
                batch = targets[batch_start : batch_start + job.batch_size]
                semaphore = asyncio.Semaphore(job.concurrency)
                results = await asyncio.gather(
                    *(self._backfill_one(job, memory, semaphore) for memory in batch)
                )
                for result, error in results:
                    if result == "completed":
                        job.completed += 1
                    elif result == "skipped":
                        job.skipped += 1
                    else:
                        job.failed += 1
                        job.last_error = error or "embedding_failed"
                job.updated_at = utc_now()
            if job.status == "running":
                job.status = "completed"
                self.service.append_audit_event(
                    "retrieval.backfill_completed",
                    metadata={"job_id": job.job_id},
                )
            elif job.status == "failed":
                self.service.append_audit_event(
                    "retrieval.backfill_failed",
                    metadata={"job_id": job.job_id, "error": job.last_error},
                )
        except Exception as exc:  # pragma: no cover - defensive safety for background task
            job.status = "failed"
            job.last_error = exc.__class__.__name__
            self.service.append_audit_event(
                "retrieval.backfill_failed",
                metadata={"job_id": job.job_id, "error": job.last_error},
            )
        finally:
            job.updated_at = utc_now()
            if self.running_job_id == job.job_id:
                self.running_job_id = None

    async def _backfill_one(
        self,
        job: BackfillJob,
        memory: ResearchMemory,
        semaphore: asyncio.Semaphore,
    ) -> tuple[str, str | None]:
        async with semaphore:
            if job.cancel_requested:
                return "skipped", "cancelled"
            backend = self.service.backend
            embedding_client = getattr(backend, "embedding_client", None)
            if embedding_client is None or not embedding_client.enabled:
                return "skipped", None
            vector = await asyncio.to_thread(embedding_client.embed, memory_to_search_document(memory))
            if not vector:
                return "failed", getattr(embedding_client, "last_error", "embedding_failed")
            if not isinstance(backend, SQLiteMemoryBackend):
                return "failed", "unsupported_backend"
            with backend._connect() as connection:
                connection.execute("DELETE FROM memory_embeddings WHERE memory_id = ?", (memory.memory_id,))
                connection.execute(
                    "INSERT INTO memory_embeddings(memory_id, embedding, updated_at) VALUES (?, ?, ?)",
                    (memory.memory_id, json.dumps(vector), utc_now()),
                )
            return "completed", None

    def _targets(self, options: dict[str, Any]) -> list[ResearchMemory]:
        statuses = scope_to_statuses(options.get("scope", "active"))
        memories = self.service.backend.list_all(statuses=statuses)
        if options.get("project"):
            memories = [memory for memory in memories if memory.project == options["project"]]
        if options.get("memory_type"):
            memories = [memory for memory in memories if memory.memory_type.value == options["memory_type"]]
        limit = options.get("limit", 100)
        if limit != "all":
            memories = memories[: max(0, min(int(limit), 1000))]
        return memories

    def embedded_memory_ids(self) -> set[str]:
        backend = self.service.backend
        if not isinstance(backend, SQLiteMemoryBackend):
            return set()
        with backend._connect() as connection:
            return {
                row["memory_id"]
                for row in connection.execute("SELECT memory_id FROM memory_embeddings").fetchall()
            }


@dataclass
class ConversationVectorizationJob:
    job_id: str
    status: str = "running"
    total: int = 0
    completed: int = 0
    failed: int = 0
    skipped: int = 0
    total_sections: int = 0
    embedded_sections: int = 0
    cache_reused_sections: int = 0
    failed_sections: int = 0
    current_file: str | None = None
    started_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    last_error: str | None = None
    cancel_requested: bool = False
    job_timeout_seconds: int = 86400

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


class ConversationVectorizationManager:
    """Background vector backfill for the conversation archive index.

    Archive Markdown remains untouched. Each candidate file is re-indexed against
    the currently active embedding model so section identities and cached vectors
    move together after a model switch.
    """

    def __init__(self, service: ResearchMemoryService) -> None:
        self.service = service
        self.jobs: dict[str, ConversationVectorizationJob] = {}
        self.running_job_id: str | None = None
        self.tasks: dict[str, asyncio.Task[None]] = {}

    def dry_run(self, *, force: bool = False, limit: int | None = None) -> dict[str, Any]:
        retrieval = self.service.conversation_retrieval
        candidates = retrieval.index_db.embedding_backfill_candidates(force=force, limit=limit)
        stats = retrieval.index_db.stats()
        result = {
            "documents": len(candidates),
            "sections": sum(int(item["missing_sections"]) for item in candidates),
            "embedding_model": stats.get("embedding_model"),
            "embedding_version": stats.get("embedding_version"),
        }
        self.service.append_audit_event(
            "conversation.vectorization_dry_run",
            metadata={"documents": result["documents"], "sections": result["sections"]},
        )
        return result

    def start(
        self,
        *,
        force: bool = False,
        limit: int | None = None,
        job_timeout_seconds: int = 86400,
    ) -> ConversationVectorizationJob:
        if self.running_job_id and self.jobs[self.running_job_id].status == "running":
            raise RuntimeError("Only one conversation vectorization job can run at a time")

        retrieval = self.service.conversation_retrieval
        embedding_client = retrieval.embedding_client
        if embedding_client is None or not embedding_client.enabled or not embedding_client.model:
            raise RuntimeError("Conversation embedding is not configured or enabled")

        candidates = retrieval.index_db.embedding_backfill_candidates(force=force, limit=limit)
        job = ConversationVectorizationJob(
            job_id=f"cv_{secrets.token_hex(8)}",
            total=len(candidates),
            total_sections=sum(int(item["missing_sections"]) for item in candidates),
            job_timeout_seconds=bounded_int(job_timeout_seconds, 60, 604800, 86400),
        )
        self.jobs[job.job_id] = job
        self.running_job_id = job.job_id
        self.service.append_audit_event(
            "conversation.vectorization_started",
            metadata={
                "job_id": job.job_id,
                "documents": job.total,
                "sections": job.total_sections,
            },
        )
        task = asyncio.create_task(self._run(job, candidates))
        self.tasks[job.job_id] = task
        task.add_done_callback(lambda _task, job_id=job.job_id: self.tasks.pop(job_id, None))
        return job

    def cancel(self, job_id: str) -> ConversationVectorizationJob:
        job = self.jobs[job_id]
        job.cancel_requested = True
        job.updated_at = utc_now()
        self.service.append_audit_event(
            "conversation.vectorization_cancel_requested",
            metadata={"job_id": job_id},
        )
        return job

    async def _run(
        self,
        job: ConversationVectorizationJob,
        candidates: list[dict[str, Any]],
    ) -> None:
        retrieval = self.service.conversation_retrieval
        index_db = retrieval.index_db
        index_db.reset_embedding_stats()
        started = time.monotonic()
        try:
            if not candidates:
                job.status = "completed"
                self.service.append_audit_event(
                    "conversation.vectorization_completed",
                    metadata={
                        "job_id": job.job_id,
                        "status": job.status,
                        "completed_documents": 0,
                        "failed_documents": 0,
                        "embedded_sections": 0,
                        "cache_reused_sections": 0,
                        "failed_sections": 0,
                    },
                )
                return

            for item in candidates:
                if job.cancel_requested:
                    job.status = "cancelled"
                    break
                if time.monotonic() - started > job.job_timeout_seconds:
                    job.status = "failed"
                    job.last_error = "job_timeout"
                    break

                path = str(item["vault_path"])
                job.current_file = path
                job.updated_at = utc_now()
                try:
                    await asyncio.to_thread(index_db.index_file, path)
                    job.completed += 1
                except FileNotFoundError:
                    job.failed += 1
                    job.last_error = f"missing_file: {path}"
                except Exception as exc:
                    job.failed += 1
                    job.last_error = f"{exc.__class__.__name__}: {exc}"

                emb_stats = index_db.embedding_stats
                job.embedded_sections = int(emb_stats["new_embedding_requests"])
                job.cache_reused_sections = int(emb_stats["cache_reused"])
                job.failed_sections = int(emb_stats["failures"])
                job.updated_at = utc_now()

            if job.status == "running":
                job.status = "completed_with_errors" if job.failed or job.failed_sections else "completed"

            event_name = (
                "conversation.vectorization_completed"
                if job.status in {"completed", "completed_with_errors"}
                else "conversation.vectorization_failed"
                if job.status == "failed"
                else "conversation.vectorization_cancelled"
            )
            self.service.append_audit_event(
                event_name,
                metadata={
                    "job_id": job.job_id,
                    "status": job.status,
                    "completed_documents": job.completed,
                    "failed_documents": job.failed,
                    "embedded_sections": job.embedded_sections,
                    "cache_reused_sections": job.cache_reused_sections,
                    "failed_sections": job.failed_sections,
                },
            )
        except Exception as exc:  # pragma: no cover - defensive background guard
            job.status = "failed"
            job.last_error = f"{exc.__class__.__name__}: {exc}"
            self.service.append_audit_event(
                "conversation.vectorization_failed",
                metadata={"job_id": job.job_id, "error": job.last_error},
            )
        finally:
            job.current_file = None
            job.updated_at = utc_now()
            if self.running_job_id == job.job_id:
                self.running_job_id = None


def scope_to_statuses(scope: str) -> list[str]:
    if scope == "all":
        return [status.value for status in MemoryStatus]
    if scope == "active_archived":
        return [MemoryStatus.active.value, MemoryStatus.archived.value]
    return [MemoryStatus.active.value]


def validate_backfill_options(payload: dict[str, Any]) -> dict[str, Any]:
    data = dict(payload)
    data["concurrency"] = bounded_int(data.get("concurrency"), 1, 4, 2)
    data["batch_size"] = bounded_int(data.get("batch_size"), 1, 32, 8)
    data["request_timeout_seconds"] = bounded_int(
        data.get("request_timeout_seconds"),
        5,
        120,
        30,
    )
    data["job_timeout_seconds"] = bounded_int(data.get("job_timeout_seconds"), 60, 86400, 1800)
    return data


def bounded_int(value: Any, low: int, high: int, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(low, min(parsed, high))
