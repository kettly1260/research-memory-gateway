"""Persistent OAuth authorization-code/PKCE support for the MCP HTTP surface."""
from __future__ import annotations

import base64
import binascii
import hashlib
import html
import json
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import unquote_plus, urlsplit

from mcp.server.auth.provider import AccessToken, construct_redirect_uri
from mcp.server.transport_security import RequestBodyLimitMiddleware
from mcp.shared.auth import OAuthClientMetadata
from pydantic import ValidationError
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route, request_response

from .config import AppConfig, AuthStore


SCOPE = "mcp"
COOKIE = "rmg_oauth_consent"
PUBLIC_PATHS = {
    "/.well-known/oauth-authorization-server",
    "/.well-known/oauth-protected-resource",
    "/.well-known/oauth-protected-resource/mcp",
    "/authorize", "/token", "/register", "/revoke", "/oauth/consent",
}


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class OAuthError(ValueError):
    def __init__(self, error: str, description: str, status: int = 400) -> None:
        super().__init__(description)
        self.error, self.description, self.status = error, description, status


def validate_redirects(value: Any) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= 20:
        raise ValueError("redirect_uris must contain 1 to 20 URLs")
    for uri in value:
        if not isinstance(uri, str) or len(uri) > 2048 or any(
            c.isspace() or ord(c) < 32 or c in "\\#" for c in uri
        ):
            raise ValueError("Invalid redirect URI")
        parsed = urlsplit(uri)
        if (
            not parsed.hostname or parsed.username is not None or parsed.password is not None
            or parsed.fragment or parsed.scheme not in {"http", "https"}
            or (parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"})
        ):
            raise ValueError("Redirect URI must use HTTPS or loopback HTTP, without credentials or fragment")
        parsed.port
    if len(set(value)) != len(value):
        raise ValueError("Redirect URIs must be unique")
    return value


class OAuthService:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.settings = config.oauth
        self.issuer = config.oauth.public_url
        self.resource = self.issuer + "/mcp"
        self.path = Path(config.oauth.store_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS oauth_clients (
                    client_id TEXT PRIMARY KEY, metadata TEXT NOT NULL,
                    secret_hash TEXT, status TEXT NOT NULL DEFAULT 'active',
                    created_at INTEGER NOT NULL, last_used_at INTEGER
                );
                CREATE TABLE IF NOT EXISTS oauth_artifacts (
                    digest TEXT PRIMARY KEY, kind TEXT NOT NULL, client_id TEXT NOT NULL,
                    family_id TEXT, data TEXT NOT NULL, expires_at INTEGER NOT NULL,
                    consumed INTEGER NOT NULL DEFAULT 0, revoked INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS oauth_artifacts_client ON oauth_artifacts(client_id);
                CREATE INDEX IF NOT EXISTS oauth_artifacts_family ON oauth_artifacts(family_id);
                CREATE TABLE IF NOT EXISTS oauth_login_limits (
                    address_hash TEXT PRIMARY KEY, attempts INTEGER NOT NULL, reset_at INTEGER NOT NULL
                );
            """)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _client(self, conn: sqlite3.Connection, client_id: str, *, active: bool = True) -> dict[str, Any]:
        row = conn.execute("SELECT * FROM oauth_clients WHERE client_id = ?", (client_id,)).fetchone()
        if not row or (active and row["status"] != "active"):
            raise OAuthError("invalid_client", "Unknown or disabled client", 401)
        return {**dict(row), "metadata": json.loads(row["metadata"])}

    def create_client(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            redirects = validate_redirects(payload.get("redirect_uris"))
            metadata = OAuthClientMetadata.model_validate(payload).model_dump(mode="json", exclude_none=True)
            name = payload.get("client_name", "MCP Client")
            if not isinstance(name, str) or not name.strip() or len(name) > 200:
                raise ValueError("client_name must contain 1 to 200 characters")
            method = payload.get("token_endpoint_auth_method") or "none"
            if method not in {"none", "client_secret_post", "client_secret_basic"}:
                raise ValueError("Unsupported token endpoint authentication method")
            grants = payload.get("grant_types", ["authorization_code", "refresh_token"])
            if not isinstance(grants, list) or "authorization_code" not in grants or set(grants) - {
                "authorization_code", "refresh_token"
            }:
                raise ValueError("Only authorization_code and refresh_token grants are supported")
            if payload.get("response_types", ["code"]) != ["code"]:
                raise ValueError("Only the code response type is supported")
            if payload.get("scope", SCOPE) != SCOPE:
                raise ValueError("Only the mcp scope is supported")
        except (ValueError, ValidationError) as exc:
            raise OAuthError("invalid_client_metadata", str(exc)) from exc
        metadata.update(
            redirect_uris=redirects, client_name=name.strip(), token_endpoint_auth_method=method,
            grant_types=grants, response_types=["code"], scope=SCOPE,
        )
        client_id = "rmg_client_" + secrets.token_urlsafe(18)
        secret = secrets.token_urlsafe(32) if method != "none" else None
        now = int(time.time())
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT COUNT(*) FROM oauth_clients").fetchone()[0] >= self.settings.max_clients:
                raise OAuthError("invalid_client_metadata", "Client registration limit reached", 429)
            conn.execute(
                "INSERT INTO oauth_clients(client_id, metadata, secret_hash, created_at) VALUES (?, ?, ?, ?)",
                (client_id, json.dumps(metadata), _digest(secret) if secret else None, now),
            )
        result = {**metadata, "client_id": client_id, "client_id_issued_at": now}
        if secret:
            result.update(client_secret=secret, client_secret_expires_at=0)
        return result

    def list_clients(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("""
                SELECT c.*, (SELECT COUNT(DISTINCT family_id) FROM oauth_artifacts a
                WHERE a.client_id=c.client_id AND a.kind IN ('access','refresh')
                AND a.revoked=0 AND a.consumed=0 AND a.expires_at>?) AS active_grants
                FROM oauth_clients c ORDER BY created_at DESC, client_id
            """, (int(time.time()),)).fetchall()
        return [
            {**json.loads(row["metadata"]), **{key: row[key] for key in (
                "client_id", "status", "created_at", "last_used_at", "active_grants"
            )}}
            for row in rows
        ]

    def update_client(self, client_id: str, payload: dict[str, Any]) -> None:
        if not payload or set(payload) - {"client_name", "status"}:
            raise ValueError("Only client_name and status can be changed")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            client = self._client(conn, client_id, active=False)
            metadata = client["metadata"]
            if "client_name" in payload:
                name = payload["client_name"]
                if not isinstance(name, str) or not name.strip() or len(name) > 200:
                    raise ValueError("client_name must contain 1 to 200 characters")
                metadata["client_name"] = name.strip()
            status = payload.get("status", client["status"])
            if status not in {"active", "disabled"}:
                raise ValueError("status must be active or disabled")
            conn.execute("UPDATE oauth_clients SET metadata=?, status=? WHERE client_id=?",
                         (json.dumps(metadata), status, client_id))
            if status == "disabled":
                conn.execute("UPDATE oauth_artifacts SET revoked=1 WHERE client_id=?", (client_id,))

    def revoke_client(self, client_id: str, *, delete: bool = False) -> None:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._client(conn, client_id, active=False)
            conn.execute("UPDATE oauth_artifacts SET revoked=1 WHERE client_id=?", (client_id,))
            if delete:
                conn.execute("DELETE FROM oauth_artifacts WHERE client_id=?", (client_id,))
                conn.execute("DELETE FROM oauth_clients WHERE client_id=?", (client_id,))

    def _save(self, conn: sqlite3.Connection, kind: str, client_id: str, data: dict[str, Any],
              expires: int, family: str | None = None) -> str:
        raw = "rmg_" + kind + "_" + secrets.token_urlsafe(32)
        conn.execute(
            "INSERT INTO oauth_artifacts(digest,kind,client_id,family_id,data,expires_at) VALUES (?,?,?,?,?,?)",
            (_digest(raw), kind, client_id, family, json.dumps(data), expires),
        )
        return raw

    def _artifact(self, conn: sqlite3.Connection, raw: str, kind: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM oauth_artifacts WHERE digest=? AND kind=?", (_digest(raw), kind)).fetchone()
        if not row or row["revoked"] or row["expires_at"] <= time.time():
            raise OAuthError("invalid_grant", "Expired or invalid credential")
        return row

    def begin_authorization(self, params: dict[str, str]) -> str:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            client = self._client(conn, params.get("client_id", ""))
            redirects = client["metadata"]["redirect_uris"]
            redirect = params.get("redirect_uri")
            if not redirect and len(redirects) == 1:
                redirect = redirects[0]
            if redirect not in redirects:
                raise OAuthError("invalid_request", "redirect_uri must exactly match a registered URI")
            if params.get("response_type") != "code":
                raise OAuthError("unsupported_response_type", "Only code is supported")
            if params.get("code_challenge_method") != "S256" or not re.fullmatch(
                r"[A-Za-z0-9_-]{43}", params.get("code_challenge", "")
            ):
                raise OAuthError("invalid_request", "A valid S256 PKCE challenge is required")
            self._resource(params.get("resource"))
            if params.get("scope", SCOPE) != SCOPE:
                raise OAuthError("invalid_scope", "Only mcp scope is supported")
            now = int(time.time())
            conn.execute("DELETE FROM oauth_artifacts WHERE expires_at<=?", (now,))
            if conn.execute("SELECT COUNT(*) FROM oauth_artifacts WHERE kind='pending'").fetchone()[0] >= 2000:
                raise OAuthError("temporarily_unavailable", "Too many pending authorizations", 429)
            return self._save(conn, "pending", client["client_id"], {
                "redirect_uri": redirect, "explicit_redirect": "redirect_uri" in params,
                "state": params.get("state"), "challenge": params["code_challenge"],
                "resource": self.resource,
            }, now + 600)

    def _resource(self, value: str | None) -> None:
        if value is not None and value != self.resource:
            raise OAuthError("invalid_target", "Resource must match this MCP server")

    def prepare_consent(self, pending: str) -> tuple[dict[str, Any], str]:
        browser_secret = secrets.token_urlsafe(32)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._artifact(conn, pending, "pending")
            if row["consumed"]:
                raise OAuthError("invalid_grant", "Authorization request already used")
            client = self._client(conn, row["client_id"])
            data = json.loads(row["data"])
            self._resource(data.get("resource", ""))
            data["browser_hash"] = _digest(browser_secret)
            conn.execute("UPDATE oauth_artifacts SET data=? WHERE digest=?", (json.dumps(data), row["digest"]))
            return {**data, "client_name": client["metadata"]["client_name"]}, browser_secret

    def finish_consent(self, pending: str, browser_secret: str, csrf: str,
                       password: str, approved: bool, address: str) -> str:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._artifact(conn, pending, "pending")
            data = json.loads(row["data"])
            self._resource(data.get("resource", ""))
            self._client(conn, row["client_id"])
            if row["consumed"] or not browser_secret or not secrets.compare_digest(
                browser_secret, csrf
            ) or not secrets.compare_digest(_digest(browser_secret), data.get("browser_hash", "")):
                raise OAuthError("access_denied", "Invalid or expired browser confirmation", 403)
        # Rate limiting is committed even when password verification fails.
        if approved:
            with self.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                now = int(time.time())
                conn.execute("DELETE FROM oauth_login_limits WHERE reset_at<=?", (now,))
                key = _digest(address)
                limit = conn.execute("SELECT attempts FROM oauth_login_limits WHERE address_hash=?", (key,)).fetchone()
                if limit and limit["attempts"] >= 10:
                    raise OAuthError("access_denied", "Too many password attempts; try again in 15 minutes", 429)
                conn.execute("""
                    INSERT INTO oauth_login_limits(address_hash,attempts,reset_at) VALUES (?,1,?)
                    ON CONFLICT(address_hash) DO UPDATE SET attempts=attempts+1
                """, (key, now + 900))
            if not AuthStore(self.config.webui.auth_store_path).verify(password, self.config.webui):
                raise OAuthError("access_denied", "Incorrect administrator password", 401)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._artifact(conn, pending, "pending")
            self._client(conn, row["client_id"])
            if row["consumed"] or json.loads(row["data"]).get("browser_hash") != _digest(browser_secret):
                raise OAuthError("invalid_grant", "Authorization request already used or replaced")
            conn.execute("UPDATE oauth_artifacts SET consumed=1 WHERE digest=?", (row["digest"],))
            if not approved:
                return construct_redirect_uri(data["redirect_uri"], error="access_denied", state=data.get("state"))
            conn.execute("DELETE FROM oauth_login_limits WHERE address_hash=?", (_digest(address),))
            code = self._save(conn, "code", row["client_id"], data, int(time.time()) + 300)
            return construct_redirect_uri(data["redirect_uri"], code=code, state=data.get("state"), iss=self.issuer)

    def authenticate_client(self, params: dict[str, str], authorization: str) -> dict[str, Any]:
        client_id = params.get("client_id", "")
        secret = params.get("client_secret", "")
        method = "client_secret_post" if secret else "none"
        if authorization:
            if not authorization.startswith("Basic ") or secret:
                raise OAuthError("invalid_client", "Invalid client authentication", 401)
            try:
                basic_id, secret = base64.b64decode(authorization[6:], validate=True).decode().split(":", 1)
                basic_id, secret = unquote_plus(basic_id), unquote_plus(secret)
            except (ValueError, UnicodeError, binascii.Error) as exc:
                raise OAuthError("invalid_client", "Invalid Basic credentials", 401) from exc
            if client_id and client_id != basic_id:
                raise OAuthError("invalid_client", "Client ID mismatch", 401)
            client_id, method = basic_id, "client_secret_basic"
        with self.connect() as conn:
            client = self._client(conn, client_id)
        if client["metadata"]["token_endpoint_auth_method"] != method or (
            client["secret_hash"] and not secrets.compare_digest(client["secret_hash"], _digest(secret))
        ):
            raise OAuthError("invalid_client", "Invalid client credentials", 401)
        return client

    def _mint(self, conn: sqlite3.Connection, client_id: str, family: str,
              refresh_expiry: int, issue_refresh: bool) -> dict[str, Any]:
        now = int(time.time())
        access = self._save(conn, "access", client_id, {"resource": self.resource},
                            now + self.settings.access_token_seconds, family)
        result: dict[str, Any] = {
            "access_token": access, "token_type": "Bearer",
            "expires_in": self.settings.access_token_seconds, "scope": SCOPE,
        }
        if issue_refresh:
            result["refresh_token"] = self._save(conn, "refresh", client_id, {
                "resource": self.resource,
            }, refresh_expiry, family)
        conn.execute("UPDATE oauth_clients SET last_used_at=? WHERE client_id=?", (now, client_id))
        return result

    def exchange(self, client: dict[str, Any], params: dict[str, str]) -> dict[str, Any]:
        self._resource(params.get("resource"))
        grant = params.get("grant_type")
        if grant not in {"authorization_code", "refresh_token"} or grant not in client["metadata"]["grant_types"]:
            raise OAuthError("unsupported_grant_type", "Grant type is not enabled for this client")
        kind = "code" if grant == "authorization_code" else "refresh"
        raw = params.get("code" if kind == "code" else "refresh_token", "")
        reused = False
        result: dict[str, Any] = {}
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._client(conn, client["client_id"])
            row = self._artifact(conn, raw, kind)
            if row["client_id"] != client["client_id"]:
                raise OAuthError("invalid_grant", "Credential belongs to a different client")
            data = json.loads(row["data"])
            self._resource(data.get("resource", ""))
            if row["consumed"]:
                if row["family_id"]:
                    conn.execute("UPDATE oauth_artifacts SET revoked=1 WHERE family_id=?", (row["family_id"],))
                reused = True
            elif kind == "code":
                expected_redirect = data["redirect_uri"] if data["explicit_redirect"] else None
                if params.get("redirect_uri") != expected_redirect:
                    raise OAuthError("invalid_grant", "Redirect URI does not match authorization request")
                verifier = params.get("code_verifier", "")
                if not re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", verifier):
                    raise OAuthError("invalid_grant", "Invalid PKCE verifier")
                challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
                if not secrets.compare_digest(challenge, data["challenge"]):
                    raise OAuthError("invalid_grant", "Incorrect PKCE verifier")
                family = secrets.token_urlsafe(24)
                conn.execute("UPDATE oauth_artifacts SET consumed=1,family_id=? WHERE digest=?", (family, row["digest"]))
                result = self._mint(conn, client["client_id"], family,
                                    int(time.time()) + self.settings.refresh_token_seconds,
                                    "refresh_token" in client["metadata"]["grant_types"])
            else:
                if params.get("scope", SCOPE) != SCOPE:
                    raise OAuthError("invalid_scope", "Cannot expand or change the granted scope")
                conn.execute("UPDATE oauth_artifacts SET consumed=1 WHERE digest=?", (row["digest"],))
                result = self._mint(conn, client["client_id"], row["family_id"], row["expires_at"], True)
        if reused:
            raise OAuthError("invalid_grant", "Credential reuse detected; authorization family revoked")
        return result

    def verify_access_token(self, raw: str) -> AccessToken | None:
        try:
            with self.connect() as conn:
                row = self._artifact(conn, raw, "access")
                self._client(conn, row["client_id"])
                if row["consumed"] or json.loads(row["data"])["resource"] != self.resource:
                    return None
                conn.execute("UPDATE oauth_clients SET last_used_at=? WHERE client_id=?",
                             (int(time.time()), row["client_id"]))
                return AccessToken(token=raw, client_id=row["client_id"], scopes=[SCOPE],
                                   expires_at=row["expires_at"], resource=self.resource,
                                   subject="admin", claims={"iss": self.issuer})
        except OAuthError:
            return None

    def revoke_token(self, client_id: str, token: str) -> None:
        with self.connect() as conn:
            row = conn.execute("SELECT family_id FROM oauth_artifacts WHERE digest=? AND client_id=? AND kind IN ('access','refresh')",
                               (_digest(token), client_id)).fetchone()
            if row:
                conn.execute("UPDATE oauth_artifacts SET revoked=1 WHERE family_id=?", (row["family_id"],))


def _error(exc: OAuthError) -> JSONResponse:
    headers = {"Cache-Control": "no-store", "Pragma": "no-cache"}
    if exc.error == "invalid_client" and exc.status == 401:
        headers["WWW-Authenticate"] = 'Basic realm="mcp"'
    return JSONResponse({"error": exc.error, "error_description": exc.description}, status_code=exc.status,
                        headers=headers)


async def _params(request: Request) -> dict[str, str]:
    values = request.query_params if request.method == "GET" else await request.form()
    if len(values) > 30 or len(values.multi_items()) != len(values):
        raise OAuthError("invalid_request", "Duplicate parameters are not supported")
    if any(not isinstance(value, str) or len(value) > 4096 for value in values.values()):
        raise OAuthError("invalid_request", "Invalid or oversized parameters")
    return dict(values)


def _consent_page(service: OAuthService, pending: str, data: dict[str, Any], csrf: str,
                  error: str = "") -> HTMLResponse:
    escape = html.escape
    body = f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>MCP OAuth Authorization</title>
<style>body{{font:16px system-ui;background:#f4f6f7;color:#17212b;margin:0;padding:24px}}
main{{max-width:480px;margin:8vh auto}}h1{{font-size:24px;overflow-wrap:anywhere}}.field{{margin:20px 0}}
input{{box-sizing:border-box;width:100%;padding:12px;border:1px solid #adb8c2;border-radius:4px;font:inherit}}
button{{font:inherit;padding:10px 20px;border:1px solid #adb8c2;border-radius:4px;cursor:pointer}}
button[value=approve]{{background:#087e70;color:white;border-color:#087e70}}p{{overflow-wrap:anywhere}}
.error{{color:#b42318}}.actions{{display:flex;gap:12px}}label{{display:block;margin-bottom:8px}}</style>
<main><h1>授权 {escape(data["client_name"])}</h1><p>MCP: {escape(service.resource)}</p>
<p>权限：使用网关当前配置的 MCP 工具</p><p>回调地址：{escape(data["redirect_uri"])}</p>
<p class="error" role="alert">{escape(error)}</p>
<form method="post" action="/oauth/consent">
<input type="hidden" name="request" value="{escape(pending, quote=True)}">
<input type="hidden" name="csrf" value="{escape(csrf, quote=True)}">
<div class="field"><label for="password">管理员密码</label>
<input id="password" name="password" type="password" autocomplete="current-password" maxlength="1024"></div>
<div class="actions"><button name="decision" value="approve" type="submit">允许访问</button>
<button name="decision" value="deny" type="submit">拒绝</button></div></form></main></html>"""
    return HTMLResponse(body, headers={
        "Cache-Control": "no-store", "Pragma": "no-cache", "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
        # Chromium applies form-action to the 303 callback, which is validated exactly server-side.
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'",
    })


def oauth_routes(service: OAuthService) -> list[Route]:
    async def metadata(request: Request) -> Response:
        base = service.issuer
        data = {
            "issuer": base, "authorization_endpoint": base + "/authorize", "token_endpoint": base + "/token",
            "revocation_endpoint": base + "/revoke", "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"], "scopes_supported": [SCOPE],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none", "client_secret_post", "client_secret_basic"],
        }
        if service.settings.dynamic_registration:
            data["registration_endpoint"] = base + "/register"
        return JSONResponse(data)

    async def resource_metadata(request: Request) -> Response:
        return JSONResponse({"resource": service.resource, "authorization_servers": [service.issuer],
                             "scopes_supported": [SCOPE], "bearer_methods_supported": ["header"],
                             "resource_name": "Research Memory Gateway"})

    async def register(request: Request) -> Response:
        if not service.settings.dynamic_registration:
            return JSONResponse({"error": "registration_disabled"}, status_code=403)
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise OAuthError("invalid_client_metadata", "Expected a JSON object")
            return JSONResponse(service.create_client(payload), status_code=201, headers={"Cache-Control": "no-store"})
        except json.JSONDecodeError:
            return _error(OAuthError("invalid_client_metadata", "Invalid JSON"))
        except OAuthError as exc:
            return _error(exc)

    async def authorize(request: Request) -> Response:
        try:
            pending = service.begin_authorization(await _params(request))
            return RedirectResponse("/oauth/consent?request=" + pending, status_code=302,
                                    headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
        except OAuthError as exc:
            return _error(exc)

    async def consent(request: Request) -> Response:
        pending = request.query_params.get("request", "")
        error = ""
        try:
            if request.method == "POST":
                params = await _params(request)
                pending = params.get("request", "")
                if params.get("decision") not in {"approve", "deny"}:
                    raise OAuthError("invalid_request", "Unknown consent decision")
                try:
                    redirect = service.finish_consent(
                        pending, request.cookies.get(COOKIE, ""), params.get("csrf", ""),
                        params.get("password", ""), params["decision"] == "approve",
                        request.client.host if request.client else "unknown",
                    )
                    response = RedirectResponse(redirect, status_code=303, headers={
                        "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                    })
                    response.delete_cookie(COOKIE, path="/oauth/consent")
                    return response
                except OAuthError as exc:
                    if exc.status not in {401, 429}:
                        raise
                    error = exc.description
            data, browser_secret = service.prepare_consent(pending)
            response = _consent_page(service, pending, data, browser_secret, error)
            response.set_cookie(COOKIE, browser_secret, max_age=600, httponly=True,
                                secure=service.issuer.startswith("https://"), samesite="strict", path="/oauth/consent")
            return response
        except OAuthError as exc:
            return _error(exc)

    async def token(request: Request) -> Response:
        try:
            params = await _params(request)
            client = service.authenticate_client(params, request.headers.get("authorization", ""))
            return JSONResponse(service.exchange(client, params), headers={"Cache-Control": "no-store", "Pragma": "no-cache"})
        except OAuthError as exc:
            return _error(exc)

    async def revoke(request: Request) -> Response:
        try:
            params = await _params(request)
            client = service.authenticate_client(params, request.headers.get("authorization", ""))
            service.revoke_token(client["client_id"], params.get("token", ""))
            return Response(status_code=200, headers={"Cache-Control": "no-store"})
        except OAuthError as exc:
            return _error(exc)

    def cors(handler: Any, methods: list[str]) -> Any:
        return CORSMiddleware(request_response(handler), allow_origins=["*"], allow_methods=methods,
                              allow_headers=["Authorization", "Content-Type", "MCP-Protocol-Version"])

    routes = [
        Route("/.well-known/oauth-authorization-server", cors(metadata, ["GET"]), methods=["GET", "OPTIONS"]),
        Route("/.well-known/oauth-protected-resource/mcp", cors(resource_metadata, ["GET"]), methods=["GET", "OPTIONS"]),
        Route("/.well-known/oauth-protected-resource", cors(resource_metadata, ["GET"]), methods=["GET", "OPTIONS"]),
        Route("/register", cors(register, ["POST"]), methods=["POST", "OPTIONS"]),
        Route("/authorize", authorize, methods=["GET", "POST"]),
        Route("/oauth/consent", consent, methods=["GET", "POST"]),
        Route("/token", cors(token, ["POST"]), methods=["POST", "OPTIONS"]),
        Route("/revoke", cors(revoke, ["POST"]), methods=["POST", "OPTIONS"]),
    ]
    for route in routes:
        route.app = RequestBodyLimitMiddleware(route.app, 65536)
    return routes
