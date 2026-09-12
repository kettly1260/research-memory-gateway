from __future__ import annotations

import hashlib
import json
import re
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from .models import (
    AttachmentRef,
    ExportSessionRef,
    NormalizedConversation,
    NormalizedMessage,
    ProvenanceValidation,
    ToolEvent,
)

TEXT_CONTENT_TYPES = {"input_text", "output_text", "text"}
TOOL_CALL_TYPES = {"function_call", "custom_tool_call"}
TOOL_OUTPUT_TYPES = {"function_call_output", "custom_tool_call_output"}
DATA_URI_RE = re.compile(r"data:[^;,\s]+(?:;[^,\s]+)*;base64,[A-Za-z0-9+/=\r\n]+", re.IGNORECASE)
INJECTION_PREFIX_RULES = (
    ("<recommended_plugins>", "recommended_plugins"),
    ("<app-context>", "app_context"),
    ("<environment_context>", "environment_context"),
    ("<skills_instructions>", "skills_instructions"),
    ("<turn_aborted>", "turn_aborted"),
    ("# AGENTS.md instructions for", "agents_instructions"),
    ("Another language model started to solve this problem", "handoff_summary"),
    ("The following is the Codex agent history whose request action you are assessing.", "guardian_transcript"),
)
INJECTED_USER_PREFIXES = tuple(p for p, _ in INJECTION_PREFIX_RULES)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _timestamp_from_epoch(value: Any) -> str:
    if value in (None, ""):
        return ""
    try:
        from datetime import datetime, timezone

        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return str(value)


