from __future__ import annotations

import base64
import hashlib
import re
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlsplit

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from research_memory_gateway.config import AppConfig, AuthStore, OAuthConfig
from research_memory_gateway.oauth import COOKIE, OAuthError, OAuthService, oauth_routes
from research_memory_gateway.server import BearerAuthMiddleware
from research_memory_gateway.webui.app import build_webui_app


VERIFIER = "a" * 64
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=").decode()
REDIRECT = "http://127.0.0.1:3000/callback"


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.delenv("WEBUI_PASSWORD_HASH", raising=False)
    config = AppConfig()
    config.oauth = OAuthConfig(enabled=True, public_url="http://127.0.0.1:8787", store_path=str(tmp_path / "oauth.db"))
    config.backend.sqlite_path = str(tmp_path / "memory.db")
    config.webui.enabled = True
    config.webui.initial_password = "admin-pass"
    config.webui.auth_store_path = str(tmp_path / "auth.json")
    config.webui.web_config_path = str(tmp_path / "web.yaml")
    config.webui.secret_store_path = str(tmp_path / "secrets.json")
    config.media_index.enabled = False
    config.conversation_ingest.enabled = False
    config.export.markdown_dir = str(tmp_path / "exports")
    AuthStore(config.webui.auth_store_path).bootstrap(config.webui)
    return config


@pytest.fixture
def http(config):
    service = OAuthService(config)

    async def mcp(request):
        return JSONResponse({"client_id": request.user.display_name})

    app = Starlette(routes=[*oauth_routes(service), Route("/mcp", mcp), Route("/uploads", mcp)])
    app.add_middleware(BearerAuthMiddleware, token="master-key", oauth=service)
    return TestClient(app), service


def register(client, method="none", name="Test Client"):
    response = client.post("/register", json={
        "client_name": name, "redirect_uris": [REDIRECT], "token_endpoint_auth_method": method,
    })
    assert response.status_code == 201, response.text
    return response.json()


def authorize(client, info, **overrides):
    params = {
        "client_id": info["client_id"], "redirect_uri": REDIRECT, "response_type": "code",
        "code_challenge_method": "S256", "code_challenge": CHALLENGE,
        "resource": "http://127.0.0.1:8787/mcp", "state": "state-123",
    }
    params.update(overrides)
    response = client.get("/authorize", params=params, follow_redirects=False)
    assert response.status_code == 302, response.text
    consent = client.get(response.headers["location"])
    assert consent.status_code == 200
    hidden = re.search(r'name="request" value="([^"]+)"', consent.text).group(1)
    csrf = re.search(r'name="csrf" value="([^"]+)"', consent.text).group(1)
    return hidden, csrf, consent


def grant(client, info):
    hidden, csrf, _ = authorize(client, info)
    response = client.post("/oauth/consent", data={
        "request": hidden, "csrf": csrf, "password": "admin-pass", "decision": "approve",
    }, follow_redirects=False)
    assert response.status_code == 303, response.text
    query = parse_qs(urlsplit(response.headers["location"]).query)
    assert query["state"] == ["state-123"]
    assert query["iss"] == ["http://127.0.0.1:8787"]
    return query["code"][0]


