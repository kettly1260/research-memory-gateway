"""v0.2.4 W10: multi-source safety + duplicate candidate tests (taskbook 13.5 #21-25)."""

from __future__ import annotations

from pathlib import Path

from tests.synthetic_reader import SyntheticExportReader, SyntheticSession


def _make_reader(
    tmp_path: Path,
    name: str,
    *,
    source_system: str,
    namespace: str,
    conversation_id: str,
    messages: list[tuple[str, str]],
    title: str = "Synthetic Conversation",
) -> SyntheticExportReader:
    return SyntheticExportReader(
        tmp_path / name,
        source_system=source_system,
        namespace_label=namespace,
        sessions=[SyntheticSession(conversation_id, title=title, messages=messages)],
    )


def _ingest(tmp_path: Path, reader, name: str, *, manifest=None):
    from research_memory_gateway.conversations import (
        ConversationIngestionPipeline,
        ImportManifest,
        ObsidianConversationWriter,
    )

    output_root = tmp_path / name
    if manifest is None:
        manifest = ImportManifest(output_root / ".ai-memory" / "manifest.sqlite")
    writer = ObsidianConversationWriter(output_root)
    pipeline = ConversationIngestionPipeline(reader, writer, manifest)
    results = pipeline.run([s.conversation_id for s in reader.list_sessions()])
    return pipeline, manifest, results


# --- 13.5 #21: identical text on two systems -> candidate only, never merge ------

def test_cross_source_identical_text_becomes_candidate_only(tmp_path: Path) -> None:
    from research_memory_gateway.conversations import ImportManifest

    shared = ImportManifest(tmp_path / "shared" / ".ai-memory" / "manifest.sqlite")
    messages = [("user", "What is the melting point of X?"), ("assistant", "It is 1000 K.")]
    codex = _make_reader(
        tmp_path, "codex.zip", source_system="codex", namespace="ns", conversation_id="conv-x", messages=messages
    )
    chatgpt = _make_reader(
        tmp_path, "chatgpt.zip", source_system="chatgpt", namespace="ns", conversation_id="conv-x", messages=messages
    )

    pipeline_a, _, results_a = _ingest(tmp_path, codex, "staging_ms1", manifest=shared)
    assert results_a[0].status == "written"
    pipeline_b, _, results_b = _ingest(tmp_path, chatgpt, "staging_ms2", manifest=shared)
    assert results_b[0].status == "written"

    store = pipeline_b.identity_store
    records = store.list_source_records()
    assert len(records) == 2
    systems = {r.source_system for r in records}
    assert systems == {"codex", "chatgpt"}

    candidates = store.list_candidates(status="pending")
    types = {c.candidate_type for c in candidates}
    assert "cross_source_exact_transcript" in types
    # Neither record was merged: two canonical ids remain
    canonicals = {r.canonical_conversation_id for r in records}
    assert len(canonicals) == 2
    # and no auto merge ever occurred: no confirmed links
    assert store.list_candidates(status="confirmed_same") == []
    evidence = next(c for c in candidates if c.candidate_type == "cross_source_exact_transcript").evidence()
    assert evidence["exact_transcript"] is True
    assert evidence["same_source_system"] is False
    assert evidence["message_overlap"] == 1.0


# --- 13.5 #22: same raw provider id across systems -> no collision ---------------

def test_same_raw_id_across_systems_no_collision(tmp_path: Path) -> None:
    from research_memory_gateway.conversations import ImportManifest

    shared = ImportManifest(tmp_path / "shared" / ".ai-memory" / "manifest.sqlite")
    codex = _make_reader(
        tmp_path, "codex2.zip", source_system="codex", namespace="ns", conversation_id="abc", messages=[("user", "codex side")]
    )
    chatgpt = _make_reader(
        tmp_path, "chatgpt2.zip", source_system="chatgpt", namespace="ns", conversation_id="abc", messages=[("user", "chatgpt side")]
    )
    pipeline_a, _, results_a = _ingest(tmp_path, codex, "staging_id1", manifest=shared)
    pipeline_b, _, results_b = _ingest(tmp_path, chatgpt, "staging_id2", manifest=shared)
    assert results_a[0].status == "written"
    assert results_b[0].status == "written"

    store = pipeline_b.identity_store
    records = store.find_source_records_by_conversation("abc")
    assert len(records) == 2
    keys = {r.source_key for r in records}
    assert len(keys) == 2
    # distinct notes, neither overwrote the other
    paths = {r.output_path for r in records}
    assert len(paths) == 2
    bodies = {Path(p).read_text(encoding="utf-8") for p in paths}
    assert any("codex side" in b for b in bodies)
    assert any("chatgpt side" in b for b in bodies)
    # the legacy ledger was not stolen by the second system
    assert pipeline_a.manifest.get_record("abc") is not None
    assert pipeline_a.manifest.get_record("abc")["source_entry"].startswith("files/")


