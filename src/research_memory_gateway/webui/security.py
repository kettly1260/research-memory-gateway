from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response


SESSION_COOKIE = "rmg_webui_session"
CSRF_COOKIE = "rmg_webui_csrf"


@dataclass
class Session:
    session_id: str
    expires_at: datetime


class SessionStore:
    def __init__(self, max_age_seconds: int) -> None:
        self.max_age_seconds = max_age_seconds
        self.sessions: dict[str, Session] = {}

    def create(self) -> Session:
        session = Session(
            session_id=secrets.token_urlsafe(32),
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=self.max_age_seconds),
        )
        self.sessions[session.session_id] = session
        return session

    def valid(self, session_id: str | None) -> bool:
        if not session_id:
            return False
        session = self.sessions.get(session_id)
        if session is None:
            return False
        if session.expires_at <= datetime.now(timezone.utc):
            self.sessions.pop(session_id, None)
            return False
        return True

    def delete(self, session_id: str | None) -> None:
        if session_id:
            self.sessions.pop(session_id, None)

    def clear(self) -> None:
        self.sessions.clear()


def csrf_token(request: Request) -> str:
    token = request.cookies.get(CSRF_COOKIE)
    if token:
        return token
    return secrets.token_urlsafe(32)


def set_security_headers(response: Response) -> None:
    headers = MutableHeaders(response.headers)
    headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
    headers["X-Frame-Options"] = "DENY"
    headers["X-Content-Type-Options"] = "nosniff"
    headers["Referrer-Policy"] = "same-origin"


def redirect(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


async def read_form_or_json(request: Request) -> dict[str, Any]:
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        body = await request.json()
        return body if isinstance(body, dict) else {}
    form = await request.form()
    return dict(form)