def tokens(client, info):
    data = {"client_id": info["client_id"], "grant_type": "authorization_code",
            "code": grant(client, info), "code_verifier": VERIFIER, "redirect_uri": REDIRECT}
    headers = {}
    if info["token_endpoint_auth_method"] == "client_secret_post":
        data["client_secret"] = info["client_secret"]
    elif info["token_endpoint_auth_method"] == "client_secret_basic":
        data.pop("client_id")
        credentials = base64.b64encode(f'{info["client_id"]}:{info["client_secret"]}'.encode()).decode()
        headers["Authorization"] = f"Basic {credentials}"
    response = client.post("/token", data=data, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("method", ["none", "client_secret_post", "client_secret_basic"])
def test_complete_flow_restart_and_hashed_storage(http, config, method):
    client, service = http
    info = register(client, method)
    issued = tokens(client, info)
    access = issued["access_token"]
    response = client.get("/mcp", headers={"Authorization": "Bearer " + access})
    assert response.json()["client_id"] == info["client_id"]
    assert client.get("/uploads", headers={"Authorization": "Bearer " + access}).status_code == 401
    restarted = OAuthService(config)
    assert restarted.verify_access_token(access).client_id == info["client_id"]
    assert "client_secret" not in restarted.list_clients()[0]
    raw_db = service.path.read_bytes()
    assert access.encode() not in raw_db
    assert issued["refresh_token"].encode() not in raw_db
    if method != "none":
        assert info["client_secret"].encode() not in raw_db


def test_metadata_auth_required_even_on_loopback_and_legacy_master(http):
    client, _ = http
    response = client.get("/mcp")
    assert response.status_code == 401
    assert 'resource_metadata="http://127.0.0.1:8787/.well-known/oauth-protected-resource/mcp"' in response.headers["www-authenticate"]
    assert client.get("/.well-known/oauth-protected-resource/mcp").json()["resource"].endswith("/mcp")
    assert client.get("/.well-known/oauth-authorization-server").json()["code_challenge_methods_supported"] == ["S256"]
    assert client.get("/mcp", headers={"Authorization": "Bearer master-key"}).status_code == 200


@pytest.mark.parametrize("uri", ["http://example.com/cb", "https://x/cb#fragment", "https://user:pass@x/cb", "javascript:alert(1)", "/callback", "https://example.com/cb#", "https://example.com\\cb"])
def test_reject_unsafe_registration(http, uri):
    client, _ = http
    assert client.post("/register", json={"redirect_uris": [uri]}).status_code == 400


def test_pkce_redirect_resource_and_code_reuse(http):
    client, service = http
    info = register(client)
    for overrides in [
        {"redirect_uri": REDIRECT + "/"}, {"code_challenge_method": "plain"},
        {"code_challenge": "short"}, {"resource": "https://other.example/mcp"},
        {"scope": "admin"},
    ]:
        response = client.get("/authorize", params={
            "client_id": info["client_id"], "redirect_uri": REDIRECT, "response_type": "code",
            "code_challenge_method": "S256", "code_challenge": CHALLENGE, **overrides,
        }, follow_redirects=False)
        assert response.status_code == 400
        assert "location" not in response.headers
    code = grant(client, info)
    data = {"client_id": info["client_id"], "grant_type": "authorization_code", "code": code,
            "code_verifier": "b" * 64, "redirect_uri": REDIRECT}
    assert client.post("/token", data=data).json()["error"] == "invalid_grant"
    data["code_verifier"] = VERIFIER
    data["redirect_uri"] = REDIRECT + "/"
    assert client.post("/token", data=data).status_code == 400
    data["redirect_uri"] = REDIRECT
    data["resource"] = "https://other.example/mcp"
    assert client.post("/token", data=data).json()["error"] == "invalid_target"
    data.pop("resource")
    issued = client.post("/token", data=data).json()
    assert client.post("/token", data=data).json()["error"] == "invalid_grant"
    assert service.verify_access_token(issued["access_token"]) is None


def test_refresh_rotation_reuse_revocation_and_fixed_lifetime(http, config):
    client, service = http
    info = register(client)
    issued = tokens(client, info)
    with service.connect() as conn:
        old_expiry = conn.execute("SELECT expires_at FROM oauth_artifacts WHERE kind='refresh'").fetchone()[0]
    service = OAuthService(config)
    params = {"client_id": info["client_id"], "grant_type": "refresh_token", "refresh_token": issued["refresh_token"]}
    response = client.post("/token", data=params)
    assert response.status_code == 200
    rotated = response.json()
    assert rotated["refresh_token"] != issued["refresh_token"]
    with service.connect() as conn:
        assert {row[0] for row in conn.execute("SELECT expires_at FROM oauth_artifacts WHERE kind='refresh'")} == {old_expiry}
    assert client.post("/token", data={**params, "refresh_token": rotated["refresh_token"], "scope": "admin"}).json()["error"] == "invalid_scope"
    assert client.post("/token", data=params).json()["error"] == "invalid_grant"
    assert service.verify_access_token(rotated["access_token"]) is None
    assert service.verify_access_token(issued["access_token"]) is None
    assert client.post("/token", data={**params, "refresh_token": rotated["refresh_token"]}).status_code == 400


def test_refresh_race_revokes_family(http):
    client, service = http
    info = register(client)
    issued = tokens(client, info)
    params = {"client_id": info["client_id"], "grant_type": "refresh_token", "refresh_token": issued["refresh_token"]}
    auth_client = service.authenticate_client(params, "")

    def exchange():
        try:
            return service.exchange(auth_client, params)
        except OAuthError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: exchange(), range(2)))
    successful = [result for result in results if result]
    assert len(successful) == 1
    assert service.verify_access_token(successful[0]["access_token"]) is None


