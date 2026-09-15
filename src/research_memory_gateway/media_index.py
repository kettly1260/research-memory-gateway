from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import mimetypes
from pathlib import Path
import sqlite3
import struct
from datetime import datetime, timezone
from typing import Any, Generator

from .retrieval import EmbeddingClient, cosine_similarity


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _resource_id(source_system: str, source_resource_id: str, locator: str) -> str:
    raw = f"{source_system.strip()}\0{source_resource_id.strip()}\0{locator.strip()}"
    return f"media_{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:32]}"


def _embedding_identity(content_hash: str, model: str, version: str) -> str:
    raw = f"{content_hash}\0{model.strip()}\0{version.strip()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _pack_vector(vector: list[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def _unpack_vector(blob: bytes) -> list[float]:
    if not blob:
        return []
    return list(struct.unpack(f"<{len(blob) // 4}f", blob))


class MediaIndexDatabase:
    """Content-addressed image/media vector index.

    The index deliberately shares the gateway's existing ``EmbeddingClient``.
    It never asks what modalities the model supports. Image bytes are sent as
    data-URI values through the same embedding ``input`` field used for text.
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        embedding_client: EmbeddingClient | None = None,
        embedding_version: str = "v1",
        max_image_bytes: int = 32 * 1024 * 1024,
    ) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.embedding_client = embedding_client
        self.embedding_version = embedding_version
        self.max_image_bytes = max(1, int(max_image_bytes))
        self._init_db()

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection, None, None]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _init_db(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS media_resources (
                    resource_id TEXT PRIMARY KEY,
                    resource_type TEXT NOT NULL,
                    source_system TEXT NOT NULL,
                    source_resource_id TEXT NOT NULL DEFAULT '',
                    parent_resource_id TEXT NOT NULL DEFAULT '',
                    locator TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    caption TEXT NOT NULL DEFAULT '',
                    ocr_text TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    indexed_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_media_resources_content_hash ON media_resources(content_hash)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_media_resources_source ON media_resources(source_system, source_resource_id)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_media_resources_parent ON media_resources(parent_resource_id)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS media_embeddings (
                    embedding_identity TEXT PRIMARY KEY,
                    content_hash TEXT NOT NULL,
                    model TEXT NOT NULL,
                    version TEXT NOT NULL,
                    dimension INTEGER NOT NULL,
                    vector BLOB NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(content_hash, model, version)
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_media_embeddings_model ON media_embeddings(model, version)"
            )

    def stats(self) -> dict[str, Any]:
        client = self.embedding_client
        active_model = client.model if client and client.enabled and client.model else None
        active_version = self.embedding_version if active_model else None
        with self._connect() as connection:
            resources = int(connection.execute("SELECT COUNT(*) FROM media_resources").fetchone()[0])
            images = int(
                connection.execute(
                    "SELECT COUNT(*) FROM media_resources WHERE resource_type = 'image'"
                ).fetchone()[0]
            )
            embeddings = int(connection.execute("SELECT COUNT(*) FROM media_embeddings").fetchone()[0])
            covered = 0
            dimension = None
            if active_model:
                covered = int(
                    connection.execute(
                        """
                        SELECT COUNT(*)
                        FROM media_resources r
                        WHERE EXISTS (
                            SELECT 1 FROM media_embeddings e
                            WHERE e.content_hash = r.content_hash
                              AND e.model = ? AND e.version = ?
                        )
                        """,
                        (active_model, active_version),
                    ).fetchone()[0]
                )
                row = connection.execute(
                    "SELECT dimension FROM media_embeddings WHERE model = ? AND version = ? LIMIT 1",
                    (active_model, active_version),
                ).fetchone()
                if row is not None:
                    dimension = int(row["dimension"])
        return {
            "resources": resources,
            "images": images,
            "embeddings": embeddings,
            "resources_with_active_embedding": covered,
            "resources_without_active_embedding": max(0, resources - covered),
            "vector_coverage": round(covered / resources, 4) if resources else (1.0 if active_model else 0.0),
            "embedding_model": active_model,
            "embedding_version": active_version,
            "embedding_dimension": dimension,
        }

    def index_image_file(
        self,
        file_path: str | Path,
        *,
        source_system: str = "local",
        source_resource_id: str = "",
        parent_resource_id: str = "",
        caption: str = "",
        ocr_text: str = "",
        metadata: dict[str, Any] | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        path = Path(file_path).resolve()
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(f"Image file not found: {path}")
        size = path.stat().st_size
        if size > self.max_image_bytes:
            raise ValueError(
                f"Image exceeds media_index.max_image_bytes: {size} > {self.max_image_bytes}"
            )
        content = path.read_bytes()
        mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if not mime_type.startswith("image/"):
            raise ValueError(f"Unsupported media type for image indexing: {mime_type}")
        return self.index_image_bytes(
            content,
            locator=str(path),
            mime_type=mime_type,
            source_system=source_system,
            source_resource_id=source_resource_id,
            parent_resource_id=parent_resource_id,
            caption=caption,
            ocr_text=ocr_text,
            metadata=metadata,
            force=force,
        )

    def index_image_bytes(
        self,
        content: bytes,
        *,
        locator: str,
        mime_type: str,
        source_system: str = "local",
        source_resource_id: str = "",
        parent_resource_id: str = "",
        caption: str = "",
        ocr_text: str = "",
        metadata: dict[str, Any] | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        if not content:
            raise ValueError("Image content is empty")
        if len(content) > self.max_image_bytes:
            raise ValueError(
                f"Image exceeds media_index.max_image_bytes: {len(content)} > {self.max_image_bytes}"
            )
        if not mime_type.startswith("image/"):
            raise ValueError(f"Unsupported media type for image indexing: {mime_type}")

        source_system = source_system.strip() or "local"
        source_resource_id = source_resource_id.strip()
        parent_resource_id = parent_resource_id.strip()
        locator = locator.strip()
        if not locator:
            raise ValueError("locator is required")

        content_hash = _sha256_bytes(content)
        resource_id = _resource_id(source_system, source_resource_id, locator)
        now = _utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO media_resources(
                    resource_id, resource_type, source_system, source_resource_id,
                    parent_resource_id, locator, mime_type, content_hash, size_bytes,
                    caption, ocr_text, metadata_json, indexed_at
                ) VALUES (?, 'image', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(resource_id) DO UPDATE SET
                    resource_type='image',
                    source_system=excluded.source_system,
                    source_resource_id=excluded.source_resource_id,
                    parent_resource_id=excluded.parent_resource_id,
                    locator=excluded.locator,
                    mime_type=excluded.mime_type,
                    content_hash=excluded.content_hash,
                    size_bytes=excluded.size_bytes,
                    caption=excluded.caption,
                    ocr_text=excluded.ocr_text,
                    metadata_json=excluded.metadata_json,
                    indexed_at=excluded.indexed_at
                """,
                (
                    resource_id,
                    source_system,
                    source_resource_id,
                    parent_resource_id,
                    locator,
                    mime_type,
                    content_hash,
                    len(content),
                    caption,
                    ocr_text,
                    json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )

        client = self.embedding_client
        if client is None or not client.enabled or not client.model:
            return {
                "resource_id": resource_id,
                "content_hash": content_hash,
                "embedded": False,
                "cached": False,
                "embedding_error": "embedding_not_configured",
            }

        model = client.model
        version = self.embedding_version
        identity = _embedding_identity(content_hash, model, version)
        if not force:
            with self._connect() as connection:
                cached = connection.execute(
                    """
                    SELECT dimension FROM media_embeddings
                    WHERE content_hash = ? AND model = ? AND version = ?
                    LIMIT 1
                    """,
                    (content_hash, model, version),
                ).fetchone()
            if cached is not None:
                return {
                    "resource_id": resource_id,
                    "content_hash": content_hash,
                    "embedded": True,
                    "cached": True,
                    "embedding_identity": identity,
                    "model": model,
                    "version": version,
                    "dimension": int(cached["dimension"]),
                }

        vector = client.embed_image_bytes(content, mime_type=mime_type)
        if not vector:
            return {
                "resource_id": resource_id,
                "content_hash": content_hash,
                "embedded": False,
                "cached": False,
                "embedding_error": client.last_error or "embedding_failed",
                "embedding_status_code": client.last_status_code,
                "model": model,
                "version": version,
            }

        with self._connect() as connection:
            existing_dim = connection.execute(
                "SELECT dimension FROM media_embeddings WHERE model = ? AND version = ? LIMIT 1",
                (model, version),
            ).fetchone()
            if existing_dim is not None and int(existing_dim["dimension"]) != len(vector):
                raise ValueError(
                    f"Embedding dimension mismatch for model {model}: "
                    f"expected {existing_dim['dimension']}, got {len(vector)}"
                )
            connection.execute(
                """
                INSERT INTO media_embeddings(
                    embedding_identity, content_hash, model, version,
                    dimension, vector, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(content_hash, model, version) DO UPDATE SET
                    embedding_identity=excluded.embedding_identity,
                    dimension=excluded.dimension,
                    vector=excluded.vector,
                    created_at=excluded.created_at
                """,
                (
                    identity,
                    content_hash,
                    model,
                    version,
                    len(vector),
                    _pack_vector(vector),
                    now,
                ),
            )
        return {
            "resource_id": resource_id,
            "content_hash": content_hash,
            "embedded": True,
            "cached": False,
            "embedding_identity": identity,
            "model": model,
            "version": version,
            "dimension": len(vector),
        }

    def search(
        self,
        *,
        query: str | None = None,
        image_path: str | Path | None = None,
        source_system: str | None = None,
        parent_resource_id: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        client = self.embedding_client
        if client is None or not client.enabled or not client.model:
            return {
                "count": 0,
                "results": [],
                "error": "embedding_not_configured",
            }
        if bool(query and query.strip()) == bool(image_path):
            raise ValueError("Provide exactly one of query or image_path")

        if image_path is not None:
            path = Path(image_path).resolve()
            if not path.exists() or not path.is_file():
                raise FileNotFoundError(f"Query image file not found: {path}")
            if path.stat().st_size > self.max_image_bytes:
                raise ValueError(
                    f"Image exceeds media_index.max_image_bytes: {path.stat().st_size} > {self.max_image_bytes}"
                )
            mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            if not mime_type.startswith("image/"):
                raise ValueError(f"Unsupported media type for image query: {mime_type}")
            query_vector = client.embed_image_bytes(path.read_bytes(), mime_type=mime_type)
            query_kind = "image"
        else:
            query_vector = client.embed((query or "").strip())
            query_kind = "text"

        if not query_vector:
            return {
                "count": 0,
                "results": [],
                "error": client.last_error or "embedding_failed",
                "embedding_status_code": client.last_status_code,
                "query_kind": query_kind,
            }
        results = self.search_vector(
            query_vector,
            source_system=source_system,
            parent_resource_id=parent_resource_id,
            limit=limit,
        )
        return {
            "count": len(results),
            "results": results,
            "query_kind": query_kind,
            "embedding_model": client.model,
            "embedding_version": self.embedding_version,
            "query_dimension": len(query_vector),
        }

    def search_vector(
        self,
        query_vector: list[float],
        *,
        source_system: str | None = None,
        parent_resource_id: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        client = self.embedding_client
        if client is None or not client.model:
            return []
        filters = ["e.model = ?", "e.version = ?", "e.dimension = ?"]
        params: list[Any] = [client.model, self.embedding_version, len(query_vector)]
        if source_system:
            filters.append("r.source_system = ?")
            params.append(source_system)
        if parent_resource_id:
            filters.append("r.parent_resource_id = ?")
            params.append(parent_resource_id)
        sql = f"""
            SELECT r.*, e.vector, e.dimension, e.embedding_identity
            FROM media_resources r
            JOIN media_embeddings e ON e.content_hash = r.content_hash
            WHERE {' AND '.join(filters)}
        """
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()

        scored: list[tuple[float, sqlite3.Row]] = []
        for row in rows:
            score = cosine_similarity(query_vector, _unpack_vector(row["vector"]))
            scored.append((score, row))
        scored.sort(key=lambda item: item[0], reverse=True)

        results: list[dict[str, Any]] = []
        for score, row in scored[: max(1, min(int(limit), 100))]:
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                metadata = {}
            results.append(
                {
                    "resource_id": row["resource_id"],
                    "resource_type": row["resource_type"],
                    "source_system": row["source_system"],
                    "source_resource_id": row["source_resource_id"],
                    "parent_resource_id": row["parent_resource_id"],
                    "locator": row["locator"],
                    "mime_type": row["mime_type"],
                    "content_hash": row["content_hash"],
                    "size_bytes": row["size_bytes"],
                    "caption": row["caption"],
                    "ocr_text": row["ocr_text"],
                    "metadata": metadata,
                    "vector_score": score,
                    "embedding_identity": row["embedding_identity"],
                }
            )
        return results


__all__ = ["MediaIndexDatabase"]
