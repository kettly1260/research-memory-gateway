"""ASGI tus 1.0.0 integration for Starlette."""

from __future__ import annotations

from datetime import timedelta
from asgi_tus import ASGITusApp, TusConfig

from .manager import UploadManager


def create_tus_asgi_app(
    upload_manager: UploadManager,
    upload_path: str = "/uploads",
    max_size_bytes: int = 1024 * 1024 * 1024,  # 1 GB
) -> ASGITusApp:
    """Create a tus 1.0.0 ASGI application bound to the UploadManager's storage."""
    config = TusConfig(
        upload_path=upload_path,
        max_size=max_size_bytes,
        upload_expires=timedelta(hours=upload_manager.default_expiry_hours),
        cors_enabled=True,
    )
    return ASGITusApp(upload_manager.storage, config)
