"""v0.2.4 W10: manual dedup resolution tests (taskbook 13.6 #26-30)."""

from __future__ import annotations

from pathlib import Path

import pytest

from research_memory_gateway.conversations import ImportManifest
from research_memory_gateway.conversations.cli import main as cli_main
from tests.synthetic_reader import SyntheticExportReader, SyntheticSession

MESSAGES = [("user", "identical transcript"), ("assistant", "same answer")]


def _setup(tmp_path: Path):
    manifest = ImportManifest(tmp_path / "staging" / ".ai-memory" / "manifest.sqlite")
    codex = SyntheticExportReader(
        tmp_path / "codex.zip",
        source_system="codex",
        namespace_label="ns",
        sessions=[SyntheticSession("same-id", messages=MESSAGES)],
    )
    chatgpt = SyntheticExportReader(
        tmp_path / "chatgpt.zip",
        source_system="chatgpt",
        namespace_label="ns",
        sessions=[SyntheticSession("same-id", messages=MESSAGES)],
    )
    from research_memory_gateway.conversations import (
        ConversationIngestionPipeline,
        ObsidianConversationWriter,
    )

    output_root = tmp_path / "staging"
    writer = ObsidianConversationWriter(output_root)
    pipeline_a = ConversationIngestionPipeline(codex, writer, manifest)
    results_a = pipeline_a.run(["same-id"])
    assert results_a[0].status == "written"
    pipeline_b = ConversationIngestionPipeline(chatgpt, writer, manifest)
    results_b = pipeline_b.run(["same-id"])
    assert results_b[0].status == "written"
    store = pipeline_b.identity_store
    candidate = next(
        c for c in store.list_candidates(status="pending") if c.candidate_type == "cross_source_exact_transcript"
    )
    records = store.list_source_records()
    note_paths = [Path(r.output_path) for r in records]
    return store, candidate, records, note_paths, str(tmp_path / "staging")


# --- 13.6 #26: confirm same -> both source records under one active canonical ----

def test_confirm_same_links_records_to_single_canonical(tmp_path: Path) -> None:
    store, candidate, records, note_paths, root = _setup(tmp_path)
    rc = cli_main(["dedup-resolve", str(candidate.candidate_id), "--confirm-same", "--dir", root])
    assert rc == 0

    refreshed = [store.get_source_record(r.source_key) for r in records]
    canonicals = {r.canonical_conversation_id for r in refreshed}
    assert len(canonicals) == 1
    active = store.resolve_canonical(canonicals.pop())
    assert active is not None and active.status == "active"
    assert store.identity_stats()["confirmed_duplicate_links"] >= 1


# --- 13.6 #27: both source notes still exist after confirm ------------------------

def test_confirm_same_preserves_both_source_notes(tmp_path: Path) -> None:
    store, candidate, records, note_paths, root = _setup(tmp_path)
    assert all(p.exists() for p in note_paths)
    rc = cli_main(["dedup-resolve", str(candidate.candidate_id), "--confirm-same", "--dir", root])
    assert rc == 0
    assert all(p.exists() for p in note_paths)
    # and the source records themselves are untouched
    assert len(store.list_source_records()) == 2


# --- 13.6 #28: loser canonical resolves through the alias chain -------------------

def test_loser_canonical_resolves_via_alias(tmp_path: Path) -> None:
    store, candidate, records, note_paths, root = _setup(tmp_path)
    canonical_before = {r.source_key: r.canonical_conversation_id for r in records}
    rc = cli_main(
        [
            "dedup-resolve",
            str(candidate.candidate_id),
            "--confirm-same",
            "--dir",
            root,
            "--winner-source-key",
            sorted(canonical_before)[0],
        ]
    )
    assert rc == 0

    winner_key = sorted(canonical_before)[0]
    loser_key = sorted(canonical_before)[1]
    loser_canonical = canonical_before[loser_key]
    # The loser canonical id must still resolve to the active winner canonical.
    resolved = store.resolve_canonical(loser_canonical)
    assert resolved is not None
    winner_canonical = store.get_source_record(winner_key).canonical_conversation_id
    assert resolved.canonical_conversation_id == winner_canonical
    assert resolved.status == "active"
    # loser canonical row is marked merged, never deleted
    loser_row = store.get_canonical(loser_canonical)
    assert loser_row.status == "merged"
    assert loser_row.merged_into_id == winner_canonical


# --- 13.6 #29: reject leaves canonical assignments unchanged ----------------------

def test_reject_keeps_canonical_unchanged(tmp_path: Path) -> None:
    store, candidate, records, note_paths, root = _setup(tmp_path)
    canonical_before = {r.source_key: r.canonical_conversation_id for r in records}
    rc = cli_main(["dedup-resolve", str(candidate.candidate_id), "--reject", "--dir", root])
    assert rc == 0

    for record in records:
        current = store.get_source_record(record.source_key)
        assert current.canonical_conversation_id == canonical_before[record.source_key]
    assert store.list_candidates(status="pending") == []
    assert store.identity_stats()["rejected_duplicate_candidates"] >= 1


# --- 13.6 #30: resolve without an explicit decision flag refuses to run -----------

def test_resolve_without_explicit_decision_refuses(tmp_path: Path) -> None:
    store, candidate, records, note_paths, root = _setup(tmp_path)
    with pytest.raises(SystemExit) as excinfo:
        cli_main(["dedup-resolve", str(candidate.candidate_id), "--dir", root])
    assert excinfo.value.code == 2
    # nothing changed
    assert store.get_candidate(candidate.candidate_id).status == "pending"


def test_resolve_with_both_flags_refuses(tmp_path: Path) -> None:
    store, candidate, records, note_paths, root = _setup(tmp_path)
    with pytest.raises(SystemExit) as excinfo:
        cli_main(
            ["dedup-resolve", str(candidate.candidate_id), "--confirm-same", "--reject", "--dir", root]
        )
    assert excinfo.value.code == 2
    assert store.get_candidate(candidate.candidate_id).status == "pending"