def test_consent_browser_binding_password_denial_and_xss(http):
    client, _ = http
    info = register(client, name='<script>alert("x")</script>')
    pending, csrf, page = authorize(client, info)
    assert "&lt;script&gt;" in page.text and "<script>" not in page.text
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    data = {"request": pending, "csrf": "bad", "decision": "approve", "password": "admin-pass"}
    assert client.post("/oauth/consent", data=data).status_code == 403
    data["csrf"] = csrf
    data["password"] = "wrong"
    response = client.post("/oauth/consent", data=data)
    assert "Incorrect administrator password" in response.text
    assert "location" not in response.headers
    csrf = re.search(r'name="csrf" value="([^"]+)"', response.text).group(1)
    data.update(csrf=csrf, decision="deny")
    denied = client.post("/oauth/consent", data=data, follow_redirects=False)
    assert parse_qs(urlsplit(denied.headers["location"]).query)["error"] == ["access_denied"]
    assert COOKIE not in client.cookies


def test_admin_management_csrf_rename_disable_delete_and_no_oauth_admin(http, config):
    client, service = http
    info = register(client)
    issued = tokens(client, info)
    admin = TestClient(build_webui_app(config))
    path = "/admin/api/security/oauth/clients"
    assert admin.get(path).status_code == 401
    assert admin.get(path, headers={"Authorization": "Bearer " + issued["access_token"]}).status_code == 401
    login = admin.post("/admin/api/auth/login", json={"password": "admin-pass"}).json()
    from research_memory_gateway.webui.auth_routes import verify_jwt
    csrf = verify_jwt(login["access_token"], admin.app.state.webui.sessions.signing_key)["csrf"]
    headers = {"X-CSRF-Token": csrf}
    client_path = path + "/" + info["client_id"]
    assert admin.patch(client_path, json={"client_name": "New Name"}).status_code == 403
    assert admin.patch(client_path, json={"client_name": "New Name"}, headers=headers).status_code == 200
    listed = admin.get(path).json()["items"][0]
    assert listed["client_name"] == "New Name" and listed["active_grants"] == 1
    assert "secret_hash" not in str(listed) and "access_token" not in str(listed)
    assert service.verify_access_token(issued["access_token"])
    assert admin.patch(client_path, json={"status": "disabled"}, headers=headers).status_code == 200
    assert service.verify_access_token(issued["access_token"]) is None
    assert admin.patch(client_path, json={"status": "active"}, headers=headers).status_code == 200
    assert service.verify_access_token(issued["access_token"]) is None
    new_tokens = tokens(client, info)
    assert admin.post(client_path + "/revoke", headers=headers).status_code == 200
    assert service.verify_access_token(new_tokens["access_token"]) is None
    created = admin.post(path, json={"client_name": "Managed", "redirect_uris": [REDIRECT]}, headers=headers)
    assert created.status_code == 201
    assert admin.delete(client_path, headers=headers).status_code == 200
    assert admin.patch(client_path, json={"client_name": "gone"}, headers=headers).status_code == 404


def test_expiry_revocation_wrong_client_and_limits(http, config):
    client, service = http
    info = register(client)
    other = register(client)
    issued = tokens(client, info)
    response = client.post("/token", data={"client_id": other["client_id"], "grant_type": "refresh_token", "refresh_token": issued["refresh_token"]})
    assert response.status_code == 400
    assert service.verify_access_token(issued["access_token"])
    assert client.post("/revoke", data={"client_id": other["client_id"], "token": issued["access_token"]}).status_code == 200
    assert service.verify_access_token(issued["access_token"])
    assert client.post("/revoke", data={"client_id": info["client_id"], "token": issued["refresh_token"]}).status_code == 200
    assert service.verify_access_token(issued["access_token"]) is None
    issued = tokens(client, info)
    with service.connect() as conn:
        conn.execute("UPDATE oauth_artifacts SET expires_at=?", (int(time.time()) - 1,))
    assert service.verify_access_token(issued["access_token"]) is None
    config.oauth.dynamic_registration = False
    assert client.post("/register", json={"redirect_uris": [REDIRECT]}).status_code == 403
    config.oauth.max_clients = 2
    with pytest.raises(OAuthError):
        service.create_client({"redirect_uris": [REDIRECT]})
    assert client.post("/token", content="x" * 65537).status_code == 413


