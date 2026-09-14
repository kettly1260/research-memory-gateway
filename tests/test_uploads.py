from __future__ import annotations

import hashlib
import io
import shutil
import socket
import tempfile
import time
from pathlib import Path
from threading import Thread

import httpx
import pytest
import uvicorn
from starlette.applications import Starlette
from tusclient import client as tus_client

from research_memory_gateway.config import AppConfig
from research_memory_gateway.server import _build_streamable_http_app, build_mcp
from research_memory_gateway.uploads import (
    UploadManager,
    create_tus_asgi_app,
    sha256_file,
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_port(port: int, timeout: float = 8.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise TimeoutError(f"Port {port} did not open within {timeout}s")


def test_upload_manager_validation(tmp_path: Path) -> None:
    manager = UploadManager(base_dir=tmp_path / "uploads")

    # Invalid size
    with pytest.raises(ValueError, match="strictly positive"):
        manager.create_upload("test.zip", size_bytes=0, sha256="a" * 64)

    with pytest.raises(ValueError, match="strictly positive"):
        manager.create_upload("test.zip", size_bytes=-10, sha256="a" * 64)

    # Invalid sha256
    with pytest.raises(ValueError, match="Invalid sha256"):
        manager.create_upload("test.zip", size_bytes=100, sha256="not-a-hash")

    with pytest.raises(ValueError, match="Invalid sha256"):
        manager.create_upload("test.zip", size_bytes=100, sha256="a" * 63)


def test_upload_manager_lifecycle_and_fail_closed(tmp_path: Path) -> None:
    manager = UploadManager(base_dir=tmp_path / "uploads")
    payload = b"Hello, research memory tus upload test payload!"
    real_sha256 = hashlib.sha256(payload).hexdigest()
    size = len(payload)

    # 1. Create upload session
    session = manager.create_upload("test.bin", size_bytes=size, sha256=real_sha256)
    assert session.upload_id
    assert session.state == "created"

    status = manager.get_upload_status(session.upload_id)
    assert status["total_bytes"] == size
    assert status["received_bytes"] == 0
    assert not status["completed"]

    # 2. Premature finalize should fail
    with pytest.raises(ValueError, match="not complete"):
        manager.verify_and_finalize(session.upload_id)

    # 3. Simulate writing file partially
    physical_path = manager.storage._get_upload_path(session.upload_id)
    with open(physical_path, "wb") as f:
        f.write(payload[:10])

    status = manager.get_upload_status(session.upload_id)
    assert status["received_bytes"] == 10
    assert status["state"] == "uploading"

    with pytest.raises(ValueError, match="not complete"):
        manager.verify_and_finalize(session.upload_id)

    # 4. Corrupt payload with size match but hash mismatch (fail-closed)
    corrupted_payload = payload[:-1] + b"X"
    with open(physical_path, "wb") as f:
        f.write(corrupted_payload)

    status = manager.get_upload_status(session.upload_id)
    assert status["received_bytes"] == size
    assert status["completed"]

    with pytest.raises(ValueError, match="SHA-256 checksum mismatch"):
        manager.verify_and_finalize(session.upload_id)

    # Status should record error
    status = manager.get_upload_status(session.upload_id)
    assert "checksum mismatch" in (status["error"] or "")

    # 5. Correct payload -> verify and commit into immutable blob store
    with open(physical_path, "wb") as f:
        f.write(payload)

    blob_path = manager.verify_and_finalize(session.upload_id)
    assert blob_path.exists()
    assert blob_path.name == f"{real_sha256}.blob"
    assert blob_path.read_bytes() == payload

    final_status = manager.get_upload_status(session.upload_id)
    assert final_status["state"] == "committed"
    assert final_status["final_sha256"] == real_sha256

    # 6. Cannot abort after commit
    with pytest.raises(ValueError, match="already committed"):
        manager.abort_upload(session.upload_id)


def test_upload_manager_abort(tmp_path: Path) -> None:
    manager = UploadManager(base_dir=tmp_path / "uploads")
    payload = b"Sample abort payload"
    sha = hashlib.sha256(payload).hexdigest()

    session = manager.create_upload("sample.bin", size_bytes=len(payload), sha256=sha)
    physical_path = manager.storage._get_upload_path(session.upload_id)
    assert physical_path.exists()

    res = manager.abort_upload(session.upload_id)
    assert res is True
    status = manager.get_upload_status(session.upload_id)
    assert status["state"] == "aborted"
    assert not physical_path.exists()


def test_tus_asgi_app_direct(tmp_path: Path) -> None:
    import anyio

    async def _run() -> None:
        manager = UploadManager(base_dir=tmp_path / "uploads")
        tus_app = create_tus_asgi_app(manager, upload_path="/uploads")

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=tus_app), base_url="http://test") as client:
            # OPTIONS
            resp = await client.options("/uploads/")
            assert resp.status_code == 204
            assert resp.headers.get("Tus-Resumable") == "1.0.0"

            # POST creation
            resp = await client.post(
                "/uploads/",
                headers={"Tus-Resumable": "1.0.0", "Upload-Length": "12"},
            )
            assert resp.status_code == 201
            location = resp.headers["location"]
            upload_id = location.rstrip("/").split("/")[-1]

            # HEAD
            resp = await client.head(location, headers={"Tus-Resumable": "1.0.0"})
            assert resp.status_code == 200
            assert resp.headers["upload-offset"] == "0"
            assert resp.headers["upload-length"] == "12"

            # PATCH chunk 1: 5 bytes
            resp = await client.patch(
                location,
                headers={
                    "Tus-Resumable": "1.0.0",
                    "Upload-Offset": "0",
                    "Content-Type": "application/offset+octet-stream",
                },
                content=b"hello",
            )
            assert resp.status_code == 204
            assert resp.headers["upload-offset"] == "5"

            # PATCH chunk 2: 7 bytes
            resp = await client.patch(
                location,
                headers={
                    "Tus-Resumable": "1.0.0",
                    "Upload-Offset": "5",
                    "Content-Type": "application/offset+octet-stream",
                },
                content=b" world!",
            )
            assert resp.status_code == 204
            assert resp.headers["upload-offset"] == "12"

            # Check physical file content
            physical_file = manager.storage._get_upload_path(upload_id)
            assert physical_file.read_bytes() == b"hello world!"

    anyio.run(_run)


