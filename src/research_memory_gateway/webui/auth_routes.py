from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from ..config import AppConfig


def _b64_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("utf-8").rstrip("=")


def _b64_decode(data: str) -> bytes:
    padding = "=" * (4 - len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def generate_jwt(payload: dict[str, Any], secret: str) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    header_part = _b64_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    payload_part = _b64_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))

    signature_base = f"{header_part}.{payload_part}".encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), signature_base, hashlib.sha256).digest()
    signature_part = _b64_encode(signature)

    return f"{header_part}.{payload_part}.{signature_part}"


def verify_jwt(token: str, secret: str) -> dict[str, Any] | None:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        header_part, payload_part, signature_part = parts

        signature_base = f"{header_part}.{payload_part}".encode("utf-8")
        expected_signature = hmac.new(secret.encode("utf-8"), signature_base, hashlib.sha256).digest()

        if not hmac.compare_digest(_b64_decode(signature_part), expected_signature):
            return None

        payload = json.loads(_b64_decode(payload_part).decode("utf-8"))

        if payload.get("exp", 0) < time.time():
            return None

        return payload
    except Exception:
        return None


class SessionManager:
    def __init__(self, config: AppConfig) -> None:
        self.max_age = config.webui.session_max_age_seconds
        self.signing_key = os.getenv("WEBUI_SESSION_KEY") or hashlib.sha256(
            f"{config.webui.auth_store_path}:{config.webui.port}".encode("utf-8")
        ).hexdigest()
        self.invalid_after = 0.0
        self.active_refresh_jtis: set[str] = set()

    def create(self) -> dict[str, str]:
        now = time.time()
        sid = secrets.token_urlsafe(24)
        csrf = secrets.token_urlsafe(24)
        payload = json.dumps({"sid": sid, "csrf": csrf, "exp": now + self.max_age, "iat": now}, separators=(",", ":"))
        sig = hmac.new(self.signing_key.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
        return {"cookie": f"{payload}.{sig}", "csrf": csrf}

    def current(self, request: Request) -> dict[str, Any] | None:
        raw = None
        auth_header = request.headers.get("authorization")
        if auth_header and auth_header.startswith("Bearer "):
            raw = auth_header[7:]
        else:
            raw = request.cookies.get("webui_session")

        if not raw:
            return None

        payload = verify_jwt(raw, self.signing_key)
        if not payload:
            if "." in raw:
                try:
                    payload_part, sig = raw.rsplit(".", 1)
                    expected = hmac.new(self.signing_key.encode("utf-8"), payload_part.encode("utf-8"), hashlib.sha256).hexdigest()
                    if hmac.compare_digest(expected, sig):
                        payload = json.loads(payload_part)
                except Exception:
                    pass

        if not payload:
            return None

        now = time.time()
        if payload.get("exp", 0) < now or payload.get("iat", 0) < self.invalid_after:
            return None
        return payload

    def csrf(self, request: Request) -> str:
        session = self.current(request)
        return session.get("csrf", "") if session else ""

    def verify_csrf(self, request: Request) -> bool:
        if request.url.path in {"/admin/login", "/admin/api/auth/login"}:
            return True
        auth_header = request.headers.get("authorization")
        if auth_header and auth_header.startswith("Bearer "):
            return True

        session = self.current(request)
        if not session:
            return False
        token = request.headers.get("x-csrf-token") or request.query_params.get("csrf_token")
        return bool(token) and hmac.compare_digest(token, session.get("csrf", ""))

    def invalidate_all(self) -> None:
        self.invalid_after = time.time()
        self.active_refresh_jtis.clear()


async def api_security_password(request: Request) -> Response:
    state = request.app.state.webui
    content_type = request.headers.get("content-type", "")
    payload = await _form(request) if content_type.startswith("application/x-www-form-urlencoded") else await request.json()
    try:
        state.auth_store.change_password(payload.get("current_password", ""), payload.get("new_password", ""))
    except (PermissionError, RuntimeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    state.sessions.invalidate_all()
    state.service.append_audit_event("security.password_changed")
    response = JSONResponse({"changed": True})
    response.delete_cookie("webui_session")
    return response


async def api_login(request: Request) -> Response:
    state = request.app.state.webui
    payload = await request.json()
    password = payload.get("password", "")
    if not state.auth_store.verify(password, state.config.webui):
        return JSONResponse({"error": "invalid_password"}, status_code=401)

    now = time.time()
    access_jti = secrets.token_urlsafe(16)
    refresh_jti = secrets.token_urlsafe(16)
    csrf_token = secrets.token_urlsafe(24)

    access_token = generate_jwt(
        {
            "sub": "admin",
            "iat": now,
            "exp": now + 1800,
            "jti": access_jti,
            "csrf": csrf_token,
        },
        state.sessions.signing_key,
    )

    refresh_token = generate_jwt(
        {
            "sub": "admin",
            "iat": now,
            "exp": now + state.config.webui.session_max_age_seconds,
            "jti": refresh_jti,
        },
        state.sessions.signing_key,
    )

    state.sessions.active_refresh_jtis.add(refresh_jti)

    response = JSONResponse(
        {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_in": 1800,
        }
    )
    response.set_cookie(
        "webui_session",
        access_token,
        httponly=True,
        samesite="lax",
        max_age=1800,
    )
    return response


async def api_refresh(request: Request) -> Response:
    state = request.app.state.webui
    payload = await request.json()
    refresh_token = payload.get("refresh_token", "")

    token_data = verify_jwt(refresh_token, state.sessions.signing_key)
    if not token_data or token_data.get("jti") not in state.sessions.active_refresh_jtis:
        return JSONResponse({"error": "invalid_refresh_token"}, status_code=401)

    now = time.time()
    access_jti = secrets.token_urlsafe(16)
    csrf_token = secrets.token_urlsafe(24)

    access_token = generate_jwt(
        {
            "sub": "admin",
            "iat": now,
            "exp": now + 1800,
            "jti": access_jti,
            "csrf": csrf_token,
        },
        state.sessions.signing_key,
    )

    response = JSONResponse(
        {
            "access_token": access_token,
            "expires_in": 1800,
        }
    )
    response.set_cookie(
        "webui_session",
        access_token,
        httponly=True,
        samesite="lax",
        max_age=1800,
    )
    return response


async def api_logout(request: Request) -> Response:
    state = request.app.state.webui
    payload = await _json_or_empty(request)
    refresh_token = payload.get("refresh_token", "")

    if refresh_token:
        token_data = verify_jwt(refresh_token, state.sessions.signing_key)
        if token_data:
            state.sessions.active_refresh_jtis.discard(token_data.get("jti"))

    response = JSONResponse({"logged_out": True})
    response.delete_cookie("webui_session")
    return response


async def api_list_keys(request: Request) -> Response:
    state = request.app.state.webui
    with state.service.backend._connect() as conn:
        rows = conn.execute("SELECT key_id, name, created_at, last_used_at, status FROM api_keys ORDER BY created_at DESC").fetchall()
    return JSONResponse({"items": [dict(r) for r in rows]})


async def api_create_key(request: Request) -> Response:
    state = request.app.state.webui
    payload = await request.json()
    name = payload.get("name", "").strip()
    if not name:
        return JSONResponse({"error": "name_required"}, status_code=400)

    custom_key = payload.get("custom_key", "").strip()
    key_id = f"key_{secrets.token_hex(8)}"

    if custom_key:
        raw_key = custom_key
    else:
        raw_key = f"rmg_{secrets.token_hex(16)}"

    key_hash = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
    created_at = datetime.now(timezone.utc).isoformat()

    with state.service.backend._connect() as conn:
        conn.execute(
            "INSERT INTO api_keys(key_id, name, key_hash, created_at, status) VALUES (?, ?, ?, ?, 'active')",
            (key_id, name, key_hash, created_at),
        )
    state.service.append_audit_event("security.api_key_created", metadata={"key_id": key_id, "name": name})
    return JSONResponse(
        {
            "key_id": key_id,
            "name": name,
            "api_key": raw_key,
            "created_at": created_at,
            "status": "active",
        },
        status_code=201,
    )


async def api_delete_key(request: Request) -> Response:
    state = request.app.state.webui
    key_id = request.path_params["key_id"]

    with state.service.backend._connect() as conn:
        cursor = conn.execute("DELETE FROM api_keys WHERE key_id = ?", (key_id,))
        deleted = cursor.rowcount > 0

    if not deleted:
        return JSONResponse({"error": "not_found"}, status_code=404)

    state.service.append_audit_event("security.api_key_revoked", metadata={"key_id": key_id})
    return JSONResponse({"deleted": True})


async def api_key_usage(request: Request) -> Response:
    state = request.app.state.webui
    key_id = request.path_params["key_id"]
    with state.service.backend._connect() as conn:
        rows = conn.execute(
            "SELECT client_ip, client_info, request_count, last_request_at FROM active_connections WHERE key_id = ?",
            (key_id,),
        ).fetchall()
    return JSONResponse({"connections": [dict(r) for r in rows]})


async def api_list_connections(request: Request) -> Response:
    state = request.app.state.webui
    sql = """
        SELECT c.key_id, k.name as key_name, c.client_ip, c.client_info, c.request_count, c.last_request_at
        FROM active_connections c
        LEFT JOIN api_keys k ON c.key_id = k.key_id
        ORDER BY c.last_request_at DESC
    """
    with state.service.backend._connect() as conn:
        rows = conn.execute(sql).fetchall()
    return JSONResponse({"items": [dict(r) for r in rows]})


def security_routes() -> list[Route]:
    return [
        Route("/admin/api/security/password", api_security_password, methods=["POST"]),
        Route("/admin/api/auth/login", api_login, methods=["POST"]),
        Route("/admin/api/auth/refresh", api_refresh, methods=["POST"]),
        Route("/admin/api/auth/logout", api_logout, methods=["POST"]),
        Route("/admin/api/security/api-keys", api_list_keys, methods=["GET"]),
        Route("/admin/api/security/api-keys", api_create_key, methods=["POST"]),
        Route("/admin/api/security/api-keys/{key_id:str}", api_delete_key, methods=["DELETE"]),
        Route("/admin/api/security/api-keys/{key_id:str}/usage", api_key_usage, methods=["GET"]),
        Route("/admin/api/security/connections", api_list_connections, methods=["GET"]),
    ]


async def _json_or_empty(request: Request) -> dict[str, Any]:
    try:
        return await request.json()
    except json.JSONDecodeError:
        return {}


async def _form(request: Request) -> dict[str, str]:
    body = (await request.body()).decode("utf-8")
    return {key: values[-1] for key, values in parse_qs(body).items()}
