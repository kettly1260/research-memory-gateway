from __future__ import annotations

import json

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from ..oauth import OAuthError, OAuthService


async def api_oauth_clients(request: Request) -> Response:
    state = request.app.state.webui
    config = state.config.oauth
    service: OAuthService | None = state.oauth
    if request.method == "GET":
        return JSONResponse({
            "enabled": config.enabled, "issuer": config.public_url or None,
            "resource": service.resource if service else None,
            "dynamic_registration": config.dynamic_registration,
            "items": service.list_clients() if service else [],
        }, headers={"Cache-Control": "no-store"})
    if not service:
        return JSONResponse({"error": "oauth_disabled"}, status_code=409)
    try:
        client_id = request.path_params.get("client_id")
        action = "created"
        if client_id and request.method == "DELETE":
            service.revoke_client(client_id, delete=True)
            result = {"deleted": True}
            action = "deleted"
        elif client_id and request.url.path.endswith("/revoke"):
            service.revoke_client(client_id)
            result = {"revoked": True}
            action = "revoked"
        else:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError("Expected a JSON object")
            if client_id:
                service.update_client(client_id, payload)
                result = {"updated": True}
                action = "updated"
            else:
                result = service.create_client(payload)
                client_id = result["client_id"]
        state.service.append_audit_event("security.oauth_client_" + action, metadata={"client_id": client_id})
        return JSONResponse(result, status_code=201 if action == "created" else 200,
                            headers={"Cache-Control": "no-store"})
    except OAuthError as exc:
        status = 404 if exc.error == "invalid_client" else exc.status
        return JSONResponse({"error": exc.error, "error_description": exc.description}, status_code=status)
    except (ValueError, json.JSONDecodeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


def oauth_admin_routes() -> list[Route]:
    return [
        Route("/admin/api/security/oauth/clients", api_oauth_clients, methods=["GET", "POST"]),
        Route("/admin/api/security/oauth/clients/{client_id:str}", api_oauth_clients, methods=["PATCH", "DELETE"]),
        Route("/admin/api/security/oauth/clients/{client_id:str}/revoke", api_oauth_clients, methods=["POST"]),
    ]
