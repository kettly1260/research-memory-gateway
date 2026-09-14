"""Generic resumable upload manager and blob storage for Research Memory Gateway.

Follows the tus 1.0.0 protocol specifications and provides an immutable,
content-addressed storage layer for uploaded artifacts (ZIP, PDF, images, etc.).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import shutil
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from asgi_tus.storage import FileStorage, UploadInfo

logger = logging.getLogger(__name__)

_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def sha256_file(path: Path | str, chunk_size: int = 1024 * 1024) -> str:
    """Compute streaming SHA-256 hash of a file on disk."""
    hasher = hashlib.sha256()
    with open(path, "rb") as stream:
        while True:
            chunk = stream.read(chunk_size)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest().lower()


@dataclass(frozen=True)
class UploadSession:
    upload_id: str
    filename: str
    size_bytes: int
    sha256: str
    content_type: str
    source_hint: str | None
    created_at: str
    expires_at: str
    state: str


class UploadManager:
    """Manages the lifecycle of uploaded artifacts via tus 1.0.0."""

    def __init__(self, base_dir: Path | str, default_expiry_hours: int = 24) -> None:
        self.base_dir = Path(base_dir).resolve()
        self.incoming_dir = self.base_dir / "incoming"
        self.blobs_dir = self.base_dir / "blobs"
        self.db_path = self.base_dir / "uploads.sqlite"
        self.default_expiry_hours = default_expiry_hours

        self.incoming_dir.mkdir(parents=True, exist_ok=True)
        self.blobs_dir.mkdir(parents=True, exist_ok=True)

        self.storage = FileStorage(str(self.incoming_dir))
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS uploads (
                    upload_id TEXT PRIMARY KEY,
                    filename TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    source_hint TEXT,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    final_sha256 TEXT,
                    final_blob_path TEXT,
                    error TEXT
                );
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_uploads_state ON uploads(state);")

    def create_upload(
        self,
        filename: str,
        size_bytes: int,
        sha256: str,
        content_type: str = "application/zip",
        source_hint: str | None = None,
        expiry_hours: int | None = None,
    ) -> UploadSession:
        """Register a new upload session and initialize the underlying tus upload resource."""
        clean_filename = Path(filename).name.strip()
        if not clean_filename:
            clean_filename = "unnamed_artifact.bin"

        if size_bytes <= 0:
            raise ValueError(f"size_bytes must be strictly positive, got {size_bytes}")

        clean_sha256 = sha256.strip().lower()
        if not _SHA256_RE.match(clean_sha256):
            raise ValueError(f"Invalid sha256 hash: {sha256!r}. Must be a 64-character hex string.")

        hours = expiry_hours if expiry_hours is not None else self.default_expiry_hours
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(hours=hours)

        # Pre-create the upload resource in asgi-tus storage
        upload_info: UploadInfo = self._run_async(
            self.storage.create_upload(
                length=size_bytes,
                metadata={
                    "filename": clean_filename,
                    "sha256": clean_sha256,
                    "content_type": content_type,
                },
                expires_at=expires_at,
            )
        )
        upload_id = upload_info.id

        session = UploadSession(
            upload_id=upload_id,
            filename=clean_filename,
            size_bytes=size_bytes,
            sha256=clean_sha256,
            content_type=content_type,
            source_hint=source_hint,
            created_at=now.isoformat(),
            expires_at=expires_at.isoformat(),
            state="created",
        )

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO uploads (
                    upload_id, filename, size_bytes, sha256, content_type,
                    source_hint, state, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session.upload_id,
                    session.filename,
                    session.size_bytes,
                    session.sha256,
                    session.content_type,
                    session.source_hint,
                    session.state,
                    session.created_at,
                    session.expires_at,
                ),
            )

        logger.info(
            "Created upload session upload_id=%s filename=%s size=%d sha256=%s",
            upload_id,
            clean_filename,
            size_bytes,
            clean_sha256,
        )
        return session

    def get_upload_status(self, upload_id: str) -> dict[str, Any]:
        """Query real-time status of an upload session directly from underlying storage."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM uploads WHERE upload_id = ?", (upload_id,)).fetchone()

        if not row:
            raise KeyError(f"Upload session {upload_id!r} not found")

        current_state = row["state"]
        size_bytes = row["size_bytes"]
        expires_at_dt = datetime.fromisoformat(row["expires_at"])
        now_dt = datetime.now(timezone.utc)

        if current_state in ("committed", "aborted"):
            return {
                "upload_id": upload_id,
                "filename": row["filename"],
                "state": current_state,
                "received_bytes": size_bytes if current_state == "committed" else 0,
                "total_bytes": size_bytes,
                "completed": (current_state == "committed"),
                "expires_at": row["expires_at"],
                "error": row["error"],
                "final_sha256": row["final_sha256"],
                "final_blob_path": row["final_blob_path"],
            }

        upload_info: Optional[UploadInfo] = self._run_async(self.storage.get_upload(upload_id))
        physical_file = self.storage._get_upload_path(upload_id)
        physical_size = physical_file.stat().st_size if physical_file.exists() else 0
        tus_offset = upload_info.offset if upload_info else 0
        received_bytes = max(physical_size, tus_offset)

        if received_bytes >= size_bytes:
            state = "completed"
            completed = True
        elif now_dt > expires_at_dt:
            state = "expired"
            completed = False
        elif received_bytes > 0:
            state = "uploading"
            completed = False
        else:
            state = "created"
            completed = False

        if state != current_state:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("UPDATE uploads SET state = ? WHERE upload_id = ?", (state, upload_id))

        return {
            "upload_id": upload_id,
            "filename": row["filename"],
            "state": state,
            "received_bytes": received_bytes,
            "total_bytes": size_bytes,
            "completed": completed,
            "expires_at": row["expires_at"],
            "error": row["error"],
            "final_sha256": row["final_sha256"],
            "final_blob_path": row["final_blob_path"],
        }

    def verify_and_finalize(
        self, upload_id: str, expected_sha256: str | None = None
    ) -> Path:
        """Confirm completion, verify size and SHA-256 hash, and move to immutable blob storage.

        Raises ValueError on size or checksum mismatch (fail-closed).
        """
        status = self.get_upload_status(upload_id)
        if status["state"] == "committed":
            return Path(status["final_blob_path"])

        if not status["completed"]:
            raise ValueError(
                f"Upload {upload_id!r} is not complete (received {status['received_bytes']} of {status['total_bytes']} bytes)"
            )

        physical_file = self.storage._get_upload_path(upload_id)
        if not physical_file.exists():
            raise FileNotFoundError(f"Upload file not found on disk for {upload_id!r}")

        physical_size = physical_file.stat().st_size
        if physical_size != status["total_bytes"]:
            error_msg = (
                f"Physical file size mismatch for {upload_id!r}: "
                f"expected {status['total_bytes']}, found {physical_size}"
            )
            self._record_error(upload_id, error_msg)
            raise ValueError(error_msg)

        computed_sha256 = sha256_file(physical_file)
        declared_sha256 = (expected_sha256 or status.get("sha256") or "").lower()

        # Check against declared sha256 from DB if not passed explicitly
        if not declared_sha256:
            with sqlite3.connect(self.db_path) as conn:
                row = conn.execute("SELECT sha256 FROM uploads WHERE upload_id = ?", (upload_id,)).fetchone()
                if row:
                    declared_sha256 = row[0].lower()

        if declared_sha256 and computed_sha256 != declared_sha256:
            error_msg = (
                f"SHA-256 checksum mismatch for upload {upload_id!r}: "
                f"declared {declared_sha256}, computed {computed_sha256}"
            )
            self._record_error(upload_id, error_msg)
            raise ValueError(error_msg)

        # Move to immutable content-addressed storage
        target_blob_path = self.blobs_dir / f"{computed_sha256}.blob"
        if not target_blob_path.exists():
            shutil.copy2(str(physical_file), str(target_blob_path))
        else:
            logger.info("Blob %s already exists in immutable store; reusing.", computed_sha256)

        # Update database
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE uploads
                SET state = 'committed',
                    final_sha256 = ?,
                    final_blob_path = ?,
                    error = NULL
                WHERE upload_id = ?
                """,
                (computed_sha256, str(target_blob_path), upload_id),
            )

        logger.info(
            "Finalized upload upload_id=%s into immutable blob=%s",
            upload_id,
            target_blob_path,
        )
        return target_blob_path

    def abort_upload(self, upload_id: str) -> bool:
        """Cancel an in-flight or completed upload and clean up temporary data."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT state FROM uploads WHERE upload_id = ?", (upload_id,)).fetchone()
            if not row:
                return False
            if row["state"] == "committed":
                raise ValueError(f"Cannot abort upload {upload_id!r}: already committed into immutable storage")

            conn.execute("UPDATE uploads SET state = 'aborted' WHERE upload_id = ?", (upload_id,))

        self._run_async(self.storage.delete_upload(upload_id))
        logger.info("Aborted upload upload_id=%s", upload_id)
        return True

    def _record_error(self, upload_id: str, error_msg: str) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("UPDATE uploads SET error = ? WHERE upload_id = ?", (error_msg, upload_id))

    def _run_async(self, coro: Any) -> Any:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(asyncio.run, coro)
                return future.result()
        else:
            return asyncio.run(coro)
