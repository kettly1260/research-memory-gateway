from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

from .models import AttachmentRef, NormalizedConversation

PATH_PATTERN = re.compile(r'(?:[A-Za-z]:[\\/][^:*?"<>|\s]+)|(?:/(?:Users|home|tmp|var|etc)[^:*?"<>|\s]+)')
URL_PATTERN = re.compile(r'https?://[^\s<>"\')]+')
DATA_URI_PREFIX_PATTERN = re.compile(r'^(data:([^;,\s]+)(?:;[^,\s]+)*;base64,)')
ZIP_ENTRY_PATTERN = re.compile(r'\bfiles/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*')
ARTIFACT_PATTERN = re.compile(r'\b(?:artifact|tool_artifact):[A-Za-z0-9_.-]+')
WINDOWS_ABSOLUTE_PATH_PATTERN = re.compile(r"^[A-Za-z]:[\\/]")


@dataclass
class AttachmentInventoryRecord:
    conversation_id: str
    message_id: str
    tool_call_id: str
    source_ordinal: int
    locator_type: str
    original_locator: str
    mime_or_extension: str
    size_bytes: int | None
    content_hash: str | None
    status: str
    resolved_path: str | None = None
    canonical_locator: str = ""
    observed_locators: list[str] = field(default_factory=list)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _is_within_allowlist(path: Path, allowlist: Sequence[str | Path]) -> bool:
    if not allowlist:
        return False

    raw_target = str(path)
    if WINDOWS_ABSOLUTE_PATH_PATTERN.match(raw_target):
        canonical_target = _canonicalize_path(raw_target)
        for root in allowlist:
            raw_root = str(root)
            if not WINDOWS_ABSOLUTE_PATH_PATTERN.match(raw_root):
                continue
            canonical_root = _canonicalize_path(raw_root)
            if canonical_target == canonical_root or canonical_target.startswith(
                canonical_root + "/"
            ):
                return True
        return False

    resolved_target = path.resolve()
    for root in allowlist:
        try:
            resolved_target.relative_to(Path(root).resolve())
            return True
        except (ValueError, RuntimeError):
            continue
    return False


def _sanitize_locator(locator: str) -> str:
    if locator.startswith("data:"):
        match = DATA_URI_PREFIX_PATTERN.match(locator)
        mime = match.group(2) if match else "unknown"
        return f"data:{mime};base64,...<{len(locator)} chars>"
    return locator[:500]


def _canonicalize_path(raw_path: str) -> str:
    cleaned = raw_path.strip().replace("\\", "/").rstrip("/")
    if len(cleaned) >= 2 and cleaned[1] == ":":
        cleaned = cleaned.lower()
    cleaned = re.sub(r"/+", "/", cleaned)
    return cleaned


