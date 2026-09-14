from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .attachments import AttachmentInventory
from .decisions import (
    CONFLICT,
    DRY_RUN,
    HYDRATE,
    INDEX,
    NEW_SOURCE,
    REBUILD,
    SKIP,
    WRITE,
    SourceImportDecision,
    decide_source_import,
    effective_identity,
)
from .duplicates import run_candidate_detection
from .identity import TranscriptFingerprints, compute_transcript_fingerprints
from .identity_store import ConversationIdentityStore, SourceRecord
from .manifest import ImportManifest
from .models import ExportSessionRef, NormalizedConversation
from .readers import ConversationExportReader
from .vault_writer import ObsidianConversationWriter

# skip reasons that a --no-resume forced reimport may override.  A stale
# snapshot is never overridden: an older/shorter export must not truncate a
# newer stored note even when the operator explicitly forces a rewrite.
FORCEABLE_SKIP_REASONS = {
    "unchanged",
    "same_source_in_new_archive",
    "source_packaging_changed",
}


@dataclass(frozen=True)
class IngestionResult:
    conversation_id: str
    status: str
    reason: str
    output_path: str = ""
    error: str = ""


class ConversationIngestionPipeline:
    """Multi-source ingestion pipeline.

    The pipeline is platform-agnostic: it consumes any
    ``ConversationExportReader`` and stores source-scoped identity in the
    additive v2 identity tables while keeping the legacy
    ``conversation_imports`` ledger in sync for Codex-only compatibility.
    """

    def __init__(
        self,
        reader: ConversationExportReader,
        writer: ObsidianConversationWriter,
        manifest: ImportManifest,
        attachment_inventory: AttachmentInventory | None = None,
        identity_store: ConversationIdentityStore | None = None,
    ) -> None:
        self.reader = reader
        self.writer = writer
        self.manifest = manifest
        # Default is intentionally deny-by-default for local file reads.  The
        # production CLI injects cfg.sources.allowlist explicitly.
        self.attachment_inventory = attachment_inventory or AttachmentInventory()
        self.identity_store = identity_store or ConversationIdentityStore(manifest.path)

    def run(
        self,
        import_keys: list[str],
        *,
        dry_run: bool = False,
        resume: bool = True,
    ) -> list[IngestionResult]:
        """Import the selected reader items.

        Each entry is a reader lookup key: the session ref's
        ``effective_import_key``.  For Codex this is the bare provider
        conversation id; branched sources pass one key per branch.
        """
        results: list[IngestionResult] = []
        for import_key in import_keys:
            try:
                ref = self.reader.get_session_ref(import_key)
            except Exception as exc:
                results.append(
                    IngestionResult(import_key, "failed_retryable", "unknown_session", error=str(exc))
                )
                continue

            conversation = None
            att_hash = ""
            parse_error = ""
            try:
                conversation = self.reader.parse(import_key)
                inventory_records = self.attachment_inventory.scan_one(conversation)
                att_hash = AttachmentInventory.inventory_hash(inventory_records)
            except Exception as exc:
                # Parsing errors fall back to the retryable failure path.
                conversation = None
                parse_error = str(exc)

            if conversation is None:
                results.append(self._handle_parse_failure(ref, parse_error))
                continue

            fingerprints = compute_transcript_fingerprints(conversation)
            legacy_record = self.manifest.get_record(self._legacy_record_key(ref))
            decision = decide_source_import(
                store=self.identity_store,
                ref=ref,
                fingerprints=fingerprints,
                archive_sha256=self.reader.archive_sha256,
                parser_version=self.reader.parser_version,
                schema_version=self.reader.schema_version,
                legacy_record=legacy_record,
                attachment_inventory_hash=att_hash or None,
                dry_run=dry_run,
            )
            if (
                not resume
                and decision.action == SKIP
                and decision.reason in FORCEABLE_SKIP_REASONS
            ):
                decision = SourceImportDecision(
                    WRITE,
                    "forced_reimport",
                    decision.output_path,
                    source_key=decision.source_key,
                    canonical_conversation_id=decision.canonical_conversation_id,
                    source_record=decision.source_record,
                )
            if dry_run or decision.action == DRY_RUN:
                results.append(
                    IngestionResult(import_key, "dry_run", decision.reason, decision.output_path)
                )
                continue

            if decision.action == SKIP:
                results.append(
                    self._apply_skip(
                        ref, decision, fingerprints=fingerprints,
                        att_hash=att_hash, legacy_record=legacy_record,
                    )
                )
                continue
            if decision.action == HYDRATE:
                results.append(
                    self._apply_hydrate(ref, decision, fingerprints=fingerprints)
                )
                continue
            if decision.action == INDEX:
                results.append(
                    IngestionResult(import_key, "index_stale", decision.reason, decision.output_path)
                )
                continue
            if decision.action == CONFLICT:
                candidate_path = decision.output_path
                try:
                    candidate_path = str(self.writer.write_candidate(conversation))
                except Exception:
                    candidate_path = decision.output_path
                results.append(
                    IngestionResult(import_key, "conflict", decision.reason, candidate_path)
                )
                continue
            if decision.action in {NEW_SOURCE, WRITE, REBUILD}:
                results.append(
                    self._apply_write(
                        ref, conversation, fingerprints, decision,
                        att_hash=att_hash, legacy_record=legacy_record,
                    )
                )
                continue
            # Unknown action: fail closed without touching anything.
            results.append(
                IngestionResult(import_key, "failed_retryable", decision.reason, decision.output_path)
            )
        return results

    # ------------------------------------------------------------------ paths

    def _legacy_record_key(self, ref: ExportSessionRef) -> str:
        """Ledger row key for one session ref.

        Codex keeps the historical bare provider conversation id (the legacy
        table is Codex-keyed compatibility data).  Branched sources (ChatGPT)
        use a branch-scoped key so sibling branches never fight over one row.
        """
        if (ref.source_system or "codex") == "codex":
            return ref.conversation_id
        return ref.effective_import_key

    def _handle_parse_failure(self, ref: ExportSessionRef, parse_error: str) -> IngestionResult:
        """Conservative fallback when a session cannot be parsed.

        If the stored source entry hash matches this export and the managed
        note is still on disk, treat the session as unchanged (mirrors the
        v0.2.3 resume semantics).  Otherwise report a retryable failure and
        record it in the legacy ledger so the next resume run retries; the
        pipeline never guesses continuation/stale without a transcript.
        """
        record = self._find_record_for_ref(ref)
        legacy = self.manifest.get_record(self._legacy_record_key(ref))
        if (
            record is not None
            and record.last_source_entry_sha256 == ref.source_sha256
            and record.output_path
            and Path(record.output_path).is_file()
            and (legacy is None or legacy.get("status") not in {"failed", "failed_retryable"})
        ):
            return IngestionResult(ref.conversation_id, "skipped", "unchanged", record.output_path)
        fallback_output_path = (legacy or {}).get("output_path") or ""
        if not fallback_output_path:
            try:
                fallback_output_path = str(self.writer.target_path_from_ref(ref))
            except Exception:
                fallback_output_path = ""
        self.manifest.record(
            ref,
            archive_path=str(self.reader.archive_path.resolve()),
            archive_sha256=self.reader.archive_sha256,
            output_path=fallback_output_path,
            status="failed_retryable",
            error=parse_error,
            parser_version=self.reader.parser_version,
            schema_version=self.reader.schema_version,
            record_key=self._legacy_record_key(ref),
        )
        return IngestionResult(ref.conversation_id, "failed_retryable", "parse_error", error=parse_error)

    def _find_record_for_ref(self, ref: ExportSessionRef) -> SourceRecord | None:
        """Locate a stored source record for a ref without fingerprints.

        Provider-id-backed identities can be looked up directly; synthetic
        content-derived ids cannot be reconstructed without a transcript, so
        they return None here (fail closed).
        """
        if not ref.source_conversation_id.strip():
            return None
        from .identity import ConversationSourceIdentity

        identity = ConversationSourceIdentity(
            source_system=ref.source_system or "codex",
            source_account_namespace_hash=ref.source_account_namespace_hash,
            source_conversation_id=ref.source_conversation_id,
            source_thread_id=ref.source_thread_id,
            source_branch_id=ref.source_branch_id,
        )
        return self.identity_store.find_source_record_by_identity(identity)

    def _apply_skip(
        self,
        ref: ExportSessionRef,
        decision: SourceImportDecision,
        *,
        fingerprints: TranscriptFingerprints,
        att_hash: str = "",
        legacy_record: dict | None,
    ) -> IngestionResult:
        store = self.identity_store
        record = decision.source_record
        if record is None:
            return IngestionResult(ref.conversation_id, "skipped", decision.reason, decision.output_path)
        archive_sha = self.reader.archive_sha256
        if decision.reason in {"unchanged", "same_source_in_new_archive"}:
            store.mark_seen(record.source_key, archive_sha256=archive_sha)
            entry_sha = record.last_source_entry_sha256
            if entry_sha:
                store.record_snapshot(
                    record.source_key,
                    archive_sha256=archive_sha,
                    source_entry_sha256=entry_sha,
                    fingerprints=fingerprints,
                )
        elif decision.reason == "source_packaging_changed":
            store.mark_seen(
                record.source_key,
                archive_sha256=archive_sha,
                last_source_entry_sha256=ref.source_sha256,
            )
            store.record_snapshot(
                record.source_key,
                archive_sha256=archive_sha,
                source_entry_sha256=ref.source_sha256,
                fingerprints=fingerprints,
            )
            if legacy_record is not None:
                self._sync_legacy_record(
                    ref,
                    record.output_path,
                    att_hash=att_hash,
                    status=str(legacy_record.get("status") or "written"),
                )
        elif decision.reason == "stale_snapshot":
            # Record the sighting of the older snapshot exactly as it arrived
            # (its own entry hash and its own fingerprints) but never adopt
            # it: the stored newer version and note stay untouched.
            store.record_snapshot(
                record.source_key,
                archive_sha256=archive_sha,
                source_entry_sha256=ref.source_sha256,
                fingerprints=fingerprints,
            )
            store.mark_seen(record.source_key, archive_sha256=archive_sha)
        return IngestionResult(ref.conversation_id, "skipped", decision.reason, decision.output_path)

    def _apply_hydrate(
        self,
        ref: ExportSessionRef,
        decision: SourceImportDecision,
        *,
        fingerprints: TranscriptFingerprints,
    ) -> IngestionResult:
        """First-sight hydration of a migrated legacy record (metadata only)."""
        record = decision.source_record
        if record is None:
            return IngestionResult(ref.conversation_id, "skipped", decision.reason, decision.output_path)
        self.identity_store.hydrate_source_record(
            record.source_key,
            fingerprints=fingerprints,
            archive_sha256=self.reader.archive_sha256,
            source_entry_sha256=ref.source_sha256,
        )
        return IngestionResult(ref.conversation_id, "skipped", decision.reason, decision.output_path)

    def _apply_write(
        self,
        ref: ExportSessionRef,
        conversation: NormalizedConversation,
        fingerprints: TranscriptFingerprints,
        decision: SourceImportDecision,
        *,
        att_hash: str = "",
        legacy_record: dict | None,
    ) -> IngestionResult:
        store = self.identity_store
        archive_sha = self.reader.archive_sha256
        entry_sha = ref.source_sha256
        parser_version = self.reader.parser_version
        schema_version = self.reader.schema_version
        source_meta = self._source_meta_for(ref, fingerprints, decision, parser_version, schema_version)
        try:
            if decision.action == NEW_SOURCE:
                try:
                    path = self.writer.write(conversation, source_meta=source_meta)
                except FileExistsError:
                    # The derived filename is owned by a different source
                    # record (same bare id on another platform).  Fall back to
                    # the deterministic source-key-suffixed path instead of
                    # ever taking over that note.
                    alt_path = self.writer.target_path_for_source_key(
                        conversation, str(source_meta.get("source_key") or "")
                    )
                    path = self.writer.write(
                        conversation, overwrite_managed=True, path_override=alt_path, source_meta=source_meta
                    )
            else:
                stable_output_path = decision.output_path or None
                # Once a conversation has been imported, keep its canonical
                # output path stable across later full exports.  A continued
                # conversation may gain messages and even a new title, but it
                # must update the existing managed note rather than leave an
                # orphaned duplicate under a newly derived filename.
                path = self.writer.write(
                    conversation,
                    overwrite_managed=True,
                    path_override=stable_output_path,
                    source_meta=source_meta,
                )
        except Exception as exc:
            fallback_output_path = decision.output_path
            if not fallback_output_path:
                try:
                    fallback_output_path = str(self.writer.target_path_from_ref(ref))
                except Exception:
                    fallback_output_path = ""
            self._sync_legacy_record(
                ref,
                fallback_output_path,
                att_hash=att_hash,
                status="failed_retryable",
                error=str(exc),
            )
            return IngestionResult(
                ref.conversation_id,
                "failed_retryable",
                decision.reason,
                fallback_output_path,
                error=str(exc),
            )

        if decision.action == NEW_SOURCE:
            identity, _ = effective_identity(ref, fingerprints)
            reuse_record = decision.source_record
            if reuse_record is not None:
                # Exact same-content reuse for id-less providers: adopt the
                # existing source record and canonical, refresh content hash.
                store.update_source_content(
                    reuse_record.source_key,
                    fingerprints=fingerprints,
                    archive_sha256=archive_sha,
                    source_entry_sha256=entry_sha,
                    parser_version=parser_version,
                    schema_version=schema_version,
                )
                store.set_output_path(reuse_record.source_key, str(path))
            else:
                source_record = store.create_source_record(
                    identity,
                    fingerprints=fingerprints,
                    output_path=str(path),
                    archive_sha256=archive_sha,
                    source_entry_sha256=entry_sha,
                    parser_version=parser_version,
                    schema_version=schema_version,
                    canonical_id=decision.canonical_conversation_id,
                )
                # Candidate detection never blocks source provenance landing.
                run_candidate_detection(
                    store,
                    source_record,
                    fingerprints=fingerprints,
                    title=ref.title,
                    updated_at=ref.updated_at,
                )
        else:
            record = decision.source_record
            if decision.reason == "source_continued":
                store.update_source_content(
                    record.source_key,
                    fingerprints=fingerprints,
                    archive_sha256=archive_sha,
                    source_entry_sha256=entry_sha,
                    parser_version=parser_version,
                    schema_version=schema_version,
                )
                store.record_snapshot(
                    record.source_key,
                    archive_sha256=archive_sha,
                    source_entry_sha256=entry_sha,
                    fingerprints=fingerprints,
                )
            else:
                # output_missing / failed_retryable / attachment_changed /
                # parser_upgrade / forced reimport on an unchanged entry.
                if record.fingerprint_version == 0:
                    # The rewritten/refreshed note gives us a verifiable
                    # transcript for a migrated-but-unhydrated record: hydrate
                    # identity metadata together with the write.
                    self.identity_store.hydrate_source_record(
                        record.source_key,
                        fingerprints=fingerprints,
                        archive_sha256=archive_sha,
                        source_entry_sha256=entry_sha,
                        parser_version=parser_version,
                        schema_version=schema_version,
                    )
                else:
                    store.mark_seen(
                        record.source_key,
                        archive_sha256=archive_sha,
                        last_source_entry_sha256=entry_sha,
                    )
            store.set_output_path(record.source_key, str(path))

        prior_record = legacy_record or {}
        record_status = "written"
        if (
            prior_record.get("last_indexed_at")
            and decision.reason in {"source_continued", "parser_upgrade"}
        ):
            record_status = "index_stale"

        self._sync_legacy_record(
            ref,
            str(path),
            att_hash=att_hash,
            status=record_status,
            parent_thread_id=conversation.parent_thread_id,
        )
        return IngestionResult(ref.conversation_id, "written", decision.reason, str(path))

    def _sync_legacy_record(
        self,
        ref: ExportSessionRef,
        output_path: str,
        *,
        status: str,
        att_hash: str = "",
        parent_thread_id: str = "",
        error: str = "",
    ) -> None:
        """Keep the legacy ledger in sync without cross-system key theft.

        Codex rows are keyed by the bare provider conversation id, which
        several source systems may share: only write when this source record
        is the sole owner of that id in the v2 store.  Branched sources write
        branch-scoped rows, so no shared row exists to steal.
        """
        if not ref.source_conversation_id.strip():
            return
        legacy_key = self._legacy_record_key(ref)
        if legacy_key == ref.conversation_id:
            occupants = self.identity_store.find_source_records_by_conversation(
                ref.source_conversation_id
            )
            if occupants:
                current = self._find_record_for_ref(ref)
                if current is None or {o.source_key for o in occupants} != {current.source_key}:
                    return
        self.manifest.record(
            ref,
            archive_path=str(self.reader.archive_path.resolve()),
            archive_sha256=self.reader.archive_sha256,
            output_path=output_path,
            attachment_inventory_hash=att_hash,
            parent_thread_id=parent_thread_id,
            status=status,
            error=error,
            parser_version=self.reader.parser_version,
            schema_version=self.reader.schema_version,
            record_key=legacy_key,
        )

    def _source_meta_for(
        self,
        ref: ExportSessionRef,
        fingerprints: TranscriptFingerprints,
        decision: SourceImportDecision,
        parser_version: str,
        schema_version: str,
    ) -> dict[str, str | int]:
        """Identity metadata rendered into the note frontmatter (v0.2.4)."""
        record = decision.source_record
        if record is not None:
            canonical = record.canonical_conversation_id
            source_key = record.source_key
        else:
            identity, _ = effective_identity(ref, fingerprints)
            canonical = decision.canonical_conversation_id or ""
            source_key = identity.source_key
        return {
            "canonical_conversation_id": canonical,
            "source_key": source_key,
            "source_conversation_id": ref.source_conversation_id,
            "source_thread_id": ref.source_thread_id,
            "source_branch_id": ref.source_branch_id,
            "parser_version": parser_version,
            "schema_version": schema_version,
            "fingerprint_version": fingerprints.fingerprint_version,
        }