# --- 13.5 #23: cross-source exact transcript evidence is correct ------------------

def test_cross_source_exact_transcript_evidence_fields(tmp_path: Path) -> None:
    from research_memory_gateway.conversations import ImportManifest

    shared = ImportManifest(tmp_path / "shared" / ".ai-memory" / "manifest.sqlite")
    messages = [("user", "shared prompt"), ("assistant", "shared answer")]
    codex = _make_reader(tmp_path, "c.zip", source_system="codex", namespace="ns", conversation_id="id-c", messages=messages)
    claude = _make_reader(tmp_path, "cl.zip", source_system="claude", namespace="ns", conversation_id="id-cl", messages=messages)
    pipeline_a, _, _ = _ingest(tmp_path, codex, "staging_ev1", manifest=shared)
    pipeline_b, _, _ = _ingest(tmp_path, claude, "staging_ev2", manifest=shared)
    store = pipeline_b.identity_store
    candidate = next(
        c for c in store.list_candidates(status="pending") if c.candidate_type == "cross_source_exact_transcript"
    )
    evidence = candidate.evidence()
    assert evidence["left_message_count"] == 2
    assert evidence["right_message_count"] == 2
    assert evidence["ordered_prefix"] is True
    assert evidence["same_account_namespace"] is False
    assert 0.0 < candidate.score < 1.0


# --- 13.5 #24: near duplicate -> pending candidate only ---------------------------

def test_near_duplicate_is_pending_candidate_only(tmp_path: Path) -> None:
    from research_memory_gateway.conversations import ImportManifest

    shared = ImportManifest(tmp_path / "shared" / ".ai-memory" / "manifest.sqlite")
    shared_messages = [("user", f"shared-{i}") for i in range(19)]
    messages_a = [*shared_messages, ("user", "tail-a")]
    messages_b = [*shared_messages, ("user", "tail-b")]
    a = _make_reader(tmp_path, "a.zip", source_system="codex", namespace="ns", conversation_id="dup-a", messages=messages_a)
    b = _make_reader(tmp_path, "b.zip", source_system="codex", namespace="ns2", conversation_id="dup-b", messages=messages_b)
    pipeline_a, _, _ = _ingest(tmp_path, a, "staging_nd1", manifest=shared)
    pipeline_b, _, results_b = _ingest(tmp_path, b, "staging_nd2", manifest=shared)
    assert results_b[0].status == "written"
    store = pipeline_b.identity_store
    candidates = store.list_candidates(status="pending")
    types = {c.candidate_type for c in candidates}
    assert "high_message_overlap" in types
    # notes remain independent
    records = store.list_source_records()
    assert len(records) == 2
    assert len({r.canonical_conversation_id for r in records}) == 2


# --- 13.5 #25: rejected pair is not re-raised on later full imports ---------------

def test_rejected_pair_not_reprompted_after_reimport(tmp_path: Path) -> None:
    from research_memory_gateway.conversations import ImportManifest
    from research_memory_gateway.conversations.cli import main as cli_main

    shared = ImportManifest(tmp_path / "shared" / ".ai-memory" / "manifest.sqlite")
    messages = [("user", "identical"), ("assistant", "same")]
    codex = _make_reader(tmp_path, "r1.zip", source_system="codex", namespace="ns", conversation_id="rej", messages=messages)
    chatgpt = _make_reader(tmp_path, "r2.zip", source_system="chatgpt", namespace="ns", conversation_id="rej", messages=messages)
    pipeline_a, _, _ = _ingest(tmp_path, codex, "staging_rj1", manifest=shared)
    pipeline_b, _, _ = _ingest(tmp_path, chatgpt, "staging_rj2", manifest=shared)
    store = pipeline_b.identity_store
    candidate = next(
        c for c in store.list_candidates(status="pending") if c.candidate_type == "cross_source_exact_transcript"
    )

    rc = cli_main(["dedup-resolve", str(candidate.candidate_id), "--reject", "--dir", str(shared.path.parent.parent)])
    assert rc == 0

    # Re-import the same full export again: detector runs but must not
    # resurrect the rejected pair into pending.
    results = pipeline_b.run(["rej"])
    assert results[0].status == "skipped"
    pending = store.list_candidates(status="pending")
    assert pending == []
    still = store.get_candidate(candidate.candidate_id)
    assert still.status == "rejected"
    # and the decision is durably recorded
    decisions = store.list_candidates(status="rejected")
    assert len(decisions) == 1
