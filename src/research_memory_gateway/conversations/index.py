from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import sqlite3
import struct
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator, Sequence

from research_memory_gateway.retrieval import EmbeddingClient, cosine_similarity
from .chunking import ConversationChunk, HeadingChunker


@dataclass
class SearchResult:
    section_id: str
    conversation_id: str
    vault_path: str
    heading_path: list[str]
    title: str
    content: str
    score: float
    score_type: str  # "lexical" | "vector" | "hybrid"
    date: str = ""
    projects: list[str] = field(default_factory=list)
    source_anchors: list[dict[str, str]] = field(default_factory=list)
    parent_thread_id: str = ""
    thread_source: str = ""
    source_system: str = "codex"
    source_originator: str = ""
    source_surface: str = ""
    source_version: str = ""
    model_provider: str = ""
    model_name: str = ""
    agent_path: str = ""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pack_vector(vector: Sequence[float]) -> bytes:
    return struct.pack(f"{len(vector)}f", *vector)


def _unpack_vector(blob: bytes) -> list[float]:
    count = len(blob) // 4
    return list(struct.unpack(f"{count}f", blob))


def compute_index_input_hash(path: str | Path) -> str:
    """Hash the complete searchable Markdown input.

    Import conflict detection deliberately uses the machine-managed hash, while
    the conversation search index includes the manual region as searchable
    knowledge.  Keeping a separate full-file hash lets manual edits trigger
    reindexing without becoming ingestion conflicts.
    """
    target = Path(path)
    digest = hashlib.sha256()
    with target.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class ConversationIndexDatabase:
    def __init__(
        self,
        db_path: str | Path,
        embedding_client: EmbeddingClient | None = None,
        embedding_version: str = "v1",
    ) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.embedding_client = embedding_client
        self.embedding_version = embedding_version
        self.chunker = HeadingChunker()
        self.stats_new_embeddings: int = 0
        self.stats_cache_reused: int = 0
        self.stats_failures: int = 0
        self.stats_dimension_mismatches: int = 0
        self._init_db()

    def reset_embedding_stats(self) -> None:
        self.stats_new_embeddings = 0
        self.stats_cache_reused = 0
        self.stats_failures = 0
        self.stats_dimension_mismatches = 0

    @property
    def embedding_stats(self) -> dict[str, int]:
        return {
            "new_embedding_requests": self.stats_new_embeddings,
            "cache_reused": self.stats_cache_reused,
            "failures": self.stats_failures,
            "dimension_mismatches": self.stats_dimension_mismatches,
        }

    def stats(self) -> dict[str, Any]:
        with self._connect() as conn:
            docs_count = conn.execute("SELECT COUNT(*) FROM conversation_documents").fetchone()[0]
            sections_count = conn.execute("SELECT COUNT(*) FROM conversation_sections").fetchone()[0]
            embeddings_count = conn.execute("SELECT COUNT(*) FROM conversation_embeddings").fetchone()[0]

            is_embedding_enabled = bool(self.embedding_client and getattr(self.embedding_client, "enabled", False))
            active_model = self.embedding_client.model if is_embedding_enabled else None
            active_version = self.embedding_version if is_embedding_enabled else None
            active_dim = getattr(self.embedding_client, "dimension", None) if is_embedding_enabled else None

            if is_embedding_enabled and active_model:
                if active_dim is None:
                    row = conn.execute(
                        "SELECT dimension FROM conversation_embeddings WHERE model = ? AND version = ? LIMIT 1",
                        [active_model, active_version],
                    ).fetchone()
                    if row:
                        active_dim = row[0]
                covered_row = conn.execute(
                    """
                    SELECT COUNT(s.id) FROM conversation_sections s
                    WHERE s.embedding_identity IS NOT NULL
                      AND s.embedding_identity != ''
                      AND EXISTS (
                          SELECT 1 FROM conversation_embeddings e
                          WHERE e.embedding_identity = s.embedding_identity
                            AND e.model = ? AND e.version = ?
                      )
                    """,
                    [active_model, active_version],
                ).fetchone()
                sections_with_emb = covered_row[0] if covered_row else 0
            else:
                sections_with_emb = 0

            sections_without_emb = max(0, sections_count - sections_with_emb)
            vector_coverage = round(sections_with_emb / sections_count, 4) if sections_count > 0 else (1.0 if is_embedding_enabled else 0.0)

            source_system_distribution: dict[str, int] = {}
            for row in conn.execute(
                "SELECT COALESCE(NULLIF(source_system, ''), 'codex'), COUNT(*) FROM conversation_documents GROUP BY 1"
            ).fetchall():
                source_system_distribution[row[0]] = row[1]

            thread_source_distribution: dict[str, int] = {}
            for row in conn.execute(
                "SELECT COALESCE(NULLIF(thread_source, ''), 'unknown'), COUNT(*) FROM conversation_documents GROUP BY 1"
            ).fetchall():
                thread_source_distribution[row[0]] = row[1]

            return {
                "documents": docs_count,
                "sections": sections_count,
                "embeddings": embeddings_count,
                "sections_with_embedding": sections_with_emb,
                "sections_without_embedding": sections_without_emb,
                "vector_coverage": vector_coverage,
                "embedding_model": active_model,
                "embedding_version": active_version,
                "embedding_dimension": active_dim,
                "source_system_distribution": source_system_distribution,
                "thread_source_distribution": thread_source_distribution,
            }

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_documents (
                    id TEXT PRIMARY KEY,
                    vault_path TEXT NOT NULL,
                    title TEXT,
                    file_hash TEXT NOT NULL,
                    created_date TEXT,
                    projects_json TEXT,
                    topics_json TEXT,
                    parent_thread_id TEXT,
                    thread_source TEXT,
                    source_system TEXT,
                    source_originator TEXT,
                    source_surface TEXT,
                    source_version TEXT,
                    model_provider TEXT,
                    model_name TEXT,
                    agent_path TEXT,
                    indexed_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_sections (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    vault_path TEXT NOT NULL,
                    heading_path TEXT,
                    title TEXT,
                    content TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    chunk_index INTEGER NOT NULL,
                    source_anchors_json TEXT,
                    projects_json TEXT,
                    date TEXT,
                    embedding_identity TEXT,
                    parent_thread_id TEXT,
                    thread_source TEXT,
                    source_system TEXT,
                    source_originator TEXT,
                    source_surface TEXT,
                    source_version TEXT,
                    model_provider TEXT,
                    model_name TEXT,
                    agent_path TEXT,
                    indexed_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS conversation_sections_fts USING fts5(
                    id UNINDEXED,
                    conversation_id,
                    parent_thread_id,
                    heading_path,
                    content,
                    tokenize='unicode61'
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_embeddings (
                    id TEXT NOT NULL,
                    embedding_identity TEXT NOT NULL,
                    model TEXT NOT NULL,
                    version TEXT NOT NULL,
                    dimension INTEGER NOT NULL,
                    vector BLOB NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(embedding_identity, model, version)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_index_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_at TEXT NOT NULL,
                    indexed_files INTEGER NOT NULL,
                    skipped_files INTEGER NOT NULL DEFAULT 0,
                    total_chunks INTEGER NOT NULL,
                    embedded_chunks INTEGER NOT NULL,
                    cache_reused_chunks INTEGER NOT NULL DEFAULT 0,
                    failed_chunks INTEGER NOT NULL DEFAULT 0,
                    model TEXT,
                    status TEXT,
                    error TEXT
                )
                """
            )
            _migrate_index_db(conn)

    def index_file(
        self,
        path: str | Path,
        *,
        dry_run: bool = False,
    ) -> int:
        target = Path(path)
        if not target.exists():
            raise FileNotFoundError(f"Markdown file not found: {target}")

        model_name = self.embedding_client.model if (self.embedding_client and self.embedding_client.enabled) else ""
        frontmatter, chunks = self.chunker.chunk_file(
            target,
            embedding_model=model_name,
            embedding_version=self.embedding_version,
        )
        if not chunks:
            return 0

        if dry_run:
            return len(chunks)

        conv_id = str(frontmatter.get("conversation_id") or chunks[0].conversation_id)
        parent_thread_id = str(frontmatter.get("parent_thread_id") or "")
        thread_source = str(frontmatter.get("thread_source") or "")
        source_system = str(frontmatter.get("source_system") or (frontmatter.get("source") if frontmatter.get("source") == "codex" else "") or "codex")
        source_originator = str(frontmatter.get("source_originator") or "")
        source_surface = str(frontmatter.get("source_surface") or "")
        source_version = str(frontmatter.get("source_version") or "")
        model_provider = str(frontmatter.get("model_provider") or "")
        model_name = str(frontmatter.get("model_name") or "")
        agent_path = str(frontmatter.get("agent_path") or "")
        embedding_model_name = self.embedding_client.model if self.embedding_client else None
        file_hash = compute_index_input_hash(target)
        now = _utc_now()

        with self._connect() as conn:
            # 1. 记录 document
            conn.execute(
                """
                INSERT INTO conversation_documents(
                    id, vault_path, title, file_hash, created_date,
                    projects_json, topics_json, parent_thread_id, thread_source,
                    source_system, source_originator, source_surface, source_version,
                    model_provider, model_name, agent_path, indexed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    vault_path=excluded.vault_path,
                    title=excluded.title,
                    file_hash=excluded.file_hash,
                    created_date=excluded.created_date,
                    projects_json=excluded.projects_json,
                    topics_json=excluded.topics_json,
                    parent_thread_id=excluded.parent_thread_id,
                    thread_source=excluded.thread_source,
                    source_system=excluded.source_system,
                    source_originator=excluded.source_originator,
                    source_surface=excluded.source_surface,
                    source_version=excluded.source_version,
                    model_provider=excluded.model_provider,
                    model_name=excluded.model_name,
                    agent_path=excluded.agent_path,
                    indexed_at=excluded.indexed_at
                """,
                (
                    conv_id,
                    str(target.resolve()),
                    chunks[0].title,
                    file_hash,
                    str(frontmatter.get("created") or ""),
                    json.dumps(frontmatter.get("projects") or []),
                    json.dumps(frontmatter.get("topics") or []),
                    parent_thread_id,
                    thread_source,
                    source_system,
                    source_originator,
                    source_surface,
                    source_version,
                    model_provider,
                    model_name,
                    agent_path,
                    now,
                ),
            )

            # 2. 删除旧的 sections & fts
            conn.execute("DELETE FROM conversation_sections WHERE conversation_id = ?", (conv_id,))
            conn.execute("DELETE FROM conversation_sections_fts WHERE conversation_id = ?", (conv_id,))

            # 3. 写入新的 sections & fts
            for chunk in chunks:
                heading_str = " > ".join(chunk.heading_path)
                conn.execute(
                    """
                    INSERT INTO conversation_sections(
                        id, conversation_id, vault_path, heading_path, title,
                        content, content_hash, chunk_index, source_anchors_json,
                        projects_json, date, embedding_identity, parent_thread_id, thread_source,
                        source_system, source_originator, source_surface, source_version,
                        model_provider, model_name, agent_path, indexed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        chunk.chunk_id,
                        chunk.conversation_id,
                        chunk.vault_path,
                        heading_str,
                        chunk.title,
                        chunk.content,
                        chunk.content_hash,
                        chunk.chunk_index,
                        json.dumps(chunk.source_anchors),
                        json.dumps(chunk.projects),
                        chunk.date,
                        chunk.embedding_identity,
                        parent_thread_id,
                        thread_source,
                        source_system,
                        source_originator,
                        source_surface,
                        source_version,
                        model_provider,
                        model_name,
                        agent_path,
                        now,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO conversation_sections_fts(id, conversation_id, parent_thread_id, heading_path, content)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (chunk.chunk_id, chunk.conversation_id, parent_thread_id, heading_str, chunk.content),
                )

                # 4. 向量 embedding 处理（仅在启用且 identity 未命中时调用）
                if self.embedding_client and self.embedding_client.enabled and embedding_model_name:
                    self._ensure_embedding(conn, chunk, embedding_model_name)

        return len(chunks)

    def check_changed_reason(self, path: str | Path) -> str:
        target = Path(path)
        if not target.exists():
            return "missing_file"
        file_hash = compute_index_input_hash(target)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT file_hash FROM conversation_documents WHERE vault_path = ?",
                (str(target.resolve()),),
            ).fetchone()
            if row is None:
                # 尝试从 frontmatter 读取 conversation_id 匹配
                try:
                    text = target.read_text(encoding="utf-8")
                    fm, _ = self.chunker.chunk_text(text)
                    cid = fm.get("conversation_id")
                    if cid:
                        row = conn.execute(
                            "SELECT file_hash FROM conversation_documents WHERE id = ?",
                            (cid,),
                        ).fetchone()
                except Exception:
                    pass
            if row is None:
                return "new_document"
            if row["file_hash"] != file_hash:
                return "changed_file"

            # Historical cache rows and the active/live section identity are
            # distinct.  A cached vector is reusable, but the section itself
            # must still be activated for the currently selected model/version.
            if self.embedding_client and self.embedding_client.enabled:
                model_name = self.embedding_client.model
                _, expected_chunks = self.chunker.chunk_file(
                    target,
                    embedding_model=model_name,
                    embedding_version=self.embedding_version,
                )
                for chunk in expected_chunks:
                    live = conn.execute(
                        "SELECT embedding_identity FROM conversation_sections WHERE id = ?",
                        (chunk.chunk_id,),
                    ).fetchone()
                    cache = conn.execute(
                        """
                        SELECT 1 FROM conversation_embeddings
                        WHERE embedding_identity = ? AND model = ? AND version = ?
                        LIMIT 1
                        """,
                        (chunk.embedding_identity, model_name, self.embedding_version),
                    ).fetchone()
                    if live is None:
                        return "changed_file"
                    if live["embedding_identity"] != chunk.embedding_identity:
                        return (
                            "embedding_activation_changed"
                            if cache is not None
                            else "embedding_identity_changed"
                        )
                    if cache is None:
                        return "embedding_identity_changed"

            return "unchanged"

    def check_changed(self, path: str | Path) -> bool:
        return self.check_changed_reason(path) != "unchanged"

    def record_index_run(
        self,
        *,
        run_at: str,
        indexed_files: int,
        skipped_files: int = 0,
        total_chunks: int,
        embedded_chunks: int,
        cache_reused_chunks: int = 0,
        failed_chunks: int = 0,
        model: str = "",
        status: str = "success",
        error: str = "",
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO conversation_index_runs(
                    run_at, indexed_files, skipped_files, total_chunks, embedded_chunks,
                    cache_reused_chunks, failed_chunks, model, status, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_at,
                    indexed_files,
                    skipped_files,
                    total_chunks,
                    embedded_chunks,
                    cache_reused_chunks,
                    failed_chunks,
                    model,
                    status,
                    error,
                ),
            )

    def index_metadata_for_file(self, path: str | Path) -> dict[str, Any]:
        target = Path(path).resolve()
        with self._connect() as conn:
            doc = conn.execute(
                "SELECT id, file_hash, indexed_at FROM conversation_documents WHERE vault_path = ?",
                (str(target),),
            ).fetchone()
            if doc is None:
                return {}
            sections = conn.execute(
                "SELECT content_hash FROM conversation_sections WHERE conversation_id = ? ORDER BY chunk_index",
                (doc["id"],),
            ).fetchall()
            emb = None
            if self.embedding_client and self.embedding_client.enabled and self.embedding_client.model:
                model_name = self.embedding_client.model
                active_rows = conn.execute(
                    """
                    SELECT DISTINCT e.model, e.version, e.dimension
                    FROM conversation_sections s
                    JOIN conversation_embeddings e
                      ON e.embedding_identity = s.embedding_identity
                     AND e.model = ?
                     AND e.version = ?
                    WHERE s.conversation_id = ?
                    """,
                    (model_name, self.embedding_version, doc["id"]),
                ).fetchall()
                section_count = conn.execute(
                    "SELECT COUNT(*) AS n FROM conversation_sections WHERE conversation_id = ?",
                    (doc["id"],),
                ).fetchone()["n"]
                active_count = conn.execute(
                    """
                    SELECT COUNT(*) AS n
                    FROM conversation_sections s
                    WHERE s.conversation_id = ?
                      AND EXISTS (
                        SELECT 1 FROM conversation_embeddings e
                        WHERE e.embedding_identity = s.embedding_identity
                          AND e.model = ? AND e.version = ?
                      )
                    """,
                    (doc["id"], model_name, self.embedding_version),
                ).fetchone()["n"]
                if active_count != section_count:
                    raise ValueError(
                        f"Incomplete active embedding metadata for {doc['id']}: "
                        f"{active_count}/{section_count} sections"
                    )
                if len(active_rows) != 1:
                    raise ValueError(
                        f"Mixed active embedding metadata for {doc['id']}: {len(active_rows)} variants"
                    )
                emb = active_rows[0]
        return {
            "conversation_id": doc["id"],
            "index_source_hash": doc["file_hash"],
            "last_indexed_at": doc["indexed_at"],
            "content_section_hashes": [row["content_hash"] for row in sections],
            "embedding_model": emb["model"] if emb else "",
            "embedding_version": emb["version"] if emb else "",
            "embedding_dimension": emb["dimension"] if emb else None,
        }

    def _ensure_embedding(self, conn: sqlite3.Connection, chunk: ConversationChunk, model_name: str) -> None:
        # 检查是否已存在具有相同 identity 的 vector
        existing = conn.execute(
            """SELECT dimension, vector FROM conversation_embeddings
               WHERE embedding_identity = ? AND model = ? AND version = ? LIMIT 1""",
            (chunk.embedding_identity, model_name, self.embedding_version),
        ).fetchone()
        if existing is not None:
            self.stats_cache_reused += 1
            return

        # 发起 embedding 请求
        try:
            vec = self.embedding_client.embed(chunk.content)
        except Exception:
            self.stats_failures += 1
            raise
        if not vec:
            self.stats_failures += 1
            return

        # 检查模型维度一致性
        dim_row = conn.execute(
            "SELECT dimension FROM conversation_embeddings WHERE model = ? LIMIT 1",
            (model_name,),
        ).fetchone()
        if dim_row is not None and dim_row["dimension"] != len(vec):
            self.stats_dimension_mismatches += 1
            raise ValueError(
                f"Embedding dimension mismatch for model {model_name}: expected {dim_row['dimension']}, got {len(vec)}"
            )

        packed = _pack_vector(vec)
        conn.execute(
            """
            INSERT OR IGNORE INTO conversation_embeddings(
                id, embedding_identity, model, version, dimension, vector, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chunk.chunk_id,
                chunk.embedding_identity,
                model_name,
                self.embedding_version,
                len(vec),
                packed,
                _utc_now(),
            ),
        )
        self.stats_new_embeddings += 1

    def search_fts(
        self,
        query: str,
        *,
        limit: int = 10,
        conversation_id: str | None = None,
        parent_thread_id: str | None = None,
        source_system: str | None = None,
        thread_source: str | None = None,
    ) -> list[SearchResult]:
        clean_q = query.strip()
        if not clean_q:
            return []

        # 转义或格式化 FTS 查询
        fts_expr = _format_fts_query(clean_q)

        sql = """
            SELECT s.id, s.conversation_id, s.vault_path, s.heading_path, s.title,
                   s.content, s.date, s.projects_json, s.source_anchors_json,
                   s.parent_thread_id, s.thread_source,
                   s.source_system, s.source_originator, s.source_surface, s.source_version,
                   s.model_provider, s.model_name, s.agent_path,
                   fts.rank AS rank_score
            FROM conversation_sections_fts fts
            JOIN conversation_sections s ON s.id = fts.id
            WHERE conversation_sections_fts MATCH ?
        """
        params: list[Any] = [fts_expr]
        if conversation_id:
            sql += " AND s.conversation_id = ?"
            params.append(conversation_id)
        if parent_thread_id:
            sql += " AND s.parent_thread_id = ?"
            params.append(parent_thread_id)
        if source_system:
            sql += " AND (s.source_system = ? OR (s.source_system IS NULL AND ? = 'codex'))"
            params.extend([source_system, source_system])
        if thread_source:
            sql += " AND s.thread_source = ?"
            params.append(thread_source)

        sql += " ORDER BY fts.rank LIMIT ?"
        params.append(limit)

        results: list[SearchResult] = []
        with self._connect() as conn:
            try:
                cursor = conn.execute(sql, params)
                for row in cursor.fetchall():
                    headings = [h.strip() for h in (row["heading_path"] or "").split(">") if h.strip()]
                    results.append(
                        SearchResult(
                            section_id=row["id"],
                            conversation_id=row["conversation_id"],
                            vault_path=row["vault_path"],
                            heading_path=headings,
                            title=row["title"] or "",
                            content=row["content"],
                            score=abs(float(row["rank_score"])),
                            score_type="lexical",
                            date=row["date"] or "",
                            projects=json.loads(row["projects_json"] or "[]"),
                            source_anchors=json.loads(row["source_anchors_json"] or "[]"),
                            parent_thread_id=row["parent_thread_id"] or "",
                            thread_source=row["thread_source"] or "",
                            source_system=row["source_system"] or "codex",
                            source_originator=row["source_originator"] or "",
                            source_surface=row["source_surface"] or "",
                            source_version=row["source_version"] or "",
                            model_provider=row["model_provider"] or "",
                            model_name=row["model_name"] or "",
                            agent_path=row["agent_path"] or "",
                        )
                    )
            except sqlite3.OperationalError:
                return []
        return results

    def search_vector(
        self,
        query_vector: Sequence[float],
        *,
        limit: int = 10,
        conversation_id: str | None = None,
        parent_thread_id: str | None = None,
        source_system: str | None = None,
        thread_source: str | None = None,
    ) -> list[SearchResult]:
        if not query_vector:
            return []
        if not (self.embedding_client and self.embedding_client.enabled and self.embedding_client.model):
            return []

        model_name = self.embedding_client.model
        query_dimension = len(query_vector)

        sql = """
            SELECT s.id, s.conversation_id, s.vault_path, s.heading_path, s.title,
                   s.content, s.date, s.projects_json, s.source_anchors_json,
                   s.parent_thread_id, s.thread_source,
                   s.source_system, s.source_originator, s.source_surface, s.source_version,
                   s.model_provider, s.model_name, s.agent_path,
                   e.vector, e.dimension
            FROM conversation_embeddings e
            JOIN conversation_sections s
              ON s.embedding_identity = e.embedding_identity
             AND e.model = ?
             AND e.version = ?
             AND e.dimension = ?
        """
        params: list[Any] = [model_name, self.embedding_version, query_dimension]
        conditions: list[str] = []
        if conversation_id:
            conditions.append("s.conversation_id = ?")
            params.append(conversation_id)
        if parent_thread_id:
            conditions.append("s.parent_thread_id = ?")
            params.append(parent_thread_id)
        if source_system:
            conditions.append("(s.source_system = ? OR (s.source_system IS NULL AND ? = 'codex'))")
            params.extend([source_system, source_system])
        if thread_source:
            conditions.append("s.thread_source = ?")
            params.append(thread_source)
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)

        candidates: list[tuple[float, sqlite3.Row]] = []
        with self._connect() as conn:
            cursor = conn.execute(sql, params)
            for row in cursor.fetchall():
                vec = _unpack_vector(row["vector"])
                sim = cosine_similarity(list(query_vector), vec)
                candidates.append((sim, row))

        candidates.sort(key=lambda x: x[0], reverse=True)
        top = candidates[:limit]

        results: list[SearchResult] = []
        for sim, row in top:
            headings = [h.strip() for h in (row["heading_path"] or "").split(">") if h.strip()]
            results.append(
                SearchResult(
                    section_id=row["id"],
                    conversation_id=row["conversation_id"],
                    vault_path=row["vault_path"],
                    heading_path=headings,
                    title=row["title"] or "",
                    content=row["content"],
                    score=float(sim),
                    score_type="vector",
                    date=row["date"] or "",
                    projects=json.loads(row["projects_json"] or "[]"),
                    source_anchors=json.loads(row["source_anchors_json"] or "[]"),
                    parent_thread_id=row["parent_thread_id"] or "",
                    thread_source=row["thread_source"] or "",
                    source_system=row["source_system"] or "codex",
                    source_originator=row["source_originator"] or "",
                    source_surface=row["source_surface"] or "",
                    source_version=row["source_version"] or "",
                    model_provider=row["model_provider"] or "",
                    model_name=row["model_name"] or "",
                    agent_path=row["agent_path"] or "",
                )
            )
        return results

    def rebuild_from_markdown(self, markdown_paths: Sequence[str | Path]) -> int:
        with self._connect() as conn:
            conn.execute("DELETE FROM conversation_documents")
            conn.execute("DELETE FROM conversation_sections")
            conn.execute("DELETE FROM conversation_sections_fts")
        total_chunks = 0
        for path in markdown_paths:
            p = Path(path)
            if p.exists() and p.suffix.lower() == ".md":
                total_chunks += self.index_file(p)
        return total_chunks


def _migrate_index_db(conn: sqlite3.Connection) -> None:
    for tbl, col, col_type in [
        ("conversation_documents", "parent_thread_id", "TEXT"),
        ("conversation_documents", "thread_source", "TEXT"),
        ("conversation_documents", "source_system", "TEXT"),
        ("conversation_documents", "source_originator", "TEXT"),
        ("conversation_documents", "source_surface", "TEXT"),
        ("conversation_documents", "source_version", "TEXT"),
        ("conversation_documents", "model_provider", "TEXT"),
        ("conversation_documents", "model_name", "TEXT"),
        ("conversation_documents", "agent_path", "TEXT"),
        ("conversation_sections", "parent_thread_id", "TEXT"),
        ("conversation_sections", "thread_source", "TEXT"),
        ("conversation_sections", "source_system", "TEXT"),
        ("conversation_sections", "source_originator", "TEXT"),
        ("conversation_sections", "source_surface", "TEXT"),
        ("conversation_sections", "source_version", "TEXT"),
        ("conversation_sections", "model_provider", "TEXT"),
        ("conversation_sections", "model_name", "TEXT"),
        ("conversation_sections", "agent_path", "TEXT"),
        ("conversation_index_runs", "skipped_files", "INTEGER NOT NULL DEFAULT 0"),
        ("conversation_index_runs", "cache_reused_chunks", "INTEGER NOT NULL DEFAULT 0"),
        ("conversation_index_runs", "failed_chunks", "INTEGER NOT NULL DEFAULT 0"),
    ]:
        cursor = conn.execute(f"PRAGMA table_info({tbl})")
        cols = {row[1] for row in cursor.fetchall()}
        if col not in cols:
            conn.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} {col_type}")

    try:
        conn.execute("UPDATE conversation_documents SET source_system = 'codex' WHERE source_system IS NULL OR source_system = ''")
        conn.execute("UPDATE conversation_sections SET source_system = 'codex' WHERE source_system IS NULL OR source_system = ''")
    except sqlite3.OperationalError:
        pass

    # Older databases used chunk id as the embedding table primary key.  That
    # overwrote historical identities whenever model/version changed, defeating
    # the intended identity cache.  Migrate in-place to an identity-keyed cache;
    # search joins only live sections by embedding_identity, so orphan cache rows
    # remain harmless and reusable.
    emb_sql_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='conversation_embeddings'"
    ).fetchone()
    emb_sql = (emb_sql_row[0] or "") if emb_sql_row else ""
    if "id TEXT PRIMARY KEY" in emb_sql:
        conn.execute(
            """
            CREATE TABLE conversation_embeddings_v2 (
                id TEXT NOT NULL,
                embedding_identity TEXT NOT NULL,
                model TEXT NOT NULL,
                version TEXT NOT NULL,
                dimension INTEGER NOT NULL,
                vector BLOB NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(embedding_identity, model, version)
            )
            """
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO conversation_embeddings_v2(
                id, embedding_identity, model, version, dimension, vector, created_at
            )
            SELECT id, embedding_identity, model, version, dimension, vector, created_at
            FROM conversation_embeddings
            """
        )
        conn.execute("DROP TABLE conversation_embeddings")
        conn.execute("ALTER TABLE conversation_embeddings_v2 RENAME TO conversation_embeddings")

    # 检查 FTS 表是否包含 parent_thread_id
    cursor = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='conversation_sections_fts'")
    row = cursor.fetchone()
    if row and "parent_thread_id" not in row[0]:
        conn.execute("DROP TABLE conversation_sections_fts")
        conn.execute(
            """
            CREATE VIRTUAL TABLE conversation_sections_fts USING fts5(
                id UNINDEXED,
                conversation_id,
                parent_thread_id,
                heading_path,
                content,
                tokenize='unicode61'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO conversation_sections_fts(id, conversation_id, parent_thread_id, heading_path, content)
            SELECT id, conversation_id, COALESCE(parent_thread_id, ''), heading_path, content
            FROM conversation_sections
            """
        )


def _format_fts_query(query: str) -> str:
    # 若包含双引号，直接原样作为精确短语查询
    if '"' in query:
        return query
    # 否则对特殊标点符号和空格做转义或短语包裹
    tokens = query.split()
    clean_tokens: list[str] = []
    for token in tokens:
        # 如果包含任何非字母数字下划线字符（如 +、-、\、:、/、. 等），用双引号安全包裹避免 FTS 语法错误
        if any(not c.isalnum() and c != '_' for c in token):
            escaped = token.replace('"', '""')
            clean_tokens.append(f'"{escaped}"')
        else:
            clean_tokens.append(token)
    return " ".join(clean_tokens)
