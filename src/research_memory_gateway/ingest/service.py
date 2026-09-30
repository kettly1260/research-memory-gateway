"""Gateway-side conversation ingest service (the ``bridge -> HTTP -> archive`` path).

Responsibilities, in order:

1. validate the wire payload against the versioned schema (fail-closed);
2. second-pass secret redaction (the client sanitizer is not trusted alone);
3. idempotent ledger insert keyed by ``event_id``;
4. materialize the archive Markdown note from the ledger and re-index FTS so the
   existing MCP read tools (``conversation_search`` / ``conversation_recall`` /
   ``conversation_read``) can find the new content immediately;
5. reconcile session-end declarations and snapshot payloads.

The service never performs retrieval, ranking, embedding or rerank -- that stays
in the gateway's read path.  The bridge is a client, not a second memory server.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ..config import AppConfig, atomic_write_text
from ..secret_scan import redact_text
from . import render
from .identity import derive_event_id, event_content_hash, session_key
from .schema import (
    INGEST_SCHEMA_VERSION,
    SUPPORTED_SCHEMA_VERSIONS,
    BatchRequest,
    BatchResponse,
    IngestEvent,
    RejectedEvent,
    SessionEndRequest,
    SnapshotRequest,
)
from .store import IngestStore, metadata_bytes, metadata_json, utc_now

logger = logging.getLogger(__name__)


class IngestDisabledError(RuntimeError):
    """Raised when the ingest surface is switched off by configuration."""

    code = "ingest_disabled"


class ConversationIngestService:
    """Validated, idempotent write path into the conversation archive."""

    def __init__(self, config: AppConfig, service: Any) -> None:
        self.config = config
        self.service = service
        self.store = IngestStore(config.conversation_ingest.resolve_state_path())

    # -- capability ----------------------------------------------------------

    @property
    def enabled(self) -> bool:
        """Ingest needs both the ingest surface and the archive it writes into."""
        return bool(
            self.config.conversation_ingest.enabled and self.config.conversation_archive.enabled
        )

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise IngestDisabledError(
                "Conversation ingest is disabled. Enable conversation_archive.enabled and "
                "conversation_ingest.enabled in the gateway config."
            )

    @property
    def staging_dir(self) -> Path:
        return self.config.conversation_archive.resolve_staging_dir()

    def _index_db(self) -> Any:
        return self.service.conversation_retrieval.index_db

    # -- validation ----------------------------------------------------------

    def _check_version(self, version: int) -> RejectedEvent | None:
        if version not in SUPPORTED_SCHEMA_VERSIONS:
            return RejectedEvent(
                event_id="",
                code="unsupported_schema_version",
                message=(
                    f"schema_version={version} is not supported; "
                    f"this gateway accepts {sorted(SUPPORTED_SCHEMA_VERSIONS)}"
                ),
            )
        return None

    def _validate_event(self, event: IngestEvent) -> RejectedEvent | None:
        cfg = self.config.conversation_ingest
        if len(event.content) > cfg.max_content_chars:
            return RejectedEvent(
                event_id=event.event_id,
                code="content_too_large",
                message=f"content is {len(event.content)} chars; limit is {cfg.max_content_chars}",
            )
        if metadata_bytes(event.metadata) > 65_536:
            return RejectedEvent(
                event_id=event.event_id,
                code="metadata_too_large",
                message="metadata exceeds 65536 bytes",
            )
        if event.event_type in {"user_prompt", "assistant_message"} and not event.conversation_id:
            if not event.session_id:
                return RejectedEvent(
                    event_id=event.event_id,
                    code="missing_required_field",
                    message="conversation_id or session_id is required for message events",
                )
        return None

    @staticmethod
    def _thread_of(event: IngestEvent) -> tuple[str, str, str, str]:
        conversation_id = event.conversation_id or event.session_id
        return (event.source_system, conversation_id, event.thread_id, event.branch_id)

    # -- event ingestion -----------------------------------------------------

    def ingest_batch(self, request: BatchRequest) -> BatchResponse:
        self._require_enabled()
        response = BatchResponse(schema_version=INGEST_SCHEMA_VERSION)
        version_error = self._check_version(request.schema_version)
        if version_error is not None:
            response.rejected.append(version_error)
            return response

        cfg = self.config.conversation_ingest
        if len(request.events) > cfg.max_batch_events:
            response.rejected.append(
                RejectedEvent(
                    event_id="",
                    code="invalid_payload",
                    message=(
                        f"batch has {len(request.events)} events; "
                        f"limit is {cfg.max_batch_events}"
                    ),
                )
            )
            return response

        return self._ingest_events(request.events, client_id=request.client_id)

    def _ingest_events(self, events: list[IngestEvent], *, client_id: str) -> BatchResponse:
        response = BatchResponse(schema_version=INGEST_SCHEMA_VERSION)
        rows: list[dict[str, Any]] = []
        touched: dict[tuple[str, str, str, str], dict[str, Any]] = {}

        for event in events:
            rejection = self._validate_event(event)
            if rejection is not None:
                response.rejected.append(rejection)
                continue
            # Server-side second pass: the client sanitizer is a defence, not a
            # guarantee.  Redaction is deterministic, so a retry of the same
            # unredacted payload still produces the same stored fingerprint.
            redacted_content, _report = redact_text(event.content, path="$.ingest.content")
            thread = self._thread_of(event)
            rows.append(
                {
                    "event_id": event.event_id,
                    "schema_version": event.schema_version,
                    "source_system": event.source_system,
                    "source_account_namespace": event.source_account_namespace,
                    "session_id": event.session_id,
                    "conversation_id": thread[1],
                    "thread_id": event.thread_id,
                    "branch_id": event.branch_id,
                    "message_id": event.message_id,
                    "turn_id": event.turn_id,
                    "event_type": event.event_type,
                    "role": event.role,
                    "content": redacted_content,
                    "content_hash": event_content_hash(
                        event.event_type, event.role, redacted_content
                    ),
                    "timestamp": event.timestamp,
                    "metadata_json": metadata_json(event.metadata),
                    "client_id": client_id,
                    "received_at": utc_now(),
                }
            )
            touched.setdefault(thread, {"event": event, "client_id": client_id})

        accepted, duplicates, conflicts = self.store.insert_events(rows)
        response.accepted.extend(accepted)
        response.duplicates.extend(duplicates)
        for event_id, _stored_hash in conflicts:
            response.rejected.append(
                RejectedEvent(
                    event_id=event_id,
                    code="event_id_conflict",
                    message=(
                        "event_id already exists with a different content fingerprint; "
                        "the client must derive event_id from stable event identity"
                    ),
                )
            )

        for thread, info in touched.items():
            source_system, conversation_id, thread_id, branch_id = thread
            event = info["event"]
            self._upsert_session(
                source_system=source_system,
                source_account_namespace=event.source_account_namespace,
                session_id=event.session_id,
                conversation_id=conversation_id,
                thread_id=thread_id,
                branch_id=branch_id,
                client_id=info["client_id"],
            )

        materialized = self._materialize_threads(touched.keys(), client_id=client_id)
        if len(materialized) == 1:
            response.session = next(iter(materialized.values()))

        self.store.bump_counters(
            accepted=len(response.accepted),
            duplicates=len(response.duplicates),
            rejected=len(response.rejected),
        )
        self.store.record_client(
            client_id=client_id,
            source_system=(rows[0]["source_system"] if rows else ""),
            accepted=len(response.accepted),
            duplicates=len(response.duplicates),
            rejected=len(response.rejected),
        )
        return response

    def ingest_single(self, event: IngestEvent, *, client_id: str = "") -> BatchResponse:
        self._require_enabled()
        version_error = self._check_version(event.schema_version)
        if version_error is not None:
            response = BatchResponse(schema_version=INGEST_SCHEMA_VERSION)
            response.rejected.append(version_error)
            return response
        return self._ingest_events([event], client_id=client_id)

    def _upsert_session(
        self,
        *,
        source_system: str,
        source_account_namespace: str,
        session_id: str,
        conversation_id: str,
        thread_id: str,
        branch_id: str,
        client_id: str,
        title: str = "",
        model: str = "",
    ) -> str:
        key = session_key(
            source_system=source_system,
            source_account_namespace=source_account_namespace,
            session_id=session_id,
            conversation_id=conversation_id,
            thread_id=thread_id,
            branch_id=branch_id,
        )
        self.store.upsert_session(
            session_key=key,
            source_system=source_system,
            source_account_namespace=source_account_namespace,
            session_id=session_id,
            conversation_id=conversation_id,
            thread_id=thread_id,
            branch_id=branch_id,
            title=title,
            model=model,
        )
        existing = self.store.get_session(key)
        if existing is not None and existing.get("ended"):
            # A hook arriving after the session was declared finished reopens it;
            # the materializer stays append-only, so nothing is lost or reordered.
            self.store.reopen_session(key)
        return key

    # -- materialization -----------------------------------------------------

    def _materialize_threads(self, threads: Any, *, client_id: str) -> dict[str, dict[str, Any]]:
        results: dict[str, dict[str, Any]] = {}
        for source_system, conversation_id, thread_id, branch_id in threads:
            try:
                summary = self._materialize(
                    source_system=source_system,
                    conversation_id=conversation_id,
                    thread_id=thread_id,
                    branch_id=branch_id,
                    client_id=client_id,
                )
            except Exception as exc:  # pragma: no cover - defensive
                logger.error(
                    "Ingest materialization failed source=%s conversation=%s: %s",
                    source_system,
                    conversation_id,
                    exc,
                )
                continue
            results[f"{source_system}:{conversation_id}:{thread_id}:{branch_id}"] = summary
        return results

    def _materialize(
        self,
        *,
        source_system: str,
        conversation_id: str,
        thread_id: str = "",
        branch_id: str = "",
        client_id: str = "",
        extra_frontmatter: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Render the archive note from the ledger and re-index it.

        Idempotent by construction: each block carries an ``rmb:ev <event_id>``
        marker, so a crash between "note written" and "cursor advanced" only
        causes the same blocks to be skipped on the next pass -- never duplicated.
        """
        events = self.store.events_for_thread(
            source_system=source_system,
            conversation_id=conversation_id,
            thread_id=thread_id,
            branch_id=branch_id,
        )
        if not events:
            return {"materialized": False, "reason": "no_events"}

        path = render.note_path_for(
            self.staging_dir,
            source_system=source_system,
            conversation_id=conversation_id,
            thread_id=thread_id,
            branch_id=branch_id,
        )
        first = events[0]
        sessions = self.store.sessions_for_thread(
            source_system=source_system,
            conversation_id=conversation_id,
            thread_id=thread_id,
            branch_id=branch_id,
        )
        session = sessions[0] if sessions else {}
        title = render.note_title(
            conversation_id,
            title=str(session.get("title") or ""),
            metadata=self._title_metadata(events),
        )
        message_count = sum(
            1 for event in events if event["event_type"] in {"user_prompt", "assistant_message"}
        )
        completion = self._completion_status(events)
        frontmatter = render.build_frontmatter(
            source_system=source_system,
            source_account_namespace=str(first.get("source_account_namespace") or ""),
            conversation_id=conversation_id,
            thread_id=thread_id,
            branch_id=branch_id,
            client_id=client_id or str(first.get("client_id") or ""),
            title=title,
            model=str(session.get("model") or ""),
            created_at=str(first.get("timestamp") or first.get("received_at") or ""),
            updated_at=str(events[-1].get("timestamp") or events[-1].get("received_at") or ""),
            event_count=len(events),
            message_count=message_count,
            completion_status=completion,
            metadata=self._note_metadata(events),
            schema_version=int(first.get("schema_version") or INGEST_SCHEMA_VERSION),
        )
        if extra_frontmatter:
            frontmatter.update(extra_frontmatter)

        existing_text = ""
        if path.exists():
            try:
                existing_text = path.read_text(encoding="utf-8")
            except OSError as exc:  # pragma: no cover - defensive
                logger.error("Cannot read existing bridge note %s: %s", path, exc)
                existing_text = ""

        has_managed_region = bool(existing_text) and render.RMB_BEGIN in existing_text
        # A complete ledger can always be re-rendered exactly.  Only a *pruned*
        # ledger (older rows removed by retention) forces the append-only path,
        # because re-rendering then would silently truncate archived content.
        pruned = bool(session.get("events_pruned"))
        manual = ""
        if has_managed_region:
            _frontmatter, _managed, manual = render.parse_note(existing_text)

        if pruned and has_managed_region:
            already = render.scan_event_ids(existing_text)
            pending = [event for event in events if str(event["event_id"]) not in already]
            pending = self._drop_superseded(pending, events)
            renderable = [event for event in pending if render.is_renderable(event)]
            if not renderable and not extra_frontmatter:
                self._update_progress(
                    source_system=source_system,
                    conversation_id=conversation_id,
                    thread_id=thread_id,
                    branch_id=branch_id,
                    path=path,
                    events=events,
                    message_count=message_count,
                )
                return {
                    "materialized": False,
                    "reason": "already_materialized",
                    "note_path": str(path),
                    "event_count": len(events),
                }
            new_text = render.append_to_note(
                existing_text=existing_text,
                new_blocks=render.render_blocks(renderable),
                frontmatter_updates=frontmatter,
            )
        else:
            if has_managed_region and not extra_frontmatter:
                already = render.scan_event_ids(existing_text)
                if all(str(event["event_id"]) in already for event in events):
                    self._update_progress(
                        source_system=source_system,
                        conversation_id=conversation_id,
                        thread_id=thread_id,
                        branch_id=branch_id,
                        path=path,
                        events=events,
                        message_count=message_count,
                    )
                    return {
                        "materialized": False,
                        "reason": "already_materialized",
                        "note_path": str(path),
                        "event_count": len(events),
                    }
            renderable = [
                event
                for event in self._drop_superseded(events, events)
                if render.is_renderable(event)
            ]
            new_text = render.render_note(
                frontmatter=frontmatter,
                title=title,
                blocks=render.render_blocks(renderable),
                manual=manual,
            )

        atomic_write_text(path, new_text)
        indexed = self._index(path)
        self._update_progress(
            source_system=source_system,
            conversation_id=conversation_id,
            thread_id=thread_id,
            branch_id=branch_id,
            path=path,
            events=events,
            message_count=message_count,
        )
        return {
            "materialized": True,
            "note_path": str(path),
            "event_count": len(events),
            "message_count": message_count,
            "indexed": indexed,
            "completion_status": completion,
        }

    @staticmethod
    def _drop_superseded(
        candidates: list[dict[str, Any]], all_events: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Drop adapter-synthesised blocks that a provider-identified block covers.

        Codex's `notify` hook carries no message ids, so the realtime path
        anchors messages on synthesised ids while the transcript reconciliation
        path has the provider ids.  Without this rule the same message would be
        archived twice.  The rule is deliberately narrow: a block is dropped only
        when it is *not* provider-identified **and** a provider-identified block
        with the same role and content fingerprint already exists.
        """
        identified = {
            (str(event.get("role") or ""), str(event.get("content_hash") or ""))
            for event in all_events
            if render.id_source(event) == render.ID_SOURCE_PROVIDER
        }
        if not identified:
            return candidates
        return [
            event
            for event in candidates
            if render.id_source(event) == render.ID_SOURCE_PROVIDER
            or (str(event.get("role") or ""), str(event.get("content_hash") or ""))
            not in identified
        ]

    def _update_progress(
        self,
        *,
        source_system: str,
        conversation_id: str,
        thread_id: str,
        branch_id: str,
        path: Path,
        events: list[dict[str, Any]],
        message_count: int,
    ) -> None:
        key = session_key(
            source_system=source_system,
            source_account_namespace=str(events[0].get("source_account_namespace") or ""),
            session_id=str(events[0].get("session_id") or ""),
            conversation_id=conversation_id,
            thread_id=thread_id,
            branch_id=branch_id,
        )
        self.store.update_session_progress(
            session_key=key,
            stored_message_count=message_count,
            last_materialized_seq=max(int(event.get("seq") or 0) for event in events),
            materialized_event_count=len(events),
            last_message_id=self.store.last_message_id(
                source_system=source_system,
                conversation_id=conversation_id,
                thread_id=thread_id,
                branch_id=branch_id,
            ),
            note_path=str(path),
        )

    def _index(self, path: Path) -> bool:
        try:
            self._index_db().index_file(path)
            return True
        except Exception as exc:
            logger.warning("Failed to index bridge note %s: %s", path, exc)
            return False

    @staticmethod
    def _title_metadata(events: list[dict[str, Any]]) -> dict[str, Any]:
        for event in events:
            metadata = event.get("metadata")
            if isinstance(metadata, dict) and metadata.get("title"):
                return {"title": metadata["title"]}
        return {}

    @staticmethod
    def _note_metadata(events: list[dict[str, Any]]) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        for event in events:
            metadata = event.get("metadata")
            if not isinstance(metadata, dict):
                continue
            for key in ("projects", "model_provider", "agent_version", "cwd"):
                if key in metadata and key not in merged:
                    merged[key] = metadata[key]
        return merged

    @staticmethod
    def _completion_status(events: list[dict[str, Any]]) -> str:
        for event in reversed(events):
            if event["event_type"] == "assistant_message":
                return "complete"
            if event["event_type"] == "user_prompt":
                return "incomplete_or_unknown"
        return "incomplete_or_unknown"

    # -- session end ---------------------------------------------------------

    def session_end(self, request: SessionEndRequest) -> dict[str, Any]:
        self._require_enabled()
        version_error = self._check_version(request.schema_version)
        if version_error is not None:
            return {"error": version_error.model_dump(mode="json")}

        conversation_id = request.conversation_id or request.session_id
        thread_id, branch_id = self._resolve_session_thread(
            source_system=request.source_system,
            conversation_id=conversation_id,
            thread_id=request.thread_id,
            branch_id=request.branch_id,
        )
        key = self._upsert_session(
            source_system=request.source_system,
            source_account_namespace=request.source_account_namespace,
            session_id=request.session_id,
            conversation_id=conversation_id,
            thread_id=thread_id,
            branch_id=branch_id,
            client_id=request.client_id,
        )
        stored = self.store.count_messages(
            source_system=request.source_system,
            conversation_id=conversation_id,
            thread_id=thread_id,
            branch_id=branch_id,
        )
        missing = 0
        if self.config.conversation_ingest.reconcile_on_session_end:
            missing = max(0, int(request.observed_message_count) - stored)
        self.store.mark_session_ended(
            session_key=key,
            ended_at=request.ended_at,
            observed_message_count=int(request.observed_message_count),
            last_message_id=request.last_message_id,
        )
        session = self.store.get_session(key) or {}
        summary: dict[str, Any] = {}
        try:
            summary = self._materialize(
                source_system=request.source_system,
                conversation_id=conversation_id,
                thread_id=thread_id,
                branch_id=branch_id,
                client_id=request.client_id,
                extra_frontmatter={"ingest_session_ended": True},
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Session-end materialization failed: %s", exc)

        # The client's declared last_message_id is authoritative here: the
        # gateway may legitimately have fewer events than the agent saw.
        return {
            "session_id": request.session_id,
            "conversation_id": conversation_id,
            "thread_id": thread_id,
            "stored_message_count": stored,
            "observed_message_count": int(request.observed_message_count),
            "missing_event_count": missing,
            "reconciliation_required": missing > 0,
            "last_message_id": str(
                request.last_message_id
                or session.get("last_message_id")
                or self.store.last_message_id(
                    source_system=request.source_system,
                    conversation_id=conversation_id,
                    thread_id=thread_id,
                    branch_id=branch_id,
                )
            ),
            "note_path": summary.get("note_path", ""),
        }

    def _resolve_session_thread(
        self,
        *,
        source_system: str,
        conversation_id: str,
        thread_id: str,
        branch_id: str,
    ) -> tuple[str, str]:
        """Fill in an omitted thread/branch when the answer is unambiguous.

        A session-end declaration is about a conversation, not about the
        adapter-specific thread/branch pair (for Codex the thread id equals the
        conversation id; a generic adapter leaves it empty).  When the client
        omits both and exactly one session exists for the conversation, adopt
        that one.  If several exist the request stays scoped to the empty pair,
        so the gateway never silently reconciles the wrong thread.
        """
        if thread_id or branch_id:
            return thread_id, branch_id
        existing = self.store.sessions_for_conversation(
            source_system=source_system, conversation_id=conversation_id
        )
        if len(existing) == 1:
            return str(existing[0]["thread_id"] or ""), str(existing[0]["branch_id"] or "")
        return thread_id, branch_id

    # -- snapshot ------------------------------------------------------------

    def snapshot(self, request: SnapshotRequest) -> BatchResponse:
        """Ingest a full conversation snapshot as ordinary idempotent events.

        Snapshot messages reuse the exact same event-id derivation as the
        realtime path, so hook-captured and snapshot-captured copies of one
        message collapse into a single archived message.
        """
        self._require_enabled()
        response = BatchResponse(schema_version=INGEST_SCHEMA_VERSION)
        version_error = self._check_version(request.schema_version)
        if version_error is not None:
            response.rejected.append(version_error)
            return response
        if not request.messages:
            response.rejected.append(
                RejectedEvent(
                    event_id="",
                    code="invalid_payload",
                    message="snapshot must contain at least one message",
                )
            )
            return response

        conversation_id = request.conversation_id or request.session_id
        if not conversation_id:
            response.rejected.append(
                RejectedEvent(
                    event_id="",
                    code="missing_required_field",
                    message="snapshot requires conversation_id or session_id",
                )
            )
            return response

        cfg = self.config.conversation_ingest
        events: list[IngestEvent] = []
        for message in request.messages:
            redacted, _report = redact_text(message.content, path="$.snapshot.content")
            event_type = "user_prompt" if message.role == "user" else (
                "assistant_message" if message.role == "assistant" else "system_message"
            )
            event_id = message.event_id or derive_event_id(
                schema_version=request.schema_version,
                source_system=request.source_system,
                source_account_namespace=request.source_account_namespace,
                conversation_id=conversation_id,
                thread_id=request.thread_id,
                branch_id=request.branch_id,
                message_id=message.message_id,
                turn_id=message.turn_id,
                event_type=event_type,
                content=redacted,
            )
            if len(redacted) > cfg.max_content_chars:
                response.rejected.append(
                    RejectedEvent(
                        event_id=event_id,
                        code="content_too_large",
                        message=f"snapshot message exceeds {cfg.max_content_chars} chars",
                    )
                )
                continue
            events.append(
                IngestEvent(
                    event_id=event_id,
                    schema_version=request.schema_version,
                    source_system=request.source_system,
                    source_account_namespace=request.source_account_namespace,
                    session_id=request.session_id or conversation_id,
                    conversation_id=conversation_id,
                    thread_id=request.thread_id,
                    branch_id=request.branch_id,
                    message_id=message.message_id,
                    turn_id=message.turn_id,
                    event_type=event_type,
                    role=message.role,
                    content=redacted,
                    timestamp=message.timestamp,
                    metadata={
                        **request.metadata,
                        "snapshot": True,
                        **({"title": request.title} if request.title else {}),
                        **({"model": request.model} if request.model else {}),
                    },
                )
            )

        if not events:
            return response

        key = self._upsert_session(
            source_system=request.source_system,
            source_account_namespace=request.source_account_namespace,
            session_id=request.session_id or conversation_id,
            conversation_id=conversation_id,
            thread_id=request.thread_id,
            branch_id=request.branch_id,
            client_id=request.client_id,
            title=request.title,
            model=request.model,
        )
        result = self._ingest_events(events, client_id=request.client_id)
        if request.ended:
            self.store.mark_session_ended(
                session_key=key,
                observed_message_count=len(events),
            )
            result.session = self.session_end(
                SessionEndRequest(
                    schema_version=request.schema_version,
                    client_id=request.client_id,
                    source_system=request.source_system,
                    source_account_namespace=request.source_account_namespace,
                    session_id=request.session_id or conversation_id,
                    conversation_id=conversation_id,
                    thread_id=request.thread_id,
                    branch_id=request.branch_id,
                    observed_message_count=len(events),
                )
            )
        return result

    # -- observability -------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "schema_version": INGEST_SCHEMA_VERSION,
            "supported_schema_versions": sorted(SUPPORTED_SCHEMA_VERSIONS),
            "state_path": str(self.config.conversation_ingest.resolve_state_path()),
            "staging_dir": str(self.staging_dir),
            **self.store.stats(),
        }

    def prune(self) -> dict[str, int]:
        return self.store.prune(retention_days=self.config.conversation_ingest.retention_days)


def rejection_from_validation_error(error: ValidationError) -> list[RejectedEvent]:
    """Map a pydantic validation failure onto stable rejection codes."""
    rejected: list[RejectedEvent] = []
    for item in error.errors():
        location = ".".join(str(part) for part in item.get("loc", ()))
        message = str(item.get("msg") or "")
        error_type = str(item.get("type") or "")
        if error_type == "extra_forbidden":
            code = "invalid_payload"
        elif error_type in {"missing", "value_error"} and "event_type" in location:
            code = "invalid_event_type"
        elif "role" in location:
            code = "invalid_role"
        elif error_type == "missing":
            code = "missing_required_field"
        else:
            code = "invalid_payload"
        rejected.append(RejectedEvent(event_id="", code=code, message=f"{location}: {message}"))
    return rejected or [
        RejectedEvent(event_id="", code="invalid_payload", message="payload failed validation")
    ]
