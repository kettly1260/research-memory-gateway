from __future__ import annotations

import json
import os
import re
import sqlite3
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import httpx

from .config import AppConfig
from .models import ResearchMemory, SearchResult


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
        limit: int = 10,
    ) -> list[SearchResult]:
        raise NotImplementedError

    @abstractmethod
    def list_all(self) -> list[ResearchMemory]:
        raise NotImplementedError

    @abstractmethod
    def get(self, memory_id: str) -> ResearchMemory | None:
        raise NotImplementedError


class SQLiteMemoryBackend(MemoryBackend):
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_db(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    memory_id TEXT PRIMARY KEY,
                    project TEXT NOT NULL,
                    topic TEXT NOT NULL,
                    memory_type TEXT NOT NULL,
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL,
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

    def save(self, memory: ResearchMemory) -> ResearchMemory:
        data = memory.model_dump_json()
        tags = " ".join(memory.tags)
        entities = " ".join(entity.name for entity in memory.entities)
        claims = " ".join(claim.claim for claim in memory.claims)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO memories(memory_id, project, topic, memory_type, title, summary, data, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(memory_id) DO UPDATE SET
                    project=excluded.project,
                    topic=excluded.topic,
                    memory_type=excluded.memory_type,
                    title=excluded.title,
                    summary=excluded.summary,
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
                    data,
                    memory.created_at,
                    memory.updated_at,
                ),
            )
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
        return memory

    def search(
        self,
        query: str,
        *,
        project: str | None = None,
        memory_type: str | None = None,
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

    def list_all(self) -> list[ResearchMemory]:
        with self._connect() as connection:
            rows = connection.execute("SELECT data FROM memories ORDER BY updated_at DESC").fetchall()
        return [ResearchMemory.model_validate_json(row["data"]) for row in rows]

    def get(self, memory_id: str) -> ResearchMemory | None:
        with self._connect() as connection:
            row = connection.execute("SELECT data FROM memories WHERE memory_id = ?", (memory_id,)).fetchone()
        if row is None:
            return None
        return ResearchMemory.model_validate_json(row["data"])


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
        limit: int = 10,
    ) -> list[SearchResult]:
        raise NotImplementedError(
            "Nocturne adapter requires mapping to your deployed Nocturne MCP/HTTP contract. "
            "Use backend.type=sqlite for local validation."
        )

    def list_all(self) -> list[ResearchMemory]:
        raise NotImplementedError("Nocturne adapter is not yet mapped to a concrete endpoint")

    def get(self, memory_id: str) -> ResearchMemory | None:
        raise NotImplementedError("Nocturne adapter is not yet mapped to a concrete endpoint")

    def health(self) -> dict[str, Any]:
        with httpx.Client(timeout=10) as client:
            response = client.get(f"{self.url}/health", headers=self._headers())
            return {"status_code": response.status_code, "text": response.text[:500]}


def build_backend(config: AppConfig) -> MemoryBackend:
    if config.backend.type == "sqlite":
        return SQLiteMemoryBackend(config.backend.sqlite_path)

    url = os.getenv(config.backend.nocturne_url_env, "")
    token = os.getenv(config.backend.nocturne_token_env)
    return NocturneMemoryBackend(url=url, token=token)


def memory_to_search_document(memory: ResearchMemory) -> str:
    return json.dumps(memory.model_dump(), ensure_ascii=False, indent=2)


def _to_fts_query(query: str) -> str:
    tokens = re.findall(r"[\w\u4e00-\u9fff]+", query, flags=re.UNICODE)
    tokens = [token for token in tokens if token.strip()]
    if not tokens:
        return '""'
    return " OR ".join(f'"{token}"' for token in tokens[:20])
