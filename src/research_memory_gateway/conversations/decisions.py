"""Import decision state machine for multi-source conversation ingestion.

v0.2.4 W4.  Given one export session ref plus its computed fingerprints, this
module decides what the ingestion pipeline must do, using the additive v2
identity tables.  Decisions are conservative and fail closed:

* unchanged / repackaged exports            -> skip
* strict continuation (old is prefix of new)-> update existing note in place
* stale snapshot (new is prefix of old)     -> skip, never truncate the note
* divergence without branch identity        -> conflict / review, never overwrite
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .identity import (
    FINGERPRINT_VERSION,
    TranscriptFingerprints,
    canonical_conversation_id_for,
    sequence_relation,
)
from .identity_store import ConversationIdentityStore, SourceRecord
from .models import ExportSessionRef
from .vault_writer import compute_managed_hash

NEW_SOURCE = "new"
WRITE = "write"
REBUILD = "rebuild"
SKIP = "skip"
CONFLICT = "conflict"
INDEX = "index"
DRY_RUN = "dry_run"


@dataclass(frozen=True)
class SourceImportDecision:
    action: str
    reason: str
    output_path: str = ""
    source_key: str = ""
    canonical_conversation_id: str = ""
    source_record: SourceRecord | None = None
    detail: dict[str, Any] | None = None

    @property
    def status(self) -> str:
        return self.action


def effective_identity(
    ref: ExportSessionRef, fingerprints: TranscriptFingerprints
) -> tuple[Any, bool]:
    """Return (identity, id_is_synthetic) for decision making.

    When the provider did not supply a stable conversation id, the identity is
    derived deterministically from the normalized transcript content.  Two
    full exports of the same unnamed conversation then share one source key
    (exact same-content dedup) while different content never collides.
    """
    from .identity import ConversationSourceIdentity

    conversation_id = ref.source_conversation_id.strip()
    if conversation_id:
        return (
            ConversationSourceIdentity(
                source_system=ref.source_system or "codex",
                source_account_namespace_hash=ref.source_account_namespace_hash,
                source_conversation_id=conversation_id,
                source_thread_id=ref.source_thread_id,
                source_branch_id=ref.source_branch_id,
            ),
            False,
        )
    return (
        ConversationSourceIdentity(
            source_system=ref.source_system or "codex",
            source_account_namespace_hash=ref.source_account_namespace_hash,
            source_conversation_id=f"synthetic-content-{fingerprints.normalized_transcript_sha256[:32]}",
            source_thread_id=ref.source_thread_id,
            source_branch_id=ref.source_branch_id,
        ),
        True,
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _index_stale(
    legacy: dict[str, Any] | None,
    output_path: str,
    *,
    check_index: bool,
) -> SourceImportDecision | None:
    if legacy is None:
        return None
    if legacy.get("status") == "index_stale":
        return SourceImportDecision(INDEX, "index_stale", output_path)
    has_index_state = bool(legacy.get("last_indexed_at") and legacy.get("index_source_hash"))
    if check_index and not has_index_state:
        return SourceImportDecision(INDEX, "index_stale", output_path)
    if has_index_state:
        path = Path(output_path) if output_path else None
        if path is None or not path.is_file():
            return None
        if legacy["index_source_hash"] != _file_sha256(path):
            return SourceImportDecision(INDEX, "index_stale", output_path)
    return None


def _note_integrity_decision(
    legacy: dict[str, Any] | None,
    output_path: str,
    *,
    attachment_inventory_hash: str | None,
    check_index: bool,
) -> SourceImportDecision | None:
    """Managed-note integrity checks shared by the unchanged-entry paths."""
    if legacy is not None and legacy.get("status") in {"failed", "failed_retryable"}:
        return SourceImportDecision(WRITE, "failed_retryable", output_path)

    path = Path(output_path) if output_path else None
    if path is None or not path.exists() or not path.is_file():
        return SourceImportDecision(WRITE, "output_missing", output_path)

    if legacy is not None:
        current_text = path.read_text(encoding="utf-8")
        current_managed_hash = compute_managed_hash(current_text)
        stored_managed_hash = legacy.get("managed_output_sha256")
        if stored_managed_hash:
            if current_managed_hash != stored_managed_hash:
                return SourceImportDecision(CONFLICT, "managed_output_modified", output_path)
        elif legacy.get("output_sha256"):
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest != legacy["output_sha256"]:
                return SourceImportDecision(CONFLICT, "managed_output_modified", output_path)

        stored_att = legacy.get("attachment_inventory_hash") or ""
        if attachment_inventory_hash and stored_att and stored_att != attachment_inventory_hash:
            return SourceImportDecision(WRITE, "attachment_changed", output_path)

    return None


def decide_source_import(
    *,
    store: ConversationIdentityStore,
    ref: ExportSessionRef,
    fingerprints: TranscriptFingerprints,
    archive_sha256: str,
    parser_version: str,
    schema_version: str,
    legacy_record: dict[str, Any] | None = None,
    attachment_inventory_hash: str | None = None,
    check_index: bool = False,
    dry_run: bool = False,
) -> SourceImportDecision:
    identity, synthetic_id = effective_identity(ref, fingerprints)
    source_key = identity.source_key
    record = store.find_source_record_by_identity(identity)

    if record is None:
        # Same-source exact-transcript reuse for id-less providers (7.2B):
        # only when both sides lack a provider id and the normalized
        # transcript fingerprint matches exactly.
        if synthetic_id:
            for candidate in store.list_source_records(
                source_system=identity.source_system
            ):
                if candidate.source_account_namespace_hash != identity.source_account_namespace_hash:
                    continue
                if candidate.normalized_transcript_sha256 != fingerprints.normalized_transcript_sha256:
                    continue
                if candidate.source_conversation_id.startswith("synthetic-content-"):
                    return SourceImportDecision(
                        WRITE,
                        "same_source_missing_id_reused",
                        candidate.output_path,
                        source_key=candidate.source_key,
                        canonical_conversation_id=candidate.canonical_conversation_id,
                        source_record=candidate,
                    )
        canonical = canonical_conversation_id_for(source_key)
        return SourceImportDecision(
            NEW_SOURCE, "new_source_record", source_key=source_key, canonical_conversation_id=canonical
        )

    output_path = record.output_path

    # The legacy ledger row is keyed by the bare provider id, which several
    # source systems may share.  Only trust it for note-integrity/index checks
    # when it clearly points at *this* source record's note.
    trusted_legacy: dict[str, Any] | None = None
    if legacy_record is not None:
        legacy_output = str(legacy_record.get("output_path") or "")
        if legacy_output and record.output_path:
            try:
                same_note = Path(legacy_output).resolve() == Path(record.output_path).resolve()
            except (OSError, ValueError):
                same_note = False
            if same_note:
                trusted_legacy = legacy_record

    parser_changed = bool(
        (parser_version and record.parser_version and parser_version != record.parser_version)
        or (schema_version and record.schema_version and schema_version != record.schema_version)
    )

    same_entry = bool(
        record.last_source_entry_sha256 and record.last_source_entry_sha256 == ref.source_sha256
    )

    if same_entry:
        integrity = _note_integrity_decision(
            trusted_legacy,
            output_path,
            attachment_inventory_hash=attachment_inventory_hash,
            check_index=check_index,
        )
        if integrity is not None:
            return _with_identity(integrity, record)
        stale = _index_stale(
            trusted_legacy, output_path, check_index=check_index
        )
        if stale is not None:
            return _with_identity(stale, record)
        reason = (
            "unchanged"
            if record.last_seen_archive_sha256 == archive_sha256
            else "same_source_in_new_archive"
        )
        return _with_identity(SourceImportDecision(SKIP, reason, output_path), record)

    # Different raw entry hash: compare normalized transcripts.
    if record.fingerprint_version != FINGERPRINT_VERSION:
        return _with_identity(
            SourceImportDecision(
                CONFLICT,
                "fingerprint_version_mismatch",
                output_path,
                detail={
                    "record_fingerprint_version": record.fingerprint_version,
                    "current_fingerprint_version": FINGERPRINT_VERSION,
                },
            ),
            record,
        )

    if record.normalized_transcript_sha256 == fingerprints.normalized_transcript_sha256:
        if parser_changed:
            action = DRY_RUN if dry_run else REBUILD
            return _with_identity(
                SourceImportDecision(action, "parser_upgrade", output_path), record
            )
        return _with_identity(
            SourceImportDecision(SKIP, "source_packaging_changed", output_path), record
        )

    stored_sequence = store.message_fingerprint_sequence(source_key)
    relation = sequence_relation(stored_sequence, list(fingerprints.message_fingerprints))
    if relation == "equal":
        return _with_identity(
            SourceImportDecision(SKIP, "source_packaging_changed", output_path), record
        )
    if relation == "left_strict_prefix":
        # Stored sequence is shorter: the new export continues the conversation.
        return _with_identity(
            SourceImportDecision(WRITE, "source_continued", output_path), record
        )
    if relation == "right_strict_prefix":
        # The incoming export is a shorter/older snapshot of the stored one.
        # Never truncate the stored note.
        return _with_identity(SourceImportDecision(SKIP, "stale_snapshot", output_path), record)
    return _with_identity(SourceImportDecision(CONFLICT, "source_diverged", output_path), record)


def _with_identity(decision: SourceImportDecision, record: SourceRecord) -> SourceImportDecision:
    return SourceImportDecision(
        action=decision.action,
        reason=decision.reason,
        output_path=decision.output_path or record.output_path,
        source_key=record.source_key,
        canonical_conversation_id=record.canonical_conversation_id,
        source_record=record,
        detail=decision.detail,
    )
