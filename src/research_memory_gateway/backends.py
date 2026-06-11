from __future__ import annotations

import json
import os
import re
import sqlite3
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from logging import getLogger
from pathlib import Path
from typing import Any

import httpx

from .config import AppConfig, RetrievalConfig, RuntimeConfigResolver
from .models import MemoryStatus, ResearchMemory, SearchResult
from .retrieval import EmbeddingClient, RerankClient, cosine_similarity


logger = getLogger(__name__)
SCHEMA_VERSION = 4
ACTIVE_STATUSES = (MemoryStatus.active.value,)
ALL_STATUSES = tuple(status.value for status in MemoryStatus)


class MemoryBackend(ABC):
    @abstractmethod
    def save(self, memory: ResearchMemory) -> ResearchMemory:
        raise NotImplementedError

    @abstractmethod
    def search(
        self,
        query: str,
        *,
        project: str | None = None,
        memory_type: str | None = None,
        statuses: list[str] | None = None,
        limit: int = 10,
    ) -> list[SearchResult]:
        raise NotImplementedError

    @abstractmethod
    def list_all(self, *, statuses: list[str] | None = None) -> list[ResearchMemory]:
        raise NotImplementedError

    @abstractmethod
    def get(self, memory_id: str) -> ResearchMemory | None:
        raise NotImplementedError

    def retrieval_health(self) -> dict[str, Any]:
        return {"backend": self.__class__.__name__}

    def health(self) -> dict[str, Any]:
        return self.retrieval_health()

    def delete(self, memory_id: str) -> bool:
        raise NotImplementedError


