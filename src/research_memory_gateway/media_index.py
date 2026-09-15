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
        embedding_version: str | None = None,
        vector_generation: int | None = None,
        max_image_bytes: int = 32 * 1024 * 1024,
    ) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.embedding_client = embedding_client
        if vector_generation is not None:
            self.vector_generation: int | str = int(vector_generation)
            self.embedding_version = self._generation_cache_key(int(vector_generation))
        else:
            self.embedding_version = embedding_version or "v1"
            self.vector_generation = self.embedding_version
        self.max_image_bytes = max(1, int(max_image_bytes))
        self._init_db()

    def set_vector_generation(self, generation: int) -> None:
        """Switch the active RMG-local vector generation."""
        self.vector_generation = int(generation)
        self.embedding_version = self._generation_cache_key(int(generation))

    @staticmethod
    def _generation_cache_key(generation: int) -> str:
        # Adopt the existing v1 cache as generation 1. Unlike legacy Research
        # Memory vectors, Media vectors already stored model + version safely.
        return "v1" if generation == 1 else f"g{generation}"

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
                CREATE TABLE IF NOT EXISTS media_blobs (
                    content_hash TEXT PRIMARY KEY,
                    mime_type TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    content BLOB NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
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
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS media_index_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )

    def _state_get(self, key: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM media_index_state WHERE key = ?",
                (key,),
            ).fetchone()
        return str(row["value"]) if row is not None else None

    def _state_set(self, **values: str | int | None) -> None:
        with self._connect() as connection:
            for key, value in values.items():
                if value is None:
                    connection.execute("DELETE FROM media_index_state WHERE key = ?", (key,))
                else:
                    connection.execute(
                        """
                        INSERT INTO media_index_state(key, value) VALUES (?, ?)
                        ON CONFLICT(key) DO UPDATE SET value = excluded.value
                        """,
                        (key, str(value)),
                    )

    def _record_image_embedding_result(self, *, success: bool) -> None:
        client = self.embedding_client
        status_code = client.last_status_code if client is not None else None
        error = client.last_error if client is not None else None
        if success:
            state = "ready"
            error = None
            status_code = 200 if status_code is None else status_code
        elif status_code in {400, 415, 422}:
            # This is deliberately phrased as a downstream rejection rather
            # than a model-capability verdict. The configured provider/model
            # may be changed at any time without RMG maintaining a capability
            # registry.
            state = "image_input_rejected"
        else:
            state = "error"
        self._state_set(
            image_embedding_state=state,
            image_embedding_last_error=error,
            image_embedding_last_status_code=status_code,
            image_embedding_last_model=(client.model if client is not None else ""),
            image_embedding_last_attempt_at=_utc_now(),
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
            cached_blobs = int(connection.execute("SELECT COUNT(*) FROM media_blobs").fetchone()[0])
            cached_blob_bytes = int(
                connection.execute("SELECT COALESCE(SUM(size_bytes), 0) FROM media_blobs").fetchone()[0]
            )
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
            "cached_blobs": cached_blobs,
            "cached_blob_bytes": cached_blob_bytes,
            "resources_with_active_embedding": covered,
            "resources_without_active_embedding": max(0, resources - covered),
            "vector_coverage": round(covered / resources, 4) if resources else (1.0 if active_model else 0.0),
            "embedding_model": active_model,
            "embedding_version": active_version,
            "vector_generation": self.vector_generation if active_model else None,
            "embedding_dimension": dimension,
            "image_embedding_state": self._state_get("image_embedding_state") or "unknown",
            "image_embedding_last_error": self._state_get("image_embedding_last_error"),
            "image_embedding_last_status_code": (
                int(status_code)
                if (status_code := self._state_get("image_embedding_last_status_code"))
                else None
            ),
            "image_embedding_last_model": self._state_get("image_embedding_last_model"),
            "image_embedding_last_attempt_at": self._state_get("image_embedding_last_attempt_at"),
        }

    def embedding_backfill_candidates(
        self,
        *,
        force: bool = False,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return media resources missing a vector for the active identity."""
        client = self.embedding_client
        if client is None or not client.enabled or not client.model:
            return []
        params: list[Any] = [client.model, self.embedding_version]
        where = ""
        if not force:
            where = """
                WHERE NOT EXISTS (
                    SELECT 1 FROM media_embeddings e
                    WHERE e.content_hash = r.content_hash
                      AND e.model = ? AND e.version = ?
                )
            """
        else:
            params = []
        sql = f"""
            SELECT r.*, CASE WHEN b.content_hash IS NULL THEN 0 ELSE 1 END AS has_cached_content
            FROM media_resources r
            LEFT JOIN media_blobs b ON b.content_hash = r.content_hash
            {where}
            ORDER BY r.indexed_at, r.resource_id
        """
        if limit is not None:
            sql += " LIMIT ?"
            params.append(max(0, int(limit)))
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        candidates: list[dict[str, Any]] = []
        for row in rows:
            locator = str(row["locator"])
            path = Path(locator)
            has_cached_content = bool(row["has_cached_content"])
            candidates.append(
                {
                    "resource_id": str(row["resource_id"]),
                    "locator": locator,
                    "mime_type": str(row["mime_type"]),
                    "content_hash": str(row["content_hash"]),
                    "source_system": str(row["source_system"]),
                    "source_resource_id": str(row["source_resource_id"]),
                    "parent_resource_id": str(row["parent_resource_id"]),
                    "rebuildable": (path.exists() and path.is_file()) or has_cached_content,
                    "has_cached_content": has_cached_content,
                }
            )
        return candidates

    def reembed_resource(self, resource_id: str, *, force: bool = False) -> dict[str, Any]:
        """Re-embed one stored media resource from its persisted local locator."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM media_resources WHERE resource_id = ?",
                (resource_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Media resource not found: {resource_id}")
        locator = str(row["locator"])
        path = Path(locator)
        if path.exists() and path.is_file():
            content = path.read_bytes()
        else:
            with self._connect() as connection:
                blob = connection.execute(
                    "SELECT content FROM media_blobs WHERE content_hash = ?",
                    (row["content_hash"],),
                ).fetchone()
            if blob is None:
                raise FileNotFoundError(f"Media source is unavailable and no cached content exists: {locator}")
            content = bytes(blob["content"])
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        return self.index_image_bytes(
            content,
            locator=locator,
            mime_type=str(row["mime_type"]),
            source_system=str(row["source_system"]),
            source_resource_id=str(row["source_resource_id"]),
            parent_resource_id=str(row["parent_resource_id"]),
            caption=str(row["caption"]),
            ocr_text=str(row["ocr_text"]),
            metadata=metadata,
            force=force,
        )

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
                INSERT OR IGNORE INTO media_blobs(
                    content_hash, mime_type, size_bytes, content, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (content_hash, mime_type, len(content), content, now),
            )
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
            self._record_image_embedding_result(success=False)
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

        self._record_image_embedding_result(success=True)

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
            if query_vector:
                self._record_image_embedding_result(success=True)
            else:
                self._record_image_embedding_result(success=False)
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
