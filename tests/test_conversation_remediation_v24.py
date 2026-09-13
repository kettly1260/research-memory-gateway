"""v0.2.4 remediation regression tests.

Covers the audit release blockers:
  1. legacy migration fingerprint hydration (A-D scenarios)
  2. stale snapshot ledger records the *incoming* export fingerprints
  3. dedup confirm idempotency + self-merge refusal
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from research_memory_gateway.conversations import (
    CodexExportReader,
    ConversationIngestionPipeline,
    ImportManifest,
    ObsidianConversationWriter,
)
from research_memory_gateway.conversations.cli import main as cli_main
from research_memory_gateway.conversations.identity import (
    FINGERPRINT_VERSION_UNHYDRATED,
    compute_transcript_fingerprints,
)
from research_memory_gateway.conversations.identity_store import (
    ConversationIdentityStore,
    load_legacy_import_rows,
)
from tests.test_conversation_ingestion import make_export
from tests.test_conversation_ingestion_v24 import _tail_records


def _legacy_setup(tmp_path: Path, *, title: str = "Legacy Conversation", tail: bool = False):
    """Create a v0.2.3-style state: note on disk + legacy manifest row, no v2 tables."""
    (tmp_path / "exp").mkdir()
    archive, cid = make_export(tmp_path / "exp", title=title, extra_tail_records=_tail_records([("msg-user-7", "tail", 9)]) if tail else None)
    output_root = tmp_path / "staging"
    manifest = ImportManifest(output_root / ".ai-memory" / "manifest.sqlite")
    writer = ObsidianConversationWriter(output_root)
    reader = CodexExportReader(archive)
    ref = reader.get_session_ref(cid)
    conversation = reader.parse(cid)
    note = writer.write(conversation)
    manifest.record(
        ref,
        archive_path=str(archive),
        archive_sha256=reader.archive_sha256,
        output_path=str(note),
        status="written",
    )
    store = ConversationIdentityStore(manifest.path)
    report = store.migrate_legacy_imports(load_legacy_import_rows(manifest.path))
    assert report.source_records_created == 1
    pipeline = ConversationIngestionPipeline(reader, writer, manifest, identity_store=store)
    return pipeline, manifest, store, writer, cid, Path(note), archive


# --- Blocker 1A: migrate -> same full export hydrates without touching notes -----

def test_migration_hydration_on_same_entry(tmp_path: Path) -> None:
    pipeline, manifest, store, _, cid, note, archive = _legacy_setup(tmp_path)
    record = store.list_source_records()[0]
    assert record.fingerprint_version == FINGERPRINT_VERSION_UNHYDRATED
    assert record.message_count == 0
    assert store.message_fingerprint_sequence(record.source_key) == []
    note_hash_before = note.read_bytes()

    first = pipeline.run([cid])[0]
    assert first.status == "skipped"
    assert first.reason == "identity_hydrated"
    # Markdown untouched, path untouched
    assert note.read_bytes() == note_hash_before
    assert Path(first.output_path) == note.resolve()

    hydrated = store.get_source_record(record.source_key)
    assert hydrated is not None
    assert hydrated.fingerprint_version == 1
    assert hydrated.message_count > 0
    assert hydrated.normalized_transcript_sha256
    sequence = store.message_fingerprint_sequence(record.source_key)
    assert len(sequence) == hydrated.message_count > 0

    # the migration-era snapshot row was backfilled too
    snapshots = store.list_snapshots(record.source_key)
    matching = [s for s in snapshots if s.source_entry_sha256 == pipeline.reader.get_session_ref(cid).source_sha256]
    assert len(matching) == 1
    assert matching[0].fingerprint_version == 1
    assert matching[0].normalized_transcript_sha256 == hydrated.normalized_transcript_sha256
    assert matching[0].message_count == hydrated.message_count

    # second identical replay is a plain unchanged skip (not another hydrate)
    second = pipeline.run([cid])[0]
    assert second.status == "skipped"
    assert second.reason == "unchanged"
    assert note.read_bytes() == note_hash_before


# --- Blocker 1B: hydrated record + strict continuation -> source_continued -------

def test_hydrated_record_continuation_works(tmp_path: Path) -> None:
    pipeline, manifest, store, _, cid, note, archive = _legacy_setup(tmp_path)
    assert pipeline.run([cid])[0].reason == "identity_hydrated"

    # strictly longer export: same head, one appended user message
    (tmp_path / "exp2").mkdir()
    archive2, _ = make_export(
        tmp_path / "exp2", title="Legacy Conversation",
        extra_tail_records=_tail_records([("msg-user-2", "Follow-up after hydration", 9)]),
    )
    pipeline.reader = CodexExportReader(archive2)
    result = pipeline.run([cid])[0]
    assert result.status == "written"
    assert result.reason == "source_continued"
    assert Path(result.output_path) == note.resolve()
    assert "Follow-up after hydration" in note.read_text(encoding="utf-8")


# --- Blocker 1C: hydrated record + modified middle message -> divergence ---------

def test_hydrated_record_divergence_fails_closed(tmp_path: Path) -> None:
    pipeline, manifest, store, _, cid, note, archive = _legacy_setup(tmp_path)
    assert pipeline.run([cid])[0].reason == "identity_hydrated"
    before = note.read_bytes()

    # same message count, first user message body rewritten
    (tmp_path / "exp3").mkdir()
    archive3, _ = make_export(tmp_path / "exp3", title="Legacy Conversation", extra_user_text="REWRITTEN")
    pipeline.reader = CodexExportReader(archive3)
    result = pipeline.run([cid])[0]
    assert result.status == "conflict"
    assert result.reason == "source_diverged"
    assert note.read_bytes() == before  # original note never overwritten
    assert Path(result.output_path).name.endswith(".candidate.md")


# --- Blocker 1D + 2: hydrated record + shorter snapshot -> stale, ledger correct --

def test_hydrated_record_stale_snapshot_and_ledger(tmp_path: Path) -> None:
    pipeline, manifest, store, _, cid, note, archive = _legacy_setup(tmp_path, tail=True)
    assert pipeline.run([cid])[0].reason == "identity_hydrated"
    before = note.read_bytes()
    record = store.list_source_records()[0]
    latest_fp = compute_transcript_fingerprints(pipeline.reader.parse(cid))

    # shorter (older) snapshot of the same conversation
    (tmp_path / "exp4").mkdir()
    short_archive, _ = make_export(tmp_path / "exp4", title="Legacy Conversation")
    pipeline.reader = CodexExportReader(short_archive)
    stale_ref = pipeline.reader.get_session_ref(cid)
    stale_fp = compute_transcript_fingerprints(pipeline.reader.parse(cid))
    assert stale_fp.normalized_transcript_sha256 != latest_fp.normalized_transcript_sha256

    result = pipeline.run([cid])[0]
    assert result.status == "skipped"
    assert result.reason == "stale_snapshot"
    assert note.read_bytes() == before  # never truncated

    # Blocker 2: the stale snapshot row must carry the *incoming* stale
    # fingerprints paired with the stale entry hash -- not the latest ones.
    snapshots = store.list_snapshots(record.source_key)
    stale_rows = [s for s in snapshots if s.source_entry_sha256 == stale_ref.source_sha256]
    assert len(stale_rows) == 1
    stale_row = stale_rows[0]
    assert stale_row.normalized_transcript_sha256 == stale_fp.normalized_transcript_sha256
    assert stale_row.ordered_message_hash == stale_fp.ordered_message_hash
    assert stale_row.message_set_hash == stale_fp.message_set_hash
    assert stale_row.message_count == stale_fp.message_count
    assert stale_row.normalized_transcript_sha256 != record.normalized_transcript_sha256

    # the source record's current fingerprints did not regress
    current = store.get_source_record(record.source_key)
    assert current.normalized_transcript_sha256 == latest_fp.normalized_transcript_sha256
    assert current.last_source_entry_sha256 != stale_ref.source_sha256

    # unhydrated + different entry fail closed (never masquerades as continuation)
    fresh_store = ConversationIdentityStore(tmp_path / "fresh" / ".ai-memory" / "manifest.sqlite")
    fresh_store.migrate_legacy_imports(load_legacy_import_rows(manifest.path))
    from research_memory_gateway.conversations.decisions import decide_source_import

    decision = decide_source_import(
        store=fresh_store,
        ref=pipeline.reader.get_session_ref(cid),
        fingerprints=stale_fp,
        archive_sha256=pipeline.reader.archive_sha256,
        parser_version="codex-export-v1.3",
        schema_version="ai-conversation-v1",
        legacy_record=None,
    )
    assert decision.action == "conflict"
    assert decision.reason == "fingerprints_unhydrated"


# --- Blocker 3: confirm idempotency and self-merge refusal ------------------------

def _dedup_setup(tmp_path: Path):
    from tests.synthetic_reader import SyntheticExportReader, SyntheticSession

    messages = [("user", "same transcript"), ("assistant", "same answer")]
    manifest = ImportManifest(tmp_path / "staging" / ".ai-memory" / "manifest.sqlite")
    writer = ObsidianConversationWriter(tmp_path / "staging")
    codex = SyntheticExportReader(
        tmp_path / "c.zip", source_system="codex", namespace_label="ns",
        sessions=[SyntheticSession("shared-id", messages=messages)],
    )
    chatgpt = SyntheticExportReader(
        tmp_path / "g.zip", source_system="chatgpt", namespace_label="ns",
        sessions=[SyntheticSession("shared-id", messages=messages)],
    )
    pa = ConversationIngestionPipeline(codex, writer, manifest)
    ra = pa.run(["shared-id"])
    assert ra[0].status == "written"
    pb = ConversationIngestionPipeline(chatgpt, writer, manifest)
    rb = pb.run(["shared-id"])
    assert rb[0].status == "written"
    store = pb.identity_store
    candidate = next(
        c for c in store.list_candidates(status="pending")
        if c.candidate_type == "cross_source_exact_transcript"
    )
    notes = [Path(r.output_path) for r in store.list_source_records()]
    assert len(notes) == 2
    return store, candidate, notes, str(tmp_path / "staging")


def _self_merge_violations(store: ConversationIdentityStore) -> dict[str, int]:
    with sqlite3.connect(store.path) as conn:
        self_alias = conn.execute(
            "SELECT COUNT(*) FROM canonical_aliases WHERE alias_canonical_id = active_canonical_id"
        ).fetchone()[0]
        self_merged = conn.execute(
            "SELECT COUNT(*) FROM canonical_conversations WHERE merged_into_id != '' "
            "AND merged_into_id = canonical_conversation_id"
        ).fetchone()[0]
    return {"self_alias": int(self_alias), "self_merged": int(self_merged)}


def test_confirm_same_is_idempotent_and_self_merge_proof(tmp_path: Path) -> None:
    store, candidate, notes, root = _dedup_setup(tmp_path)

    rc1 = cli_main(["dedup-resolve", str(candidate.candidate_id), "--confirm-same", "--dir", root])
    assert rc1 == 0
    records = store.list_source_records()
    canonicals = {r.canonical_conversation_id for r in records}
    assert len(canonicals) == 1
    active_id = canonicals.pop()
    loser_old = [
        r for r in store.list_source_records()
    ]
    # capture the pre-merge canonical ids from both records' history via aliases
    with sqlite3.connect(store.path) as conn:
        alias_pairs = conn.execute(
            "SELECT alias_canonical_id, active_canonical_id FROM canonical_aliases"
        ).fetchall()
    assert len(alias_pairs) == 1
    loser_canonical = alias_pairs[0][0]

    # second confirm on the same candidate: safe no-op, no self-merge
    rc2 = cli_main(["dedup-resolve", str(candidate.candidate_id), "--confirm-same", "--dir", root])
    assert rc2 == 0

    active = store.get_canonical(active_id)
    assert active is not None and active.status == "active"
    assert not active.merged_into_id
    resolved = store.resolve_canonical(loser_canonical)
    assert resolved is not None
    assert resolved.canonical_conversation_id == active_id
    violations = _self_merge_violations(store)
    assert violations == {"self_alias": 0, "self_merged": 0}
    # notes untouched
    assert all(n.exists() for n in notes)
    assert len(store.list_source_records()) == 2
    # exactly one confirmed decision row for this pair
    with sqlite3.connect(store.path) as conn:
        decisions = conn.execute(
            "SELECT COUNT(*) FROM dedup_decisions WHERE left_source_key = ? AND right_source_key = ? "
            "AND decision = 'confirmed_same'",
            (candidate.left_source_key, candidate.right_source_key),
        ).fetchone()[0]
    assert decisions == 1

    # a third confirm through the store API directly is also a safe no-op
    result = store.confirm_candidate_link(candidate.candidate_id)
    assert result["outcome"] == "no_op"
    assert _self_merge_violations(store) == {"self_alias": 0, "self_merged": 0}
    assert store.resolve_canonical(loser_canonical).canonical_conversation_id == active_id


def test_rejected_candidate_refuses_confirmation(tmp_path: Path) -> None:
    store, candidate, notes, root = _dedup_setup(tmp_path)
    rc = cli_main(["dedup-resolve", str(candidate.candidate_id), "--reject", "--dir", root])
    assert rc == 0
    canonical_before = {r.source_key: r.canonical_conversation_id for r in store.list_source_records()}

    # a rejected candidate refuses confirmation (no silent reopen)
    rc_confirm = cli_main(["dedup-resolve", str(candidate.candidate_id), "--confirm-same", "--dir", root])
    assert rc_confirm == 1

    # nothing changed
    for record in store.list_source_records():
        assert record.canonical_conversation_id == canonical_before[record.source_key]
    assert store.get_candidate(candidate.candidate_id).status == "rejected"
    assert _self_merge_violations(store) == {"self_alias": 0, "self_merged": 0}


def test_link_sources_to_canonical_direct_self_merge_refusal(tmp_path: Path) -> None:
    store, candidate, notes, root = _dedup_setup(tmp_path)
    rc = cli_main(["dedup-resolve", str(candidate.candidate_id), "--confirm-same", "--dir", root])
    assert rc == 0
    records = store.list_source_records()
    assert len({r.canonical_conversation_id for r in records}) == 1
    winner_key = records[0].source_key

    # calling the link API directly after a completed merge is a no-op
    result_id = store.link_sources_to_canonical(
        records[0].source_key, records[1].source_key, winner_source_key=winner_key
    )
    assert result_id == records[0].canonical_conversation_id
    assert _self_merge_violations(store) == {"self_alias": 0, "self_merged": 0}
