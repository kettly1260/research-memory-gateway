"""Starlette routes for the non-MCP conversation ingest API.

Mounted next to the MCP transport on the same port so the existing
``BearerAuthMiddleware`` (master token + ``api_keys`` table) protects the write
path with the *same* authentication model as the read path -- no second,
incompatible auth system.

Endpoints
---------
``POST /api/conversations/events``        one event
``POST /api/conversations/events/batch``  up to ``max_batch_events`` events
``POST /api/conversations/session-end``   session completion declaration
``POST /api/conversations/snapshot``      full-conversation reconciliation
``GET  /api/conversations/ingest/stats``  ingest observability

Rejected payloads always answer with the stable response envelope
``{"schema_version": 1, "accepted": [], "duplicates": [], "rejected": [...]}`` so
the client can switch on machine-readable codes instead of HTTP status text.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import ValidationError
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .schema import (
    INGEST_SCHEMA_VERSION,
    BatchRequest,
    BatchResponse,
    IngestEvent,
    RejectedEvent,
    SessionEndRequest,
    SnapshotRequest,
)
from .service import ConversationIngestService, IngestDisabledError, rejection_from_validation_error

logger = logging.getLogger(__name__)

#: Hard cap on request body size (bytes) before JSON parsing.
MAX_REQUEST_BYTES = 32 * 1024 * 1024


def _envelope(
    *,
    accepted: list[str] | None = None,
    duplicates: list[str] | None = None,
    rejected: list[RejectedEvent] | None = None,
    session: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = BatchResponse(
        schema_version=INGEST_SCHEMA_VERSION,
        accepted=accepted or [],
        duplicates=duplicates or [],
        rejected=rejected or [],
        session=session,
    )
    return payload.model_dump(mode="json")


def _reject(code: str, message: str, *, status_code: int, event_id: str = "") -> JSONResponse:
    body = _envelope(rejected=[RejectedEvent(event_id=event_id, code=code, message=message)])
    return JSONResponse(body, status_code=status_code)


def _status_for(result: BatchResponse) -> int:
    """HTTP status for an ingest result.

    The response envelope is always the same, so a client can parse rejections
    regardless of status.  The status still distinguishes the two cases that
    matter operationally:

    * the whole request was refused (bad version, oversized batch, empty
      snapshot) -- every rejection is request-level, i.e. carries no event id --
      which is a client error and answers ``400``;
    * some events were accepted and some refused, which is normal partial
      success and answers ``200``.
    """
    if result.accepted or result.duplicates:
        return 200
    if result.rejected and all(not item.event_id for item in result.rejected):
        return 400
    return 200


async def _read_json(request: Any) -> tuple[dict[str, Any] | None, JSONResponse | None]:
    raw = await request.body()
    if len(raw) > MAX_REQUEST_BYTES:
        return None, _reject(
            "invalid_payload",
            f"request body exceeds {MAX_REQUEST_BYTES} bytes",
            status_code=413,
        )
    if not raw:
        return None, _reject("invalid_payload", "empty request body", status_code=400)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, _reject("invalid_payload", f"malformed JSON: {exc.msg}", status_code=400)
    if not isinstance(payload, dict):
        return None, _reject("invalid_payload", "body must be a JSON object", status_code=400)
    return payload, None


def _disabled_response() -> JSONResponse:
    return _reject(
        "ingest_disabled",
        "Conversation ingest is disabled on this gateway "
        "(enable conversation_archive.enabled and conversation_ingest.enabled).",
        status_code=503,
    )


def build_ingest_routes(ingest: ConversationIngestService) -> list[Route]:
    """Build the ingest route table for one gateway process."""

    async def ingest_event(request: Any) -> Response:
        payload, error = await _read_json(request)
        if error is not None:
            return error
        assert payload is not None
        try:
            event = IngestEvent.model_validate(payload)
        except ValidationError as exc:
            return JSONResponse(
                _envelope(rejected=rejection_from_validation_error(exc)), status_code=400
            )
        try:
            result = ingest.ingest_single(event, client_id=str(payload.get("client_id") or ""))
        except IngestDisabledError:
            return _disabled_response()
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("Ingest failed for event %s", event.event_id)
            return _reject("internal_error", exc.__class__.__name__, status_code=500)
        return JSONResponse(result.model_dump(mode="json"), status_code=_status_for(result))

    async def ingest_batch(request: Any) -> Response:
        payload, error = await _read_json(request)
        if error is not None:
            return error
        assert payload is not None
        try:
            batch = BatchRequest.model_validate(payload)
        except ValidationError as exc:
            return JSONResponse(
                _envelope(rejected=rejection_from_validation_error(exc)), status_code=400
            )
        try:
            result = ingest.ingest_batch(batch)
        except IngestDisabledError:
            return _disabled_response()
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("Batch ingest failed")
            return _reject("internal_error", exc.__class__.__name__, status_code=500)
        return JSONResponse(result.model_dump(mode="json"), status_code=_status_for(result))

    async def session_end(request: Any) -> Response:
        payload, error = await _read_json(request)
        if error is not None:
            return error
        assert payload is not None
        try:
            parsed = SessionEndRequest.model_validate(payload)
        except ValidationError as exc:
            return JSONResponse(
                _envelope(rejected=rejection_from_validation_error(exc)), status_code=400
            )
        try:
            result = ingest.session_end(parsed)
        except IngestDisabledError:
            return _disabled_response()
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("Session-end reconciliation failed")
            return _reject("internal_error", exc.__class__.__name__, status_code=500)
        if "error" in result:
            return JSONResponse(
                _envelope(rejected=[RejectedEvent(**result["error"])]), status_code=400
            )
        return JSONResponse({"schema_version": INGEST_SCHEMA_VERSION, **result})

    async def snapshot(request: Any) -> Response:
        payload, error = await _read_json(request)
        if error is not None:
            return error
        assert payload is not None
        try:
            parsed = SnapshotRequest.model_validate(payload)
        except ValidationError as exc:
            return JSONResponse(
                _envelope(rejected=rejection_from_validation_error(exc)), status_code=400
            )
        try:
            result = ingest.snapshot(parsed)
        except IngestDisabledError:
            return _disabled_response()
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("Snapshot ingest failed")
            return _reject("internal_error", exc.__class__.__name__, status_code=500)
        return JSONResponse(result.model_dump(mode="json"), status_code=_status_for(result))

    async def ingest_stats(request: Any) -> Response:
        try:
            return JSONResponse(ingest.stats())
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("Ingest stats failed")
            return _reject("internal_error", exc.__class__.__name__, status_code=500)

    return [
        Route("/api/conversations/events", ingest_event, methods=["POST"]),
        Route("/api/conversations/events/batch", ingest_batch, methods=["POST"]),
        Route("/api/conversations/session-end", session_end, methods=["POST"]),
        Route("/api/conversations/snapshot", snapshot, methods=["POST"]),
        Route("/api/conversations/ingest/stats", ingest_stats, methods=["GET"]),
    ]
