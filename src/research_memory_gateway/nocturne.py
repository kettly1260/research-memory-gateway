from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


@dataclass
class NocturneReservedConnector:
    transport: str = "unknown"
    url: str | None = None
    token: str | None = None

    def status(self) -> dict[str, Any]:
        return {
            "reserved": True,
            "transport": self.transport,
            "configured": bool(self.url),
            "capabilities": ["config", "encrypted_token", "connection_test"],
            "unsupported": ["sync", "import", "dual_write", "create", "search", "read", "update", "delete"],
        }

    async def test_connection(self) -> dict[str, Any]:
        if not self.url:
            return {"ok": False, "status": "not_configured", **self.status()}
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                response = await client.get(self.url, headers=headers)
            return {"ok": response.status_code < 500, "status_code": response.status_code, **self.status()}
        except httpx.HTTPError as exc:
            return {"ok": False, "error": exc.__class__.__name__, **self.status()}

    def create(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return self._not_implemented("create")

    def search(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return self._not_implemented("search")

    def read(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return self._not_implemented("read")

    def update(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return self._not_implemented("update")

    def delete(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return self._not_implemented("delete")

    def _not_implemented(self, operation: str) -> dict[str, Any]:
        return {
            "ok": False,
            "error": "not_implemented",
            "operation": operation,
            "message": "Nocturne v1 is reserved for configuration and connection testing only.",
            **self.status(),
        }
