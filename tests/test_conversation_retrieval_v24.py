"""v0.2.4 W10: retrieval identity fields + canonical collapse tests (taskbook 13.7 #31-36)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from research_memory_gateway.conversations import (
    ConversationIndexDatabase,
    ConversationIngestionPipeline,
    ImportManifest,
    ObsidianConversationWriter,
    ConversationRetrievalService,
)
from research_memory_gateway.conversations.codex_export import CodexExportReader
from tests.test_conversation_ingestion import make_export


@pytest.fixture()
def indexed_archive(tmp_path: Path):
    (tmp_path / "exp").mkdir()
    archive, cid = make_export(tmp_path / "exp")
    output_root = tmp_path / "staging"
    manifest = ImportManifest(output_root / ".ai-memory" / "manifest.sqlite")
    writer = ObsidianConversationWriter(output_root)
    pipeline = ConversationIngestionPipeline(CodexExportReader(archive), writer, manifest)
    results = pipeline.run([cid])
    assert results[0].status == "written"
    store = pipeline.identity_store
    record = store.list_source_records()[0]
    idx = ConversationIndexDatabase(tmp_path / "index.sqlite")
    identity_lookup = lambda cid_: {  # noqa: E731
        "source_key": record.source_key,
        "canonical_conversation_id": record.canonical_conversation_id,
        "source_conversation_id": record.source_conversation_id,
    }
    chunks = idx.index_file(results[0].output_path, identity_lookup=identity_lookup)
    assert chunks > 0
    return {
        "pipeline": pipeline,
        "store": store,
        "record": record,
        "index": idx,
        "cid": cid,
        "note": results[0].output_path,
        "tmp": tmp_path,
    }


def _service(ctx, tmp_path: Path) -> ConversationRetrievalService:
    return ConversationRetrievalService(
        ctx["index"], allowed_roots=[tmp_path / "staging"]
    )


# --- 13.7 #31: search returns source_key + canonical_id ---------------------------

def test_search_returns_source_key_and_canonical_id(indexed_archive, tmp_path: Path) -> None:
    service = _service(indexed_archive, tmp_path)
    res = service.search("324 nm")
    assert res["count"] >= 1
    item = res["results"][0]
    assert item["source_key"] == indexed_archive["record"].source_key
    assert item["canonical_conversation_id"] == indexed_archive["record"].canonical_conversation_id
    assert item["source_conversation_id"] == indexed_archive["cid"]
    assert item["source_system"] == "codex"


def test_read_backfills_identity_for_legacy_frontmatter(tmp_path: Path) -> None:
    note = tmp_path / "staging" / "legacy.md"
    note.parent.mkdir(parents=True)
    note.write_text(
        """---
type: ai-conversation
source: codex
conversation_id: legacy-conversation-id
source_system: codex
thread_source: user
---

# Conversation