def test_old_grants_cannot_move_to_a_different_resource(http, config):
    client, service = http
    info = register(client)
    issued = tokens(client, info)
    code = grant(client, info)
    config.oauth.public_url = "https://new.example.com"
    moved = OAuthService(config)
    assert moved.verify_access_token(issued["access_token"]) is None
    auth_client = moved.authenticate_client({"client_id": info["client_id"]}, "")
    for params in [
        {"grant_type": "refresh_token", "refresh_token": issued["refresh_token"]},
        {"grant_type": "authorization_code", "code": code, "code_verifier": VERIFIER, "redirect_uri": REDIRECT},
    ]:
        with pytest.raises(OAuthError, match="Resource"):
            moved.exchange(auth_client, params)


@pytest.mark.parametrize("redirect", [REDIRECT, "https://client.example.com/callback"])
def test_consent_csp_allows_validated_callback_navigation(http, redirect):
    client, _ = http
    registration = client.post("/register", json={
        "client_name": "Callback Test", "redirect_uris": [redirect], "token_endpoint_auth_method": "none",
    })
    assert registration.status_code == 201
    info = registration.json()
    authorization = client.get("/authorize", params={
        "response_type": "code", "client_id": info["client_id"], "redirect_uri": redirect,
        "scope": "mcp", "code_challenge_method": "S256", "code_challenge": CHALLENGE,
    }, follow_redirects=False)
    consent = client.get(authorization.headers["location"])
    policy = consent.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in policy
    assert "default-src 'none'" in policy
    assert "form-action" not in policy
    assert 'action="/oauth/consent"' in consent.text


@pytest.mark.parametrize("url", ["http://example.com", "https://user:pass@example.com", "https://example.com/path", "https://example.com?x=1", "", "https://example.com?", "https://example.com/#", "https://example.com:invalid"])
def test_config_rejects_invalid_issuer(url):
    with pytest.raises(ValueError):
        OAuthConfig(enabled=True, public_url=url)


def test_real_mcp_session_identity_and_revocation(config):
    import socket
    from threading import Thread
    import anyio
    import httpx
    import uvicorn
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from research_memory_gateway.server import _build_streamable_http_app, build_mcp

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    config.server.port = port
    app = _build_streamable_http_app(build_mcp(config), "master-key", config)
    service = app.state.oauth
    client = TestClient(Starlette(routes=oauth_routes(service)))
    info = register(client)
    issued = tokens(client, info)
    other = tokens(client, register(client))
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="error", timeout_graceful_shutdown=0,
    ))
    thread = Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                if probe.connect_ex(("127.0.0.1", port)) == 0:
                    break
            time.sleep(0.05)
        else:
            raise RuntimeError("OAuth test MCP server did not start")
        base = f"http://127.0.0.1:{port}"
        assert httpx.get(base + "/mcp").status_code == 401

        # Inspect a real transport session ID, then attempt to use it with another client.
        protocol_headers = {"Authorization": "Bearer " + issued["access_token"],
                            "Accept": "application/json, text/event-stream"}
        init = httpx.post(base + "/mcp", headers=protocol_headers, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                       "clientInfo": {"name": "oauth-test", "version": "1"}},
        })
        assert init.status_code == 200, init.text
        session_id = init.headers["mcp-session-id"]
        hijack = httpx.post(base + "/mcp", headers={
            **protocol_headers, "Authorization": "Bearer " + other["access_token"],
            "Mcp-Session-Id": session_id, "MCP-Protocol-Version": "2025-11-25",
        }, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert hijack.status_code in {403, 404}, hijack.text

        async def exercise():
            async with httpx.AsyncClient(headers={"Authorization": "Bearer " + issued["access_token"]}) as transport:
                async with streamable_http_client(base + "/mcp", http_client=transport) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        tools = await session.list_tools()
                        assert "recall_memory" in {tool.name for tool in tools.tools}
                        result = await session.call_tool("recall_memory", {"query": "OAuth integration probe"})
                        assert not result.is_error
        anyio.run(exercise)
        service.revoke_client(info["client_id"])
        assert httpx.post(base + "/mcp", headers=protocol_headers, json={
            "jsonrpc": "2.0", "id": 3, "method": "tools/list",
        }).status_code == 401
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        if thread.is_alive():
            server.force_exit = True
            thread.join(timeout=5)
        assert not thread.is_alive()
