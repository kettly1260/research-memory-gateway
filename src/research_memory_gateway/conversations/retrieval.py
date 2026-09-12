from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Sequence

from research_memory_gateway.config import validate_safe_path
from research_memory_gateway.retrieval import EmbeddingClient
from .index import ConversationIndexDatabase, SearchResult


@dataclass
class HybridSearchResult:
    section_id: str
    conversation_id: str
    vault_path: str
    heading_path: list[str]
    title: str
    content: str
    lexical_score: float | None
    vector_score: float | None
    final_score: float
    score_type: str  # "lexical" | "vector" | "hybrid"
    date: str = ""
    projects: list[str] = field(default_factory=list)
    source_anchors: list[dict[str, str]] = field(default_factory=list)
    parent_thread_id: str = ""
    thread_source: str = ""
    source_system: str = "codex"
    source_originator: str = ""
    source_surface: str = ""
    source_version: str = ""
    model_provider: str = ""
    model_name: str = ""
    agent_path: str = ""


@dataclass
class RecallContextItem:
    conversation_id: str
    vault_path: str
    heading: str
    content: str
    score: float
    source_anchors: list[dict[str, str]] = field(default_factory=list)
    parent_thread_id: str = ""
    thread_source: str = ""
    source_system: str = "codex"
    source_originator: str = ""
    source_surface: str = ""
    source_version: str = ""
    model_provider: str = ""
    model_name: str = ""
    agent_path: str = ""