def test_real_tusclient_over_http_server(tmp_path: Path) -> None:
    port = _free_port()
    config = AppConfig()
    config.backend.sqlite_path = str(tmp_path / "test.db")
    config.server.host = "127.0.0.1"
    config.server.port = port
    config.upload.base_dir = str(tmp_path / "uploads")

    mcp = build_mcp(config)
    app = _build_streamable_http_app(mcp, None, config)

    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=config.server.host,
            port=port,
            log_level="error",
            timeout_graceful_shutdown=0,
        )
    )
    thread = Thread(target=server.run, daemon=True)
    thread.start()
    _wait_for_port(port)

    try:
        # Pre-create upload session via UploadManager
        payload = b"CHUNK-A" * 500 + b"CHUNK-B" * 500  # 7000 bytes
        real_sha256 = hashlib.sha256(payload).hexdigest()
        size = len(payload)

        service = mcp._rmg_service
        session = service.upload_manager.create_upload(
            filename="large_data.bin",
            size_bytes=size,
            sha256=real_sha256,
        )

        upload_url = f"http://127.0.0.1:{port}/uploads/{session.upload_id}"

        # Upload via tuspy client with 1024-byte chunk size
        client = tus_client.TusClient(f"http://127.0.0.1:{port}/uploads/")
        uploader = client.uploader(
            file_stream=io.BytesIO(payload),
            url=upload_url,
            chunk_size=1024,
        )
        uploader.upload()

        # Check status in manager
        status = service.upload_manager.get_upload_status(session.upload_id)
        assert status["completed"]
        assert status["received_bytes"] == size

        # Finalize and verify blob
        blob_path = service.upload_manager.verify_and_finalize(session.upload_id)
        assert blob_path.exists()
        assert blob_path.read_bytes() == payload
        assert sha256_file(blob_path) == real_sha256
    finally:
        server.should_exit = True
        thread.join(timeout=3.0)


def test_uploads_bearer_auth_protection(tmp_path: Path) -> None:
    port = _free_port()
    config = AppConfig()
    config.backend.sqlite_path = str(tmp_path / "test.db")
    config.server.host = "127.0.0.1"
    config.server.port = port
    config.upload.base_dir = str(tmp_path / "uploads")

    token = "secret-token-12345"
    mcp = build_mcp(config)
    app = _build_streamable_http_app(mcp, token, config)

    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=config.server.host,
            port=port,
            log_level="error",
            timeout_graceful_shutdown=0,
        )
    )
    thread = Thread(target=server.run, daemon=True)
    thread.start()
    _wait_for_port(port)

    try:
        # 1. OPTIONS should pass through without token (CORS / capability check)
        r_opt = httpx.options(f"http://127.0.0.1:{port}/uploads/")
        assert r_opt.status_code == 204
        assert r_opt.headers.get("Tus-Resumable") == "1.0.0"

        # 2. POST without token should be rejected 401
        r_unauth = httpx.post(
            f"http://127.0.0.1:{port}/uploads/",
            headers={"Tus-Resumable": "1.0.0", "Upload-Length": "10"},
        )
        assert r_unauth.status_code == 401

        # 3. POST with correct Bearer token succeeds
        r_auth = httpx.post(
            f"http://127.0.0.1:{port}/uploads/",
            headers={
                "Authorization": f"Bearer {token}",
                "Tus-Resumable": "1.0.0",
                "Upload-Length": "10",
            },
        )
        assert r_auth.status_code == 201
    finally:
        server.should_exit = True
        thread.join(timeout=3.0)