Legacy note content for identity fallback.
""",
        encoding="utf-8",
    )

    index = ConversationIndexDatabase(tmp_path / "index.sqlite")
    source_key = "srcv1_legacy_test"
    canonical_id = "11111111-1111-4111-8111-111111111111"
    indexed = index.index_file(
        note,
        identity_lookup=lambda _: {
            "source_key": source_key,
            "canonical_conversation_id": canonical_id,
            "source_conversation_id": "legacy-conversation-id",
            "source_thread_id": "legacy-thread",
            "source_branch_id": "legacy-branch",
        },
    )
    assert indexed > 0

    service = ConversationRetrievalService(index, allowed_roots=[note.parent])
    result = service.read(note)
    metadata = result["metadata"]
    assert metadata["conversation_id"] == "legacy-conversation-id"
    assert metadata["source_key"] == source_key
    assert metadata["canonical_conversation_id"] == canonical_id
    assert metadata["source_conversation_id"] == "legacy-conversation-id"
    assert metadata["source_thread_id"] == "legacy-thread"
    assert metadata["source_branch_id"] == "legacy-branch"
    assert metadata["source_system"] == "codex"


# --- 13.7 #36 (part 1): plain Codex-only search results remain usable -------------

def test_codex_only_search_ranking_unchanged(indexed_archive, tmp_path: Path) -> None:
    service = _service(indexed_archive, tmp_path)
    res = service.search("324 nm", limit=10)
    assert res["count"] >= 1
    assert all(r["source_system"] == "codex" for r in res["results"])
    # no ambiguity flag for a unique id
    assert not res.get("ambiguous_conversation_id")


# --- 13.7 #32: recall collapses confirmed canonical duplicates by default ---------

def _index_duplicate_source(
    ctx,
    tmp_path: Path,
    note_path: Path,
    source_key: str,
    canonical: str,
    *,
    keep_conversation_id: bool = False,
) -> None:
    """Index a second note as another source, optionally of the same canonical."""
    text = note_path.read_text(encoding="utf-8")
    twin = tmp_path / "staging" / "twin.md"
    replaced = text.replace(f"source_key: {source_key}", "source_key: twin-key")
    original_canonical = ctx["record"].canonical_conversation_id
    if canonical != original_canonical:
        replaced = replaced.replace(
            f"canonical_conversation_id: {original_canonical}",
            f"canonical_conversation_id: {canonical}",
        )
    if not keep_conversation_id:
        replaced = replaced.replace(
            "conversation_id: 11111111-2222-3333-4444-555555555555", "conversation_id: twin-id"
        )
    twin.write_text(replaced, encoding="utf-8")
    identity_lookup = lambda cid: {"source_key": "twin-key", "canonical_conversation_id": canonical}  # noqa: E731
    ctx["index"].index_file(twin, identity_lookup=identity_lookup)


def test_recall_collapses_confirmed_canonical_duplicates(indexed_archive, tmp_path: Path) -> None:
    ctx = indexed_archive
    record = ctx["record"]
    _index_duplicate_source(ctx, tmp_path, Path(ctx["note"]), record.source_key, record.canonical_conversation_id)

    service = _service(ctx, tmp_path)
    res = service.search("324 nm", limit=10)
    # two sources share one canonical id
    source_keys = {r["source_key"] for r in res["results"]}
    assert source_keys == {record.source_key, "twin-key"}

    recall = service.recall("324 nm", token_budget=100000)
    assert recall["collapse_canonical"] is True
    keys_in_context = {item["source_key"] for item in recall["items"]}
    assert len(keys_in_context) == 1
    collapsed = [i for i in recall["items"] if i.get("canonical_conversation_id")]
    assert collapsed and collapsed[0].get("canonical_collapsed") is True


# --- 13.7 #33: pending duplicates are not collapsed --------------------------------

def test_recall_does_not_collapse_pending_duplicates(indexed_archive, tmp_path: Path) -> None:
    ctx = indexed_archive
    record = ctx["record"]
    other_canonical = "00000000-0000-4000-8000-000000000000"
    _index_duplicate_source(ctx, tmp_path, Path(ctx["note"]), record.source_key, other_canonical)

    service = _service(ctx, tmp_path)
    recall = service.recall("324 nm", token_budget=100000)
    keys_in_context = {item["source_key"] for item in recall["items"]}
    # different canonical ids -> never grouped -> both sources present
    assert keys_in_context == {record.source_key, "twin-key"}


# --- 13.7 #34: rejected pairs are not collapsed ------------------------------------

def test_recall_does_not_collapse_rejected_pairs(indexed_archive, tmp_path: Path) -> None:
    ctx = indexed_archive
    record = ctx["record"]
    store = ctx["store"]
    # rejected decision between the two source keys
    candidate, _ = store.upsert_candidate(
        record.source_key,
        "twin-key",
        "cross_source_exact_transcript",
        score=0.8,
        evidence={"exact_transcript": True},
    )
    store.resolve_candidate(candidate.candidate_id, decision="rejected")
    other_canonical = "00000000-0000-4000-8000-000000000001"
    _index_duplicate_source(ctx, tmp_path, Path(ctx["note"]), record.source_key, other_canonical)

    service = _service(ctx, tmp_path)
    recall = service.recall("324 nm", token_budget=100000)
    keys_in_context = {item["source_key"] for item in recall["items"]}
    assert keys_in_context == {record.source_key, "twin-key"}


# --- 13.7 #35: collapse_canonical=false returns source-level results ---------------

def test_recall_collapse_opt_out_returns_all_sources(indexed_archive, tmp_path: Path) -> None:
    ctx = indexed_archive
    record = ctx["record"]
    _index_duplicate_source(ctx, tmp_path, Path(ctx["note"]), record.source_key, record.canonical_conversation_id)

    service = _service(ctx, tmp_path)
    recall = service.recall("324 nm", token_budget=100000, collapse_canonical=False)
    keys_in_context = {item["source_key"] for item in recall["items"]}
    assert keys_in_context == {record.source_key, "twin-key"}


# --- 13.7 (11.3): ambiguous bare conversation id refuses silent choice ------------

def test_ambiguous_bare_conversation_id_is_refused(indexed_archive, tmp_path: Path) -> None:
    ctx = indexed_archive
    record = ctx["record"]
    _index_duplicate_source(
        ctx,
        tmp_path,
        Path(ctx["note"]),
        record.source_key,
        record.canonical_conversation_id,
        keep_conversation_id=True,
    )

    service = _service(ctx, tmp_path)
    res = service.search("324 nm", conversation_id="11111111-2222-3333-4444-555555555555")
    assert res.get("ambiguous_conversation_id") is True
    assert len(res["ambiguous_matches"]) == 2
    assert res["results"] == []
    # disambiguated query works
    ok = service.search("324 nm", conversation_id="11111111-2222-3333-4444-555555555555", source_key=record.source_key)
    assert ok["count"] >= 1