class ConversationRetrievalService:
    def __init__(
        self,
        index_db: ConversationIndexDatabase,
        allowed_roots: Sequence[str | Path],
        embedding_client: EmbeddingClient | None = None,
        min_vector_similarity: float = 0.25,
    ) -> None:
        self.index_db = index_db
        self.allowed_roots = [Path(r).resolve() for r in allowed_roots]
        self.embedding_client = embedding_client
        self.min_vector_similarity = min_vector_similarity

    def search(
        self,
        query: str,
        *,
        project: str | None = None,
        conversation_id: str | None = None,
        parent_thread_id: str | None = None,
        source_system: str | None = None,
        thread_source: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        lexical_candidates = self.index_db.search_fts(
            query,
            limit=limit * 2,
            conversation_id=conversation_id,
            parent_thread_id=parent_thread_id,
            source_system=source_system,
            thread_source=thread_source,
        )

        vector_candidates: list[SearchResult] = []
        fallback_to_lexical = False
        fallback_reason: str | None = None

        if self.embedding_client and self.embedding_client.enabled:
            try:
                q_vec = self.embedding_client.embed(query)
                if q_vec:
                    vector_candidates = self.index_db.search_vector(
                        q_vec,
                        limit=limit * 2,
                        conversation_id=conversation_id,
                        parent_thread_id=parent_thread_id,
                        source_system=source_system,
                        thread_source=thread_source,
                    )
                else:
                    fallback_to_lexical = True
                    fallback_reason = self.embedding_client.last_error or "embedding_failed"
            except Exception as e:
                fallback_to_lexical = True
                fallback_reason = str(e)
        else:
            fallback_to_lexical = True
            fallback_reason = "embedding_disabled"

        # 融合与去重
        all_ids: set[str] = set()
        lex_map = {c.section_id: c for c in lexical_candidates}
        vec_map = {c.section_id: c for c in vector_candidates}
        all_ids.update(lex_map.keys())
        all_ids.update(vec_map.keys())

        fused_results: list[HybridSearchResult] = []
        for sid in all_ids:
            lex_item = lex_map.get(sid)
            vec_item = vec_map.get(sid)
            base = lex_item or vec_item
            if not base:
                continue

            # metadata filter by project
            if project and project not in base.projects:
                continue

            lex_score = lex_item.score if lex_item else None
            vec_score = vec_item.score if vec_item else None

            if lex_score is not None and vec_score is not None:
                # 两者皆有
                lex_norm = 1.0 / (1.0 + lex_score)
                vec_norm = max(0.0, min(1.0, vec_score))
                final_score = 0.5 * lex_norm + 0.5 * vec_norm
                score_type = "hybrid"
            elif lex_score is not None:
                final_score = 1.0 / (1.0 + lex_score)
                score_type = "lexical"
            else:
                if vec_score is None or vec_score < self.min_vector_similarity:
                    continue
                final_score = max(0.0, min(1.0, vec_score or 0.0))
                score_type = "vector"

            fused_results.append(
                HybridSearchResult(
                    section_id=base.section_id,
                    conversation_id=base.conversation_id,
                    vault_path=base.vault_path,
                    heading_path=base.heading_path,
                    title=base.title,
                    content=base.content,
                    lexical_score=lex_score,
                    vector_score=vec_score,
                    final_score=final_score,
                    score_type=score_type,
                    date=base.date,
                    projects=base.projects,
                    source_anchors=base.source_anchors,
                    parent_thread_id=base.parent_thread_id,
                    thread_source=base.thread_source,
                    source_system=base.source_system,
                    source_originator=base.source_originator,
                    source_surface=base.source_surface,
                    source_version=base.source_version,
                    model_provider=base.model_provider,
                    model_name=base.model_name,
                    agent_path=base.agent_path,
                )
            )

        fused_results.sort(key=lambda x: x.final_score, reverse=True)
        top_results = fused_results[:limit]

        return {
            "query": query,
            "count": len(top_results),
            "fallback_to_lexical": fallback_to_lexical,
            "fallback_reason": fallback_reason,
            "results": [asdict(r) for r in top_results],
        }

    def read(self, file_path: str | Path, *, heading: str | None = None) -> dict[str, Any]:
        safe_path = validate_safe_path(file_path, self.allowed_roots)
        if not safe_path.exists() or not safe_path.is_file():
            raise FileNotFoundError(f"File not found: {safe_path}")

        text = safe_path.read_text(encoding="utf-8")
        from .vault_writer import parse_frontmatter
        fm, _ = parse_frontmatter(text)
        metadata = {
            "source": fm.get("source"),
            "source_system": fm.get("source_system") or (fm.get("source") if fm.get("source") == "codex" else "") or "codex",
            "source_originator": fm.get("source_originator") or "",
            "source_surface": fm.get("source_surface") or "",
            "source_version": fm.get("source_version") or "",
            "model_provider": fm.get("model_provider") or "",
            "model_name": fm.get("model_name") or "",
            "thread_source": fm.get("thread_source") or "",
            "parent_thread_id": fm.get("parent_thread_id") or "",
            "agent_path": fm.get("agent_path") or "",
            "conversation_id": fm.get("conversation_id") or "",
            "created": fm.get("created") or "",
            "updated": fm.get("updated") or "",
            "completion_status": fm.get("completion_status") or "",
        }
        if not heading:
            return {
                "path": str(safe_path),
                "heading": None,
                "content": text,
                "metadata": metadata,
            }

        # 若指定了 heading，提取对应部分
        lines = text.splitlines()
        collecting = False
        target_level = 0
        matched_lines: list[str] = []

        for line in lines:
            stripped = line.strip()
            if stripped.startswith("#"):
                hashes, htext = stripped.split(maxsplit=1) if " " in stripped else (stripped, "")
                level = len(hashes)
                if htext.strip().lower() == heading.strip().lower():
                    collecting = True
                    target_level = level
                    matched_lines.append(line)
                    continue
                elif collecting and level <= target_level:
                    break
            if collecting:
                matched_lines.append(line)

        return {
            "path": str(safe_path),
            "heading": heading,
            "content": "\n".join(matched_lines) if matched_lines else text,
            "metadata": metadata,
        }

    def recall(
        self,
        query: str,
        *,
        token_budget: int = 1500,
        project: str | None = None,
        conversation_id: str | None = None,
        parent_thread_id: str | None = None,
        source_system: str | None = None,
        thread_source: str | None = None,
    ) -> dict[str, Any]:
        search_res = self.search(
            query,
            project=project,
            conversation_id=conversation_id,
            parent_thread_id=parent_thread_id,
            source_system=source_system,
            thread_source=thread_source,
            limit=8,
        )
        items = search_res.get("results", [])

        # Conservative approximation: 2 chars/token.  The budget applies to
        # the final Agent context, including headings, source display, anchors
        # and separators—not merely to items[].content.
        char_budget = max(0, token_budget * 2)
        recalled_items: list[RecallContextItem] = []
        context = ""

        for item in items:
            separator = "" if not context else "\n\n---\n\n"
            remaining = char_budget - len(context) - len(separator)
            if remaining <= 0:
                break

            heading_display = " > ".join(item.get("heading_path") or []) or item.get("title") or "General"
            anchors = item.get("source_anchors", [])
            anchors_text = ""
            if anchors:
                anchors_text = " (" + ", ".join(f"ordinal {a.get('ordinal')}" for a in anchors) + ")"
            short_id = item["conversation_id"][:8]
            source_display = Path(item["vault_path"]).name or item["vault_path"]
            prefixes = [
                f"### [{short_id}] {heading_display}{anchors_text}\nSource: {source_display}\n\n",
                f"[{short_id}] {heading_display}\n",
                f"[{short_id}] ",
                "",
            ]
            raw_content = item["content"]
            min_content = 1 if raw_content else 0
            prefix = next(
                (candidate for candidate in prefixes if len(candidate) + min_content <= remaining),
                "",
            )
            content_budget = max(0, remaining - len(prefix))
            content = raw_content[:content_budget]
            block = prefix + content
            if not block:
                break
            context += separator + block
            recalled_items.append(
                RecallContextItem(
                    conversation_id=item["conversation_id"],
                    vault_path=item["vault_path"],
                    heading=heading_display,
                    content=content,
                    score=item["final_score"],
                    source_anchors=anchors,
                    parent_thread_id=item.get("parent_thread_id", ""),
                    thread_source=item.get("thread_source", ""),
                    source_system=item.get("source_system", "codex"),
                    source_originator=item.get("source_originator", ""),
                    source_surface=item.get("source_surface", ""),
                    source_version=item.get("source_version", ""),
                    model_provider=item.get("model_provider", ""),
                    model_name=item.get("model_name", ""),
                    agent_path=item.get("agent_path", ""),
                )
            )

        return {
            "query": query,
            "token_budget": token_budget,
            "fallback_to_lexical": search_res.get("fallback_to_lexical", False),
            "fallback_reason": search_res.get("fallback_reason"),
            "context_char_budget": char_budget,
            "context": context,
            "items": [asdict(r) for r in recalled_items],
        }
