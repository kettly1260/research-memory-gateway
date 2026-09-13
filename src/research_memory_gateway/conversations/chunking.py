from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

SOURCE_ANCHOR_PATTERN = re.compile(
    r'<!--\s*source\s+ordinal=(\d+)(?:\s+message_id=([^\s]+))?(?:\s+turn_id=([^\s]+))?\s*-->'
)
HEADING_PATTERN = re.compile(r'^(#{1,6})\s+(.*)$')
MAX_CHUNK_CHARS = 1500


@dataclass
class ConversationChunk:
    chunk_id: str
    conversation_id: str
    vault_path: str
    heading_path: list[str]
    title: str
    content: str
    content_hash: str
    chunk_index: int
    source_anchors: list[dict[str, str]] = field(default_factory=list)
    projects: list[str] = field(default_factory=list)
    date: str = ""
    embedding_identity: str = ""


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def compute_embedding_identity(normalized_content: str, model: str, version: str) -> str:
    combined = f"{normalized_content.strip()}\0{model.strip()}\0{version.strip()}"
    return _sha256_text(combined)


class HeadingChunker:
    def __init__(self, max_chunk_chars: int = MAX_CHUNK_CHARS) -> None:
        self.max_chunk_chars = max_chunk_chars

    def chunk_file(
        self,
        path: str | Path,
        *,
        embedding_model: str = "",
        embedding_version: str = "v1",
        chunk_key: str = "",
    ) -> tuple[dict[str, Any], list[ConversationChunk]]:
        target = Path(path)
        text = target.read_text(encoding="utf-8")
        return self.chunk_text(
            text,
            vault_path=str(target.resolve()),
            embedding_model=embedding_model,
            embedding_version=embedding_version,
            chunk_key=chunk_key,
        )

    def chunk_text(
        self,
        text: str,
        *,
        vault_path: str = "",
        embedding_model: str = "",
        embedding_version: str = "v1",
        chunk_key: str = "",
    ) -> tuple[dict[str, Any], list[ConversationChunk]]:
        frontmatter: dict[str, Any] = {}
        body = text
        if text.startswith("---"):
            parts = text.split("---", 2)
            if len(parts) >= 3:
                try:
                    frontmatter = yaml.safe_load(parts[1]) or {}
                except Exception:
                    frontmatter = {}
                body = parts[2]

        conversation_id = str(frontmatter.get("conversation_id") or "")
        # v0.2.4: chunk ids may be scoped by source key so that two platforms
        # reusing the same bare provider id never overwrite each other's
        # sections.  Default stays the legacy conversation id.
        id_scope = chunk_key or conversation_id
        projects = list(frontmatter.get("projects") or [])
        date = str(frontmatter.get("created") or "")

        lines = body.splitlines()
        chunks: list[ConversationChunk] = []
        current_headings: list[str] = []
        current_lines: list[str] = []
        pending_anchors: list[dict[str, str]] = []
        doc_title = ""
        chunk_counter = 0

        def flush_section() -> None:
            nonlocal chunk_counter, current_lines, pending_anchors
            content = "\n".join(current_lines).strip()
            current_lines = []
            if not content:
                return

            # 提取包含的 source anchors（含 pending_anchors 及正文中出现的 anchors）
            anchors: list[dict[str, str]] = list(pending_anchors)
            for m in SOURCE_ANCHOR_PATTERN.finditer(content):
                anchors.append(
                    {
                        "ordinal": m.group(1),
                        "message_id": m.group(2) or "",
                        "turn_id": m.group(3) or "",
                    }
                )
            pending_anchors = []

            # 若超过 max_chunk_chars，按段落继续拆分，所有子块均完整继承所属 message 的 source anchors
            sub_texts = self._split_oversize(content)
            for sub in sub_texts:
                norm_sub = sub.strip()
                if not norm_sub:
                    continue
                content_hash = _sha256_text(norm_sub)
                emb_id = compute_embedding_identity(norm_sub, embedding_model, embedding_version)
                chunk = ConversationChunk(
                    chunk_id=f"{id_scope}#{chunk_counter}",
                    conversation_id=conversation_id,
                    vault_path=vault_path,
                    heading_path=list(current_headings),
                    title=doc_title or (current_headings[-1] if current_headings else "General"),
                    content=norm_sub,
                    content_hash=content_hash,
                    chunk_index=chunk_counter,
                    source_anchors=anchors,
                    projects=projects,
                    date=date,
                    embedding_identity=emb_id,
                )
                chunks.append(chunk)
                chunk_counter += 1

        for line in lines:
            # 过滤掉系统内部注释 marker，如 AUTOGEN_BEGIN
            if "BEGIN AUTOGENERATED CONTENT" in line or "END AUTOGENERATED CONTENT" in line:
                continue

            anchor_match = SOURCE_ANCHOR_PATTERN.search(line)
            if anchor_match:
                # 在记录新 anchor 之前，先将之前的正文内容（属于前一节/前一条消息）flush
                flush_section()
                pending_anchors.append(
                    {
                        "ordinal": anchor_match.group(1),
                        "message_id": anchor_match.group(2) or "",
                        "turn_id": anchor_match.group(3) or "",
                    }
                )
                continue

            heading_match = HEADING_PATTERN.match(line)
            if heading_match:
                flush_section()
                level = len(heading_match.group(1))
                heading_text = heading_match.group(2).strip()
                if level == 1 and not doc_title:
                    doc_title = heading_text

                # 调整 heading 层级 stack
                if level <= len(current_headings):
                    current_headings = current_headings[: level - 1]
                current_headings.append(heading_text)
            else:
                current_lines.append(line)

        flush_section()
        return frontmatter, chunks

    def _split_oversize(self, text: str) -> list[str]:
        if len(text) <= self.max_chunk_chars:
            return [text]
        cap = self.max_chunk_chars

        def hard_split(value: str) -> list[str]:
            return [value[i : i + cap] for i in range(0, len(value), cap)]

        def split_unit(value: str) -> list[str]:
            if len(value) <= cap:
                return [value]
            # Prefer line boundaries (tables/code/payloads), then sentence-ish
            # punctuation, and finally a strict character split.  The final
            # fallback makes max_chunk_chars a true upper bound for all input.
            lines = [l for l in value.splitlines(keepends=True) if l]
            if len(lines) > 1 and max(len(l) for l in lines) < len(value):
                return pack(lines)
            sentence_parts = [p for p in re.split(r"(?<=[。！？.!?;；])", value) if p]
            if len(sentence_parts) > 1 and max(len(p) for p in sentence_parts) < len(value):
                return pack(sentence_parts)
            return hard_split(value)

        def pack(units: list[str]) -> list[str]:
            packed: list[str] = []
            current = ""
            for unit in units:
                for piece in split_unit(unit) if len(unit) > cap else [unit]:
                    if not current:
                        current = piece
                    elif len(current) + len(piece) <= cap:
                        current += piece
                    else:
                        packed.append(current)
                        current = piece
                    if len(current) > cap:
                        overflow = hard_split(current)
                        packed.extend(overflow[:-1])
                        current = overflow[-1]
            if current:
                packed.append(current)
            return packed

        paragraphs = re.split(r"(\n\s*\n)", text)
        result = [part.strip() for part in pack(paragraphs) if part.strip()]
        assert all(len(part) <= cap for part in result)
        return result