class CodexExportReader:
    """Read a Codex session export ZIP without extracting or modifying it."""

    def __init__(self, archive_path: str | Path) -> None:
        self.archive_path = Path(archive_path)
        self._archive_sha256: str | None = None
        self._manifest: dict[str, Any] | None = None

    @property
    def archive_sha256(self) -> str:
        if self._archive_sha256 is None:
            digest = hashlib.sha256()
            with self.archive_path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            self._archive_sha256 = digest.hexdigest()
        return self._archive_sha256

    def manifest(self) -> dict[str, Any]:
        if self._manifest is None:
            with zipfile.ZipFile(self.archive_path) as archive:
                self._manifest = json.loads(archive.read("manifest.json"))
            if self._manifest.get("kind") != "codex-session-export":
                raise ValueError("Unsupported export kind")
        return self._manifest

    def sessions(self) -> list[ExportSessionRef]:
        refs: list[ExportSessionRef] = []
        for item in self.manifest().get("sessions", []):
            refs.append(
                ExportSessionRef(
                    conversation_id=str(item["sessionId"]),
                    title=str(item.get("title") or item["sessionId"]),
                    cwd=str(item.get("cwd") or ""),
                    updated_at=_timestamp_from_epoch(item.get("updatedAt")),
                    source_entry=str(item["fileEntry"]),
                    source_size_bytes=int(item.get("sizeBytes") or 0),
                    source_sha256=str(item.get("sha256") or ""),
                    relative_rollout_path=str(item.get("relativeRolloutPath") or ""),
                    source_instance=item.get("sourceInstance"),
                    session_index_entry=dict(item.get("sessionIndexEntry") or {}),
                )
            )
        return refs

    def get_session_ref(self, conversation_id: str) -> ExportSessionRef:
        for ref in self.sessions():
            if ref.conversation_id == conversation_id:
                return ref
        raise KeyError(f"Unknown conversation_id: {conversation_id}")

    def parse(self, conversation_id: str) -> NormalizedConversation:
        ref = self.get_session_ref(conversation_id)
        with zipfile.ZipFile(self.archive_path) as archive:
            raw = archive.read(ref.source_entry)
        actual_hash = _sha256_bytes(raw)
        if ref.source_sha256 and actual_hash != ref.source_sha256:
            raise ValueError(f"Source hash mismatch for {conversation_id}")

        record_types: Counter[str] = Counter()
        payload_types: Counter[str] = Counter()
        special: Counter[str] = Counter()
        event_mirror_counts: Counter[str] = Counter()
        turn_aborted_ordinals: list[int] = []
        has_turn_aborted = False
        messages: list[NormalizedMessage] = []
        tools: list[ToolEvent] = []
        attachments: list[AttachmentRef] = []
        json_errors: list[int] = []
        session_meta: dict[str, Any] = {}
        created_at = ""
        seen_messages: set[str] = set()

        for line_no, line in enumerate(raw.splitlines(), start=1):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                json_errors.append(line_no)
                continue
            if not isinstance(record, dict):
                special["non_object_records"] += 1
                continue
            outer_type = str(record.get("type") or "<missing>")
            record_types[outer_type] += 1
            ordinal = _coerce_int(record.get("ordinal"), line_no - 1)
            timestamp = str(record.get("timestamp") or "")
            payload = record.get("payload")
            if not isinstance(payload, dict):
                continue
            payload_type = str(payload.get("type") or "<missing>")
            payload_types[payload_type] += 1

            if outer_type == "turn_aborted" or payload_type == "turn_aborted":
                has_turn_aborted = True
                turn_aborted_ordinals.append(ordinal)
                special["turn_aborted_records"] += 1

            if outer_type == "session_meta":
                session_meta = dict(payload)
                created_at = str(payload.get("timestamp") or timestamp)
                continue

            if outer_type == "compacted" or payload_type in {"compaction", "compacted"}:
                special["compaction_records"] += 1
                if payload.get("encrypted_content"):
                    special["encrypted_compactions"] += 1

            if outer_type != "response_item":
                if outer_type == "event_msg" and payload_type == "item_completed":
                    item = payload.get("item")
                    if isinstance(item, dict):
                        itype = str(item.get("type", "<missing>"))
                        event_mirror_counts[itype] += 1
                        special[f"event_item:{itype}"] += 1
                continue

            if payload_type == "message":
                message = self._parse_message(payload, ordinal, timestamp)
                if message is not None:
                    if "<turn_aborted>" in message.text:
                        has_turn_aborted = True
                        turn_aborted_ordinals.append(ordinal)
                    dedupe_key = message.message_id or _sha256_text(
                        f"{message.role}\0{message.turn_id}\0{message.text}"
                    )
                    if dedupe_key in seen_messages:
                        special["duplicate_response_messages"] += 1
                    else:
                        seen_messages.add(dedupe_key)
                        messages.append(message)
                        if message.is_injected:
                            special["injected_user_messages"] += 1
                attachments.extend(self._parse_attachments(payload, ordinal))
            elif payload_type in TOOL_CALL_TYPES | TOOL_OUTPUT_TYPES:
                tools.append(self._parse_tool(payload, ordinal, timestamp))
            elif payload_type == "reasoning":
                special["reasoning_records"] += 1
                if payload.get("encrypted_content"):
                    special["encrypted_reasoning_records"] += 1

        if not created_at:
            created_at = str(ref.session_index_entry.get("updated_at") or ref.updated_at)

        assistants = [m for m in messages if m.role == "assistant"]
        final_answers = [m for m in assistants if m.phase == "final_answer"]
        if final_answers:
            last_final_ordinal = final_answers[-1].ordinal
            if turn_aborted_ordinals and max(turn_aborted_ordinals) > last_final_ordinal:
                completion_status = "aborted"
                completion_reason = "aborted_after_final_answer"
            else:
                completion_status = "complete"
                completion_reason = "final_answer_present"
        elif has_turn_aborted:
            completion_status = "aborted"
            completion_reason = "turn_aborted"
        elif assistants:
            completion_status = "incomplete_or_unknown"
            completion_reason = "no_final_answer"
        else:
            completion_status = "incomplete_or_unknown"
            completion_reason = "no_assistant_message"

        manifest_data = self.manifest()
        provenance = ProvenanceValidation(
            archive_path=str(self.archive_path.resolve()),
            archive_sha256=self.archive_sha256,
            entry_path=ref.source_entry,
            entry_size_bytes=len(raw),
            entry_expected_sha256=ref.source_sha256,
            entry_actual_sha256=actual_hash,
            hash_matched=(not ref.source_sha256 or actual_hash == ref.source_sha256),
            manifest_kind=str(manifest_data.get("kind") or ""),
            manifest_package_version=int(manifest_data.get("packageVersion") or 1),
        )

        return NormalizedConversation(
            ref=ref,
            archive_path=str(self.archive_path.resolve()),
            archive_sha256=self.archive_sha256,
            created_at=created_at,
            session_meta=session_meta,
            messages=messages,
            tools=tools,
            attachments=_dedupe_attachments(attachments),
            record_type_counts=dict(record_types),
            payload_type_counts=dict(payload_types),
            special_counts=dict(special),
            event_mirror_counts=dict(event_mirror_counts),
            json_errors=json_errors,
            provenance=provenance,
            completion_status=completion_status,
            completion_reason=completion_reason,
            has_turn_aborted=has_turn_aborted,
        )

    def parse_many(self, conversation_ids: Iterable[str]) -> list[NormalizedConversation]:
        return [self.parse(conversation_id) for conversation_id in conversation_ids]

    def _parse_message(
        self, payload: dict[str, Any], ordinal: int, timestamp: str
    ) -> NormalizedMessage | None:
        texts: list[str] = []
        for content in payload.get("content", []):
            if not isinstance(content, dict) or content.get("type") not in TEXT_CONTENT_TYPES:
                continue
            text = content.get("text")
            if isinstance(text, str) and text.strip():
                texts.append(text.rstrip())
        if not texts:
            return None
        metadata = payload.get("internal_chat_message_metadata_passthrough")
        turn_id = str(metadata.get("turn_id") or "") if isinstance(metadata, dict) else ""
        role = str(payload.get("role") or "unknown")
        full_text = "\n\n".join(texts)
        is_injected, injection_reason = _classify_injection(role, full_text)
        return NormalizedMessage(
            ordinal=ordinal,
            timestamp=timestamp,
            role=role,
            text=full_text,
            message_id=str(payload.get("id") or ""),
            turn_id=turn_id,
            phase=str(payload.get("phase") or ""),
            is_injected=is_injected,
            injection_reason=injection_reason,
        )

    def _parse_attachments(
        self, payload: dict[str, Any], ordinal: int
    ) -> list[AttachmentRef]:
        refs: list[AttachmentRef] = []
        message_id = str(payload.get("id") or "")
        for content in payload.get("content", []):
            if not isinstance(content, dict):
                continue
            content_type = str(content.get("type") or "")
            if content_type in TEXT_CONTENT_TYPES or not content_type:
                continue
            locator_value = _first_locator(content)
            locator_json = _stable_json(locator_value)
            is_embedded = isinstance(locator_value, str) and locator_value.startswith("data:")
            locator = "embedded-data" if is_embedded else _display_locator(locator_value)
            content_hash = _sha256_text(locator_json)
            metadata = {
                key: value
                for key, value in content.items()
                if key not in {"image_url", "url", "uri", "data", "content"}
                and isinstance(value, (str, int, float, bool))
            }
            refs.append(
                AttachmentRef(
                    attachment_id=f"att_{content_hash[:16]}",
                    ordinal=ordinal,
                    message_id=message_id,
                    content_type=content_type,
                    locator=locator,
                    content_hash=content_hash,
                    size_bytes=len(locator_value.encode("utf-8"))
                    if isinstance(locator_value, str)
                    else None,
                    metadata=metadata,
                )
            )
        return refs

    def _parse_tool(self, payload: dict[str, Any], ordinal: int, timestamp: str) -> ToolEvent:
        payload_type = str(payload.get("type") or "")
        direction = "call" if payload_type in TOOL_CALL_TYPES else "output"
        if direction == "call":
            value = payload.get("arguments", payload.get("input", ""))
        else:
            value = payload.get("output", payload.get("content", ""))
        serialized = value if isinstance(value, str) else _stable_json(value)
        excerpt_source = serialized[:2000].replace("\x00", "")
        excerpt = DATA_URI_RE.sub(
            lambda match: f"<embedded-data:{len(match.group(0))} chars>", excerpt_source
        )[:500]
        return ToolEvent(
            ordinal=ordinal,
            timestamp=timestamp,
            event_type=payload_type,
            direction=direction,
            name=str(payload.get("name") or payload.get("tool_name") or ""),
            call_id=str(payload.get("call_id") or payload.get("id") or ""),
            payload_chars=len(serialized),
            payload_hash=_sha256_text(serialized),
            excerpt=excerpt,
        )


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _first_locator(content: dict[str, Any]) -> Any:
    for key in ("image_url", "url", "uri", "file_path", "path", "data", "content"):
        if key in content:
            return content[key]
    return content


def _display_locator(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("url", "uri", "path"):
            if isinstance(value.get(key), str):
                return value[key]
    return _stable_json(value)[:500]


def _dedupe_attachments(items: list[AttachmentRef]) -> list[AttachmentRef]:
    deduped: list[AttachmentRef] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        key = (item.content_type, item.content_hash)
        if key not in seen:
            seen.add(key)
            deduped.append(item)
    return deduped


def _classify_injection(role: str, text: str) -> tuple[bool, str]:
    if role == "developer":
        return True, "developer_instructions"
    if role != "user":
        return False, ""
    stripped = text.lstrip()
    for prefix, reason in INJECTION_PREFIX_RULES:
        if stripped.startswith(prefix):
            return True, reason
    return False, ""


def _is_injected_user_message(role: str, text: str) -> bool:
    injected, _ = _classify_injection(role, text)
    return injected