class SQLiteMemoryBackend(MemoryBackend):
    def __init__(
        self,
        path: str,
        retrieval: RetrievalConfig | None = None,
        runtime_resolver: RuntimeConfigResolver | None = None,
    ) -> None:
        self.path = Path(path)
        self.retrieval = retrieval or RetrievalConfig()
        self.runtime_resolver = runtime_resolver
        self.embedding_client = EmbeddingClient(self.retrieval.embedding)
        self.rerank_client = RerankClient(self.retrieval.rerank)
        self.last_vector_dimension_mismatches = 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_db(self) -> None:
        with self._connect() as connection:
            for version, name, migration in self._migrations():
                migration(connection)
                connection.execute(
                    """
                    INSERT OR IGNORE INTO schema_migrations(version, name, applied_at)
                    VALUES (?, ?, ?)
                    """,
                    (version, name, datetime.now(timezone.utc).isoformat()),
                )

    def _migrations(self) -> list[tuple[int, str, Any]]:
        return [
            (1, "initial_sqlite_memory_schema", self._migration_initial_schema),
            (2, "webui_memory_lifecycle_and_audit", self._migration_lifecycle_and_audit),
            (3, "webui_api_keys_and_connections", self._migration_api_keys_and_connections),
            (4, "embedding_backfill_state", self._migration_embedding_backfill_state),
        ]

    def _migration_initial_schema(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS memories (
                memory_id TEXT PRIMARY KEY,
                project TEXT NOT NULL,
                topic TEXT NOT NULL,
                memory_type TEXT NOT NULL,
                title TEXT NOT NULL,
                summary TEXT NOT NULL,
                memory_status TEXT NOT NULL DEFAULT 'active',
                status_changed_at TEXT,
                status_change_reason TEXT,
                data TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
                memory_id UNINDEXED,
                project,
                topic,
                memory_type,
                title,
                summary,
                tags,
                entities,
                claims
            )
            """
        )
        self._ensure_column(connection, "memories", "memory_status", "TEXT NOT NULL DEFAULT 'active'")
        self._ensure_column(connection, "memories", "status_changed_at", "TEXT")
        self._ensure_column(connection, "memories", "status_change_reason", "TEXT")

    def _migration_lifecycle_and_audit(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_embeddings (
                memory_id TEXT PRIMARY KEY,
                embedding TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(memory_id) REFERENCES memories(memory_id) ON DELETE CASCADE
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_events (
                event_id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                actor TEXT,
                memory_id TEXT,
                metadata TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )

    def _migration_api_keys_and_connections(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS api_keys (
                key_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                key_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_used_at TEXT,
                status TEXT NOT NULL DEFAULT 'active'
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS active_connections (
                key_id TEXT NOT NULL,
                client_ip TEXT NOT NULL,
                client_info TEXT,
                request_count INTEGER DEFAULT 1,
                last_request_at TEXT NOT NULL,
                PRIMARY KEY (key_id, client_ip),
                FOREIGN KEY(key_id) REFERENCES api_keys(key_id) ON DELETE CASCADE
            )
            """
        )

    def _migration_embedding_backfill_state(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS embedding_backfill_needed (
                memory_id TEXT PRIMARY KEY,
                reason TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(memory_id) REFERENCES memories(memory_id) ON DELETE CASCADE
            )
            """
        )

    def save(self, memory: ResearchMemory) -> ResearchMemory:
        self._refresh_retrieval_clients()
        data = memory.model_dump_json()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO memories(
                    memory_id, project, topic, memory_type, title, summary,
                    memory_status, status_changed_at, status_change_reason, data, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(memory_id) DO UPDATE SET
                    project=excluded.project,
                    topic=excluded.topic,
                    memory_type=excluded.memory_type,
                    title=excluded.title,
                    summary=excluded.summary,
                    memory_status=excluded.memory_status,
                    status_changed_at=excluded.status_changed_at,
                    status_change_reason=excluded.status_change_reason,
                    data=excluded.data,
                    updated_at=excluded.updated_at
                """,
                (
                    memory.memory_id,
                    memory.project,
                    memory.topic,
                    memory.memory_type.value,
                    memory.title,
                    memory.summary,
                    memory.memory_status.value,
                    memory.status_changed_at,
                    memory.status_change_reason,
                    data,
                    memory.created_at,
                    memory.updated_at,
                ),
            )
            self._write_fts(connection, memory)
            if self.embedding_client.enabled:
                embedding = self.embedding_client.embed(memory_to_search_document(memory))
                if embedding:
                    connection.execute(
                        "DELETE FROM memory_embeddings WHERE memory_id = ?", (memory.memory_id,)
                    )
                    connection.execute(
                        """
                        INSERT INTO memory_embeddings(memory_id, embedding, updated_at)
                        VALUES (?, ?, ?)
                        """,
                        (memory.memory_id, json.dumps(embedding), memory.updated_at),
                    )
                    self._clear_backfill_needed(connection, memory.memory_id)
                else:
                    self._mark_backfill_needed(
                        connection,
                        memory.memory_id,
                        getattr(self.embedding_client, "last_error", None) or "embedding_failed",
                        memory.updated_at,
                    )
                    logger.warning(
                        "Embedding generation skipped for memory_id=%s: %s",
                        memory.memory_id,
                        getattr(self.embedding_client, "last_error", None),
                    )
            elif self._has_embedding(connection, memory.memory_id):
                self._mark_backfill_needed(
                    connection,
                    memory.memory_id,
                    "embedding_disabled_after_update",
                    memory.updated_at,
                )
        return memory

    def search(
        self,
        query: str,
        *,
        project: str | None = None,
        memory_type: str | None = None,
        statuses: list[str] | None = None,
        limit: int = 10,
    ) -> list[SearchResult]:
        self._refresh_retrieval_clients()
        safe_limit = max(1, min(limit, 50))
        status_filter = _normalize_statuses(statuses)
        if self.retrieval.mode == "hybrid" and query.strip() and self.embedding_client.enabled:
            return self._search_hybrid(
                query,
                project=project,
                memory_type=memory_type,
                statuses=status_filter,
                limit=safe_limit,
            )

        return self._search_keyword(
            query,
            project=project,
            memory_type=memory_type,
            statuses=status_filter,
            limit=safe_limit,
        )

    def _search_keyword(
        self,
        query: str,
        *,
        project: str | None = None,
        memory_type: str | None = None,
        statuses: list[str] | None = None,
        limit: int = 10,
    ) -> list[SearchResult]:
        safe_limit = max(1, min(limit, 50))
        filters: list[str] = []
        params: list[Any] = []
        if project:
            filters.append("m.project = ?")
            params.append(project)
        if memory_type:
            filters.append("m.memory_type = ?")
            params.append(memory_type)
        filters.extend(_status_sql(statuses, params, alias="m"))

        if query.strip():
            where = ["memories_fts MATCH ?", *filters]
            params = [_to_fts_query(query), *params]
            sql = f"""
                SELECT m.data, bm25(memories_fts) AS rank
                FROM memories_fts
                JOIN memories m ON m.memory_id = memories_fts.memory_id
                WHERE {' AND '.join(where)}
                ORDER BY rank
                LIMIT ?
            """
        else:
            where = filters or ["1 = 1"]
            sql = f"""
                SELECT m.data, 0 AS rank
                FROM memories m
                WHERE {' AND '.join(where)}
                ORDER BY m.updated_at DESC
                LIMIT ?
            """
        params.append(safe_limit)

        with self._connect() as connection:
            try:
                rows = connection.execute(sql, params).fetchall()
            except sqlite3.OperationalError:
                rows = self._search_like(connection, query, filters, params[1:-1], safe_limit)

        results: list[SearchResult] = []
        for row in rows:
            memory = ResearchMemory.model_validate_json(row["data"])
            results.append(SearchResult(memory=memory, score=float(row["rank"]), match_reason="fts"))
        return results

    def _search_hybrid(
        self,
        query: str,
        *,
        project: str | None = None,
        memory_type: str | None = None,
        statuses: list[str] | None = None,
        limit: int = 10,
    ) -> list[SearchResult]:
        candidate_limit = max(limit, min(self.retrieval.rerank_candidate_limit, 50))
        keyword_results = self._search_keyword(
            query,
            project=project,
            memory_type=memory_type,
            statuses=statuses,
            limit=candidate_limit,
        )
        vector_results = self._search_vector(
            query,
            project=project,
            memory_type=memory_type,
            statuses=statuses,
            limit=self.retrieval.vector_candidate_limit,
        )
        merged = self._merge_results(keyword_results, vector_results)
        if not merged:
            return []
        reranked = self._rerank_results(query, merged[: self.retrieval.rerank_candidate_limit], limit)
        if reranked:
            return reranked
        return merged[:limit]

    def _search_vector(
        self,
        query: str,
        *,
        project: str | None = None,
        memory_type: str | None = None,
        statuses: list[str] | None = None,
        limit: int = 50,
    ) -> list[SearchResult]:
        query_embedding = self.embedding_client.embed(query)
        if not query_embedding:
            logger.warning(
                "Vector search unavailable; hybrid retrieval will use SQLite FTS fallback: %s",
                getattr(self.embedding_client, "last_error", None),
            )
            return []

        filters: list[str] = []
        params: list[Any] = []
        if project:
            filters.append("m.project = ?")
            params.append(project)
        if memory_type:
            filters.append("m.memory_type = ?")
            params.append(memory_type)
        filters.extend(_status_sql(statuses, params, alias="m"))

        where = filters or ["1 = 1"]
        sql = f"""
            SELECT m.data, e.embedding
            FROM memory_embeddings e
            JOIN memories m ON m.memory_id = e.memory_id
            WHERE {' AND '.join(where)}
        """

        results: list[SearchResult] = []
        dimension_mismatches = 0
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        for row in rows:
            try:
                embedding = [float(value) for value in json.loads(row["embedding"])]
            except (TypeError, ValueError, json.JSONDecodeError):
                logger.warning("Skipping invalid stored embedding payload")
                continue
            if len(query_embedding) != len(embedding):
                dimension_mismatches += 1
                continue
            score = cosine_similarity(query_embedding, embedding)
            if score <= 0:
                continue
            memory = ResearchMemory.model_validate_json(row["data"])
            results.append(
                SearchResult(memory=memory, score=score, match_reason=f"vector:cosine={score:.4f}")
            )

        self.last_vector_dimension_mismatches = dimension_mismatches
        if dimension_mismatches:
            logger.warning(
                "Skipped %s stored embedding(s) due to vector dimension mismatch",
                dimension_mismatches,
            )

        results.sort(key=lambda item: item.score, reverse=True)
        return results[: max(1, min(limit, 100))]

    def _merge_results(
        self,
        keyword_results: list[SearchResult],
        vector_results: list[SearchResult],
    ) -> list[SearchResult]:
        merged: dict[str, SearchResult] = {}
        for index, result in enumerate(keyword_results):
            # FTS bm25 uses lower-is-better rank, so convert position into a positive merge signal.
            fts_score = 1.0 / (index + 1)
            merged[result.memory.memory_id] = SearchResult(
                memory=result.memory,
                score=fts_score,
                match_reason=f"fts:rank_position={index + 1}",
            )
        for result in vector_results:
            existing = merged.get(result.memory.memory_id)
            if existing is None:
                merged[result.memory.memory_id] = result
                continue
            existing.score += result.score
            existing.match_reason = f"{existing.match_reason}+{result.match_reason}"
        return sorted(merged.values(), key=lambda item: item.score, reverse=True)

    def _rerank_results(
        self,
        query: str,
        candidates: list[SearchResult],
        limit: int,
    ) -> list[SearchResult]:
        if not self.rerank_client.enabled or not candidates:
            return []
        documents = [memory_to_search_document(candidate.memory) for candidate in candidates]
        rerank_scores = self.rerank_client.rerank(query, documents, top_n=limit)
        if not rerank_scores:
            logger.warning(
                "Rerank unavailable; returning un-reranked hybrid results: %s",
                getattr(self.rerank_client, "last_error", None),
            )
            return []
        reranked: list[SearchResult] = []
        for index, score in rerank_scores:
            if index < 0 or index >= len(candidates):
                continue
            candidate = candidates[index]
            reranked.append(
                SearchResult(
                    memory=candidate.memory,
                    score=score,
                    match_reason=f"{candidate.match_reason}+rerank:score={score:.4f}",
                )
            )
        return reranked[:limit]

    def retrieval_health(self) -> dict[str, Any]:
        vector_count = 0
        dimension_counts: dict[int, int] = {}
        invalid_vectors = 0
        with self._connect() as connection:
            rows = connection.execute("SELECT embedding FROM memory_embeddings").fetchall()
        for row in rows:
            try:
                embedding = json.loads(row["embedding"])
                if not isinstance(embedding, list):
                    raise TypeError
                dimension_counts[len(embedding)] = dimension_counts.get(len(embedding), 0) + 1
                vector_count += 1
            except (TypeError, json.JSONDecodeError):
                invalid_vectors += 1
        return {
            "backend": "sqlite",
            "retrieval_mode": self.retrieval.mode,
            "sqlite_path": str(self.path),
            "embedding": _client_health(self.embedding_client),
            "rerank": _client_health(self.rerank_client),
            "stored_embedding_count": vector_count,
            "stored_embedding_dimensions": dimension_counts,
            "invalid_stored_embeddings": invalid_vectors,
            "last_vector_dimension_mismatches": self.last_vector_dimension_mismatches,
        }

    def health(self) -> dict[str, Any]:
        writable = False
        error: str | None = None
        try:
            with self._connect() as connection:
                connection.execute("PRAGMA user_version")
                connection.execute("CREATE TABLE IF NOT EXISTS _healthcheck(value INTEGER)")
                connection.execute("DELETE FROM _healthcheck")
                connection.execute("INSERT INTO _healthcheck(value) VALUES (1)")
                connection.execute("DROP TABLE _healthcheck")
            writable = True
        except sqlite3.Error as exc:
            error = exc.__class__.__name__
        return {**self.retrieval_health(), "sqlite_writable": writable, "sqlite_error": error}

    def _search_like(
        self,
        connection: sqlite3.Connection,
        query: str,
        filters: list[str],
        filter_params: list[Any],
        limit: int,
    ) -> list[sqlite3.Row]:
        like_query = f"%{query}%"
        where = ["(m.title LIKE ? OR m.summary LIKE ? OR m.topic LIKE ?)", *filters]
        params: list[Any] = [like_query, like_query, like_query, *filter_params, limit]
        sql = f"""
            SELECT m.data, 0 AS rank
            FROM memories m
            WHERE {' AND '.join(where)}
            ORDER BY m.updated_at DESC
            LIMIT ?
        """
        return connection.execute(sql, params).fetchall()

    def list_all(self, *, statuses: list[str] | None = None) -> list[ResearchMemory]:
        params: list[Any] = []
        filters = _status_sql(_normalize_statuses(statuses), params)
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        with self._connect() as connection:
            rows = connection.execute(f"SELECT data FROM memories {where} ORDER BY updated_at DESC", params).fetchall()
        return [ResearchMemory.model_validate_json(row["data"]) for row in rows]

    def get(self, memory_id: str) -> ResearchMemory | None:
        with self._connect() as connection:
            row = connection.execute("SELECT data FROM memories WHERE memory_id = ?", (memory_id,)).fetchone()
        if row is None:
            return None
        return ResearchMemory.model_validate_json(row["data"])

    def delete(self, memory_id: str) -> bool:
        with self._connect() as connection:
            connection.execute("DELETE FROM memories_fts WHERE memory_id = ?", (memory_id,))
            connection.execute("DELETE FROM memory_embeddings WHERE memory_id = ?", (memory_id,))
            cursor = connection.execute("DELETE FROM memories WHERE memory_id = ?", (memory_id,))
            connection.execute("DELETE FROM embedding_backfill_needed WHERE memory_id = ?", (memory_id,))
            return cursor.rowcount > 0

    def append_audit_event(
        self,
        event_type: str,
        *,
        actor: str | None = "webui",
        memory_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        event = {
            "event_id": f"audit_{datetime.now(timezone.utc).timestamp():.6f}_{os.urandom(4).hex()}",
            "event_type": event_type,
            "actor": actor,
            "memory_id": memory_id,
            "metadata": metadata or {},
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO audit_events(event_id, event_type, actor, memory_id, metadata, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    event["event_id"],
                    event_type,
                    actor,
                    memory_id,
                    json.dumps(_sanitize_metadata(event["metadata"]), ensure_ascii=False),
                    event["created_at"],
                ),
            )
        return event

    def list_audit_events(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM audit_events ORDER BY created_at DESC LIMIT ?", (max(1, min(limit, 500)),)
            ).fetchall()
        return [
            {
                "event_id": row["event_id"],
                "event_type": row["event_type"],
                "actor": row["actor"],
                "memory_id": row["memory_id"],
                "metadata": json.loads(row["metadata"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def audit_integrity(self, *, repair_fts: bool = False, repair_orphans: bool = False) -> dict[str, Any]:
        report: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "missing_fts": [],
            "orphan_embeddings": [],
            "embedding_backfill_needed": [],
            "invalid_memory_json": [],
            "invalid_source_refs": [],
            "repaired_fts": 0,
            "removed_orphan_embeddings": 0,
        }
        with self._connect() as connection:
            memory_rows = connection.execute("SELECT memory_id, data FROM memories").fetchall()
            fts_ids = {
                row["memory_id"]
                for row in connection.execute("SELECT memory_id FROM memories_fts").fetchall()
            }
            memory_ids = {row["memory_id"] for row in memory_rows}
            embedding_ids = {
                row["memory_id"]
                for row in connection.execute("SELECT memory_id FROM memory_embeddings").fetchall()
            }
            report["orphan_embeddings"] = sorted(embedding_ids - memory_ids)
            report["embedding_backfill_needed"] = [
                dict(row)
                for row in connection.execute(
                    "SELECT memory_id, reason, updated_at FROM embedding_backfill_needed ORDER BY memory_id"
                ).fetchall()
            ]
            if repair_orphans and report["orphan_embeddings"]:
                for memory_id in report["orphan_embeddings"]:
                    connection.execute("DELETE FROM memory_embeddings WHERE memory_id = ?", (memory_id,))
                report["removed_orphan_embeddings"] = len(report["orphan_embeddings"])

            for row in memory_rows:
                try:
                    memory = ResearchMemory.model_validate_json(row["data"])
                except ValueError:
                    report["invalid_memory_json"].append(row["memory_id"])
                    continue
                if row["memory_id"] not in fts_ids:
                    report["missing_fts"].append(row["memory_id"])
                    if repair_fts:
                        self._write_fts(connection, memory)
                        report["repaired_fts"] += 1
                for source_ref in memory.source_refs:
                    if not any([source_ref.path, source_ref.url, source_ref.doi, source_ref.source_id]):
                        report["invalid_source_refs"].append(
                            {"memory_id": memory.memory_id, "source_type": source_ref.source_type}
                        )
        return report

    def _write_fts(self, connection: sqlite3.Connection, memory: ResearchMemory) -> None:
        tags = " ".join(memory.tags)
        entities = " ".join(entity.name for entity in memory.entities)
        claims = " ".join(claim.claim for claim in memory.claims)
        connection.execute("DELETE FROM memories_fts WHERE memory_id = ?", (memory.memory_id,))
        connection.execute(
            """
            INSERT INTO memories_fts(memory_id, project, topic, memory_type, title, summary, tags, entities, claims)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                memory.memory_id,
                memory.project,
                memory.topic,
                memory.memory_type.value,
                memory.title,
                memory.summary,
                tags,
                entities,
                claims,
            ),
        )

    def _ensure_column(self, connection: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def _has_embedding(self, connection: sqlite3.Connection, memory_id: str) -> bool:
        row = connection.execute(
            "SELECT 1 FROM memory_embeddings WHERE memory_id = ?",
            (memory_id,),
        ).fetchone()
        return row is not None

    def _mark_backfill_needed(
        self,
        connection: sqlite3.Connection,
        memory_id: str,
        reason: str,
        updated_at: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO embedding_backfill_needed(memory_id, reason, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(memory_id) DO UPDATE SET
                reason=excluded.reason,
                updated_at=excluded.updated_at
            """,
            (memory_id, reason, updated_at),
        )

    def _clear_backfill_needed(self, connection: sqlite3.Connection, memory_id: str) -> None:
        connection.execute("DELETE FROM embedding_backfill_needed WHERE memory_id = ?", (memory_id,))

    def _refresh_retrieval_clients(self) -> None:
        if self.runtime_resolver is None:
            return
        previous = self.retrieval.model_dump(mode="json")
        self.retrieval = self.runtime_resolver.retrieval_config()
        current = self.retrieval.model_dump(mode="json")
        if current != previous:
            self.embedding_client = EmbeddingClient(self.retrieval.embedding)
            self.rerank_client = RerankClient(self.retrieval.rerank)


class NocturneMemoryBackend(MemoryBackend):
    """Thin placeholder adapter for a remote Nocturne service.

    Nocturne exposes MCP tools rather than a stable project-specific REST API. This adapter keeps the
    gateway boundary explicit. Use SQLite for smoke tests, then map these methods to your deployed
    Nocturne endpoint once the final transport/tool contract is selected.
    """

    def __init__(self, url: str, token: str | None = None) -> None:
        if not url:
            raise ValueError("Nocturne backend selected but URL is empty")
        self.url = url.rstrip("/")
        self.token = token

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def save(self, memory: ResearchMemory) -> ResearchMemory:
        raise NotImplementedError(
            "Nocturne adapter requires mapping to your deployed Nocturne MCP/HTTP contract. "
            "Use backend.type=sqlite for local validation."
        )

    def search(
        self,
        query: str,
        *,
        project: str | None = None,
        memory_type: str | None = None,
        statuses: list[str] | None = None,
        limit: int = 10,
    ) -> list[SearchResult]:
        raise NotImplementedError(
            "Nocturne adapter requires mapping to your deployed Nocturne MCP/HTTP contract. "
            "Use backend.type=sqlite for local validation."
        )

    def list_all(self, *, statuses: list[str] | None = None) -> list[ResearchMemory]:
        raise NotImplementedError("Nocturne adapter is not yet mapped to a concrete endpoint")

    def get(self, memory_id: str) -> ResearchMemory | None:
        raise NotImplementedError("Nocturne adapter is not yet mapped to a concrete endpoint")

    def delete(self, memory_id: str) -> bool:
        raise NotImplementedError("Nocturne adapter is not yet mapped to a concrete endpoint")

    def health(self) -> dict[str, Any]:
        with httpx.Client(timeout=10) as client:
            response = client.get(f"{self.url}/health", headers=self._headers())
            return {"status_code": response.status_code, "text": response.text[:500]}


def build_backend(config: AppConfig) -> MemoryBackend:
    if config.backend.type == "sqlite":
        resolver = RuntimeConfigResolver(config) if config.webui.enabled else None
        return SQLiteMemoryBackend(config.backend.sqlite_path, config.retrieval, resolver)

    url = os.getenv(config.backend.nocturne_url_env, "")
    token = os.getenv(config.backend.nocturne_token_env)
    return NocturneMemoryBackend(url=url, token=token)


def memory_to_search_document(memory: ResearchMemory) -> str:
    return json.dumps(memory.model_dump(), ensure_ascii=False, indent=2)


def _client_health(client: Any) -> dict[str, Any]:
    if hasattr(client, "health"):
        return client.health()
    return {
        "enabled": bool(getattr(client, "enabled", False)),
        "status": "custom_client",
        "last_error": getattr(client, "last_error", None),
    }


def _to_fts_query(query: str) -> str:
    tokens = re.findall(r"[\w\u4e00-\u9fff]+", query, flags=re.UNICODE)
    tokens = [token for token in tokens if token.strip()]
    if not tokens:
        return '""'
    return " OR ".join(f'"{token}"' for token in tokens[:20])


def _normalize_statuses(statuses: list[str] | None) -> list[str]:
    if statuses is None:
        return list(ACTIVE_STATUSES)
    parsed = [MemoryStatus(status).value for status in statuses]
    return parsed or list(ACTIVE_STATUSES)


def _status_sql(statuses: list[str] | None, params: list[Any], alias: str | None = None) -> list[str]:
    if not statuses:
        return []
    column = f"{alias}.memory_status" if alias else "memory_status"
    placeholders = ", ".join("?" for _ in statuses)
    params.extend(statuses)
    return [f"{column} IN ({placeholders})"]


def _sanitize_metadata(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if any(marker in lowered for marker in ("secret", "token", "api_key", "password", "authorization")):
                sanitized[key] = "[redacted]"
            else:
                sanitized[key] = _sanitize_metadata(item)
        return sanitized
    if isinstance(value, list):
        return [_sanitize_metadata(item) for item in value]
    return value
