from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .attachments import AttachmentInventory
from .codex_export import CodexExportReader
from .manifest import ImportManifest
from .vault_writer import ObsidianConversationWriter


@dataclass(frozen=True)
class IngestionResult:
    conversation_id: str
    status: str
    reason: str
    output_path: str = ""
    error: str = ""


class ConversationIngestionPipeline:
    def __init__(
        self,
        reader: CodexExportReader,
        writer: ObsidianConversationWriter,
        manifest: ImportManifest,
        attachment_inventory: AttachmentInventory | None = None,
    ) -> None:
        self.reader = reader
        self.writer = writer
        self.manifest = manifest
        # Default is intentionally deny-by-default for local file reads.  The
        # production CLI injects cfg.sources.allowlist explicitly.
        self.attachment_inventory = attachment_inventory or AttachmentInventory()

    def run(
        self,
        conversation_ids: list[str],
        *,
        dry_run: bool = False,
        resume: bool = True,
    ) -> list[IngestionResult]:
        results: list[IngestionResult] = []
        for conversation_id in conversation_ids:
            ref = self.reader.get_session_ref(conversation_id)
            conversation = None
            att_hash = ""
            try:
                conversation = self.reader.parse(conversation_id)
                inventory_records = self.attachment_inventory.scan_one(conversation)
                att_hash = AttachmentInventory.inventory_hash(inventory_records)
            except Exception:
                # Parsing errors are handled by the normal retryable failure path.
                conversation = None
            decision = self.manifest.decide(
                ref,
                archive_sha256=self.reader.archive_sha256,
                attachment_inventory_hash=att_hash,
                dry_run=dry_run,
            )
            if not resume and decision.action == "skip":
                decision = type(decision)("write", "forced_reimport", decision.output_path)
            if dry_run or decision.action == "dry_run":
                results.append(
                    IngestionResult(conversation_id, "dry_run", decision.reason, decision.output_path)
                )
                continue
            if decision.action == "skip":
                results.append(
                    IngestionResult(conversation_id, "skipped", decision.reason, decision.output_path)
                )
                continue
            if decision.action == "index":
                # Import and indexing are separate responsibilities.  Surface
                # the stale state without rewriting the Markdown note.
                results.append(
                    IngestionResult(conversation_id, "index_stale", decision.reason, decision.output_path)
                )
                continue
            if decision.action == "conflict":
                try:
                    conv = conversation or self.reader.parse(conversation_id)
                    candidate_path = self.writer.write_candidate(conv)
                    cand_str = str(candidate_path)
                except Exception:
                    cand_str = decision.output_path
                results.append(
                    IngestionResult(conversation_id, "conflict", decision.reason, cand_str)
                )
                continue
            try:
                conversation = conversation or self.reader.parse(conversation_id)
                prior_record = self.manifest.get_record(conversation_id)
                path = self.writer.write(conversation, overwrite_managed=True)
                if not att_hash:
                    att_hash = AttachmentInventory.inventory_hash(
                        self.attachment_inventory.scan_one(conversation)
                    )
                record_status = "written"
                if (
                    prior_record
                    and prior_record.get("last_indexed_at")
                    and decision.reason in {"source_changed", "parser_upgrade"}
                ):
                    record_status = "index_stale"
                self.manifest.record(
                    ref,
                    archive_path=str(self.reader.archive_path.resolve()),
                    archive_sha256=self.reader.archive_sha256,
                    output_path=path,
                    parent_thread_id=conversation.parent_thread_id,
                    attachment_inventory_hash=att_hash,
                    status=record_status,
                )
                results.append(IngestionResult(conversation_id, "written", decision.reason, str(path)))
            except Exception as exc:
                fallback_output_path = decision.output_path
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
                    error=str(exc),
                )
                results.append(
                    IngestionResult(
                        conversation_id,
                        "failed_retryable",
                        decision.reason,
                        fallback_output_path,
                        error=str(exc),
                    )
                )
        return results