class AttachmentInventory:
    def __init__(self, allowlist_roots: Sequence[str | Path] | None = None) -> None:
        self.allowlist_roots = [Path(r) for r in (allowlist_roots or [])]
        self._zip_namelist_cache: dict[str, set[str]] = {}

    def _get_zip_names(self, archive_path: str | Path | None) -> set[str]:
        if not archive_path:
            return set()
        p = Path(archive_path)
        if not p.exists() or not zipfile.is_zipfile(p):
            return set()
        key = str(p.resolve())
        if key not in self._zip_namelist_cache:
            try:
                with zipfile.ZipFile(p, "r") as zf:
                    self._zip_namelist_cache[key] = set(zf.namelist())
            except Exception:
                self._zip_namelist_cache[key] = set()
        return self._zip_namelist_cache[key]

    def scan_conversation(self, conversation: NormalizedConversation) -> list[AttachmentInventoryRecord]:
        records_map: dict[tuple[str, str], AttachmentInventoryRecord] = {}

        def add_record(rec: AttachmentInventoryRecord) -> None:
            dedup_key = (rec.conversation_id, rec.canonical_locator)
            if dedup_key in records_map:
                existing = records_map[dedup_key]
                if rec.original_locator and rec.original_locator not in existing.observed_locators:
                    existing.observed_locators.append(rec.original_locator)
                if existing.status in {"missing", "unresolved"} and rec.status == "found":
                    existing.status = "found"
                    existing.size_bytes = rec.size_bytes
                    existing.content_hash = rec.content_hash
                    existing.resolved_path = rec.resolved_path
            else:
                if rec.original_locator and rec.original_locator not in rec.observed_locators:
                    rec.observed_locators.append(rec.original_locator)
                records_map[dedup_key] = rec

        # 1. 扫描已知 attachment refs
        for att in conversation.attachments:
            rec = self._evaluate_attachment_ref(
                conversation.ref.conversation_id,
                att,
                archive_path=conversation.archive_path,
            )
            add_record(rec)

        # 2. 扫描消息正文中的外部引用
        for message in conversation.messages:
            if message.is_injected:
                continue
            for match in PATH_PATTERN.finditer(message.text):
                raw_path = match.group(0).rstrip('.,;)"\'>!?)]')
                ext = Path(raw_path).suffix.lower()
                if ext in {".pdf", ".docx", ".xlsx", ".csv", ".png", ".jpg", ".jpeg", ".md", ".zip", ".tar", ".gz"}:
                    rec = self._evaluate_local_path(
                        conversation_id=conversation.ref.conversation_id,
                        message_id=message.message_id,
                        tool_call_id="",
                        ordinal=message.ordinal,
                        raw_path=raw_path,
                        archive_path=conversation.archive_path,
                    )
                    add_record(rec)

            for match in ZIP_ENTRY_PATTERN.finditer(message.text):
                raw_entry = match.group(0).rstrip('.,;)"\'>!?)]')
                rec = self._evaluate_local_path(
                    conversation_id=conversation.ref.conversation_id,
                    message_id=message.message_id,
                    tool_call_id="",
                    ordinal=message.ordinal,
                    raw_path=raw_entry,
                    archive_path=conversation.archive_path,
                )
                add_record(rec)

            for match in ARTIFACT_PATTERN.finditer(message.text):
                raw_art = match.group(0).rstrip('.,;)"\'>!?)]')
                rec = self._evaluate_local_path(
                    conversation_id=conversation.ref.conversation_id,
                    message_id=message.message_id,
                    tool_call_id="",
                    ordinal=message.ordinal,
                    raw_path=raw_art,
                    archive_path=conversation.archive_path,
                )
                add_record(rec)

            for match in URL_PATTERN.finditer(message.text):
                raw_url = match.group(0).rstrip('.,;)"\'>!?)]')
                ext = Path(raw_url.split("?")[0]).suffix.lower()
                if ext in {".pdf", ".docx", ".xlsx", ".csv", ".png", ".jpg", ".jpeg", ".md", ".zip", ".tar", ".gz"}:
                    rec = AttachmentInventoryRecord(
                        conversation_id=conversation.ref.conversation_id,
                        message_id=message.message_id,
                        tool_call_id="",
                        source_ordinal=message.ordinal,
                        locator_type="remote_url",
                        original_locator=raw_url[:500],
                        canonical_locator=raw_url.strip(),
                        mime_or_extension=ext,
                        size_bytes=None,
                        content_hash=None,
                        status="remote",
                        resolved_path=None,
                        observed_locators=[raw_url],
                    )
                    add_record(rec)

        # 3. 扫描工具活动中的路径引用与产物引用
        for tool in conversation.tools:
            if not tool.excerpt:
                continue
            for match in PATH_PATTERN.finditer(tool.excerpt):
                raw_path = match.group(0).rstrip('.,;)"\'>!?)]')
                ext = Path(raw_path).suffix.lower()
                if ext in {".pdf", ".docx", ".xlsx", ".csv", ".png", ".jpg", ".jpeg", ".md", ".zip", ".tar", ".gz"}:
                    rec = self._evaluate_local_path(
                        conversation_id=conversation.ref.conversation_id,
                        message_id="",
                        tool_call_id=tool.call_id,
                        ordinal=tool.ordinal,
                        raw_path=raw_path,
                        archive_path=conversation.archive_path,
                    )
                    add_record(rec)

            for match in ZIP_ENTRY_PATTERN.finditer(tool.excerpt):
                raw_entry = match.group(0).rstrip('.,;)"\'>!?)]')
                rec = self._evaluate_local_path(
                    conversation_id=conversation.ref.conversation_id,
                    message_id="",
                    tool_call_id=tool.call_id,
                    ordinal=tool.ordinal,
                    raw_path=raw_entry,
                    archive_path=conversation.archive_path,
                )
                add_record(rec)

            for match in ARTIFACT_PATTERN.finditer(tool.excerpt):
                raw_art = match.group(0).rstrip('.,;)"\'>!?)]')
                rec = self._evaluate_local_path(
                    conversation_id=conversation.ref.conversation_id,
                    message_id="",
                    tool_call_id=tool.call_id,
                    ordinal=tool.ordinal,
                    raw_path=raw_art,
                    archive_path=conversation.archive_path,
                )
                add_record(rec)

        return list(records_map.values())

    scan_one = scan_conversation

    @staticmethod
    def inventory_hash(records: Sequence[AttachmentInventoryRecord]) -> str:
        """Return a deterministic hash of the stable attachment inventory fields."""
        stable = [
            {
                "locator_type": r.locator_type,
                "canonical_locator": r.canonical_locator,
                "status": r.status,
                "content_hash": r.content_hash or "",
                "size_bytes": r.size_bytes,
                "message_id": r.message_id,
                "tool_call_id": r.tool_call_id,
                "source_ordinal": r.source_ordinal,
            }
            for r in records
        ]
        stable.sort(
            key=lambda x: (
                str(x["canonical_locator"]),
                str(x["locator_type"]),
                str(x["message_id"]),
                str(x["tool_call_id"]),
                int(x["source_ordinal"] or 0),
            )
        )
        payload = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest() if stable else ""

    def scan_many(self, conversations: Iterable[NormalizedConversation]) -> list[AttachmentInventoryRecord]:
        all_records: list[AttachmentInventoryRecord] = []
        for conv in conversations:
            all_records.extend(self.scan_conversation(conv))
        return all_records

    def _evaluate_attachment_ref(
        self,
        conversation_id: str,
        att: AttachmentRef,
        archive_path: str | Path | None = None,
    ) -> AttachmentInventoryRecord:
        locator = att.locator
        if locator.startswith("data:") or locator == "embedded-data":
            orig = _sanitize_locator(locator)
            can_loc = f"embedded:{att.content_hash or att.attachment_id or orig[:32]}"
            return AttachmentInventoryRecord(
                conversation_id=conversation_id,
                message_id=att.message_id,
                tool_call_id="",
                source_ordinal=att.ordinal,
                locator_type="embedded",
                original_locator=orig,
                canonical_locator=can_loc,
                mime_or_extension=att.content_type,
                size_bytes=att.size_bytes,
                content_hash=att.content_hash,
                status="embedded",
                resolved_path=None,
                observed_locators=[orig],
            )

        if URL_PATTERN.match(locator):
            ext = Path(locator.split("?")[0]).suffix
            orig = locator[:500]
            return AttachmentInventoryRecord(
                conversation_id=conversation_id,
                message_id=att.message_id,
                tool_call_id="",
                source_ordinal=att.ordinal,
                locator_type="remote_url",
                original_locator=orig,
                canonical_locator=orig.strip(),
                mime_or_extension=ext or att.content_type,
                size_bytes=att.size_bytes,
                content_hash=att.content_hash,
                status="remote",
                resolved_path=None,
                observed_locators=[orig],
            )

        # ZIP entry
        clean_path_str = locator.strip()
        norm_zip = clean_path_str.replace("\\", "/").lstrip("/")
        zip_names = self._get_zip_names(archive_path)
        if norm_zip in zip_names:
            return AttachmentInventoryRecord(
                conversation_id=conversation_id,
                message_id=att.message_id,
                tool_call_id="",
                source_ordinal=att.ordinal,
                locator_type="zip_entry",
                original_locator=clean_path_str[:500],
                canonical_locator=f"zip:{norm_zip}",
                mime_or_extension=Path(norm_zip).suffix.lower() or att.content_type,
                size_bytes=att.size_bytes,
                content_hash=att.content_hash,
                status="found",
                resolved_path=None,
                observed_locators=[clean_path_str],
            )

        # Tool artifact
        if clean_path_str.startswith(("artifact:", "tool_artifact:")):
            return AttachmentInventoryRecord(
                conversation_id=conversation_id,
                message_id=att.message_id,
                tool_call_id="",
                source_ordinal=att.ordinal,
                locator_type="tool_artifact",
                original_locator=clean_path_str[:500],
                canonical_locator=f"artifact:{clean_path_str}",
                mime_or_extension=att.content_type,
                size_bytes=att.size_bytes,
                content_hash=att.content_hash,
                status="referenced",
                resolved_path=None,
                observed_locators=[clean_path_str],
            )

        return self._evaluate_local_path(
            conversation_id=conversation_id,
            message_id=att.message_id,
            tool_call_id="",
            ordinal=att.ordinal,
            raw_path=locator,
            fallback_hash=att.content_hash,
            fallback_size=att.size_bytes,
            archive_path=archive_path,
        )

    def _evaluate_local_path(
        self,
        conversation_id: str,
        message_id: str,
        tool_call_id: str,
        ordinal: int,
        raw_path: str,
        fallback_hash: str | None = None,
        fallback_size: int | None = None,
        archive_path: str | Path | None = None,
    ) -> AttachmentInventoryRecord:
        clean_path_str = raw_path.strip().rstrip("/\\")
        canonical_loc = _canonicalize_path(clean_path_str)

        # 检查是否为 zip entry
        norm_zip = clean_path_str.replace("\\", "/").lstrip("/")
        zip_names = self._get_zip_names(archive_path)
        if norm_zip in zip_names:
            return AttachmentInventoryRecord(
                conversation_id=conversation_id,
                message_id=message_id,
                tool_call_id=tool_call_id,
                source_ordinal=ordinal,
                locator_type="zip_entry",
                original_locator=clean_path_str[:500],
                canonical_locator=f"zip:{norm_zip}",
                mime_or_extension=Path(norm_zip).suffix.lower(),
                size_bytes=fallback_size,
                content_hash=fallback_hash,
                status="found",
                resolved_path=None,
                observed_locators=[clean_path_str],
            )

        # 检查是否为 tool artifact
        if clean_path_str.startswith(("artifact:", "tool_artifact:")):
            return AttachmentInventoryRecord(
                conversation_id=conversation_id,
                message_id=message_id,
                tool_call_id=tool_call_id,
                source_ordinal=ordinal,
                locator_type="tool_artifact",
                original_locator=clean_path_str[:500],
                canonical_locator=f"artifact:{clean_path_str}",
                mime_or_extension="unknown",
                size_bytes=fallback_size,
                content_hash=fallback_hash,
                status="referenced",
                resolved_path=None,
                observed_locators=[clean_path_str],
            )

        try:
            target_path = Path(clean_path_str)
        except Exception:
            return AttachmentInventoryRecord(
                conversation_id=conversation_id,
                message_id=message_id,
                tool_call_id=tool_call_id,
                source_ordinal=ordinal,
                locator_type="unresolved",
                original_locator=clean_path_str[:500],
                canonical_locator=canonical_loc,
                mime_or_extension="unknown",
                size_bytes=fallback_size,
                content_hash=fallback_hash,
                status="unresolved",
                resolved_path=None,
                observed_locators=[clean_path_str],
            )

        ext = target_path.suffix.lower()
        if not _is_within_allowlist(target_path, self.allowlist_roots):
            return AttachmentInventoryRecord(
                conversation_id=conversation_id,
                message_id=message_id,
                tool_call_id=tool_call_id,
                source_ordinal=ordinal,
                locator_type="local_path",
                original_locator=clean_path_str[:500],
                canonical_locator=canonical_loc,
                mime_or_extension=ext,
                size_bytes=fallback_size,
                content_hash=fallback_hash,
                status="unresolved",
                resolved_path=None,
                observed_locators=[clean_path_str],
            )

        # A Windows absolute locator can be explicitly allowlisted while this
        # inventory is running on Linux (for example when NAS indexes an
        # archive exported on Windows).  Treat it as a known-but-unavailable
        # local path; never reinterpret ``D:\\...`` as a relative POSIX path
        # and attempt to read/hash a coincidentally named file.
        if WINDOWS_ABSOLUTE_PATH_PATTERN.match(clean_path_str) and os.name != "nt":
            return AttachmentInventoryRecord(
                conversation_id=conversation_id,
                message_id=message_id,
                tool_call_id=tool_call_id,
                source_ordinal=ordinal,
                locator_type="local_path",
                original_locator=clean_path_str[:500],
                canonical_locator=canonical_loc,
                mime_or_extension=ext,
                size_bytes=fallback_size,
                content_hash=fallback_hash,
                status="missing",
                resolved_path=None,
                observed_locators=[clean_path_str],
            )

        if target_path.exists() and target_path.is_file():
            try:
                stat = target_path.stat()
                digest = _sha256_file(target_path)
                return AttachmentInventoryRecord(
                    conversation_id=conversation_id,
                    message_id=message_id,
                    tool_call_id=tool_call_id,
                    source_ordinal=ordinal,
                    locator_type="local_path",
                    original_locator=clean_path_str[:500],
                    canonical_locator=canonical_loc,
                    mime_or_extension=ext,
                    size_bytes=stat.st_size,
                    content_hash=digest,
                    status="found",
                    resolved_path=str(target_path.resolve()),
                    observed_locators=[clean_path_str],
                )
            except (PermissionError, OSError):
                pass

        return AttachmentInventoryRecord(
            conversation_id=conversation_id,
            message_id=message_id,
            tool_call_id=tool_call_id,
            source_ordinal=ordinal,
            locator_type="local_path",
            original_locator=clean_path_str[:500],
            canonical_locator=canonical_loc,
            mime_or_extension=ext,
            size_bytes=fallback_size,
            content_hash=fallback_hash,
            status="missing",
            resolved_path=None,
            observed_locators=[clean_path_str],
        )

    @staticmethod
    def export_json(records: Sequence[AttachmentInventoryRecord], path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        data = [asdict(r) for r in records]
        target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return target

    @staticmethod
    def export_csv(records: Sequence[AttachmentInventoryRecord], path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        fieldnames = [
            "conversation_id",
            "message_id",
            "tool_call_id",
            "source_ordinal",
            "locator_type",
            "original_locator",
            "canonical_locator",
            "mime_or_extension",
            "size_bytes",
            "content_hash",
            "status",
            "resolved_path",
            "observed_locators",
        ]
        with target.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fieldnames)
            writer.writeheader()
            for r in records:
                d = asdict(r)
                d["observed_locators"] = "; ".join(r.observed_locators)
                writer.writerow(d)
        return target

    @staticmethod
    def missing_records(records: Sequence[AttachmentInventoryRecord]) -> list[AttachmentInventoryRecord]:
        return [r for r in records if r.status == "missing"]
