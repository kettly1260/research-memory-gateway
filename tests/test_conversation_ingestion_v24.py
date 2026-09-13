"""v0.2.4 W10: full-export dedup, continuation/stale/divergence tests.

Covers taskbook 13.3 (#11-14) and 13.4 (#15-20).
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from research_memory_gateway.conversations import (
    CodexExportReader,
    ConversationIngestionPipeline,
    ImportManifest,
    ObsidianConversationWriter,
)
from tests.test_conversation_ingestion import make_export


def _pipeline(tmp_path: Path, archive_path: Path, name: str) -> tuple[ConversationIngestionPipeline, ImportManifest, ObsidianConversationWriter]:
    output_root = tmp_path / name
    manifest = ImportManifest(output_root / ".ai-memory" / "manifest.sqlite")
    writer = ObsidianConversationWriter(output_root)
    pipeline = ConversationIngestionPipeline(CodexExportReader(archive_path), writer, manifest)
    return pipeline, manifest, writer


def _tail_records(entries: list[tuple[str, str, int]]) -> list[dict]:
    """entries of (message_id, text, ordinal); role inferred from id prefix."""
    records = []
    for ordinal, (message_id, text, _) in enumerate(entries, start=9):
        role = "user" if message_id.startswith("msg-user") else "assistant"
        records.append(
            {
                "timestamp": f"2026-09-10T02:{ordinal:02d}:00Z",
                "ordinal": ordinal,
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "id": message_id,
                    "role": role,
                    "phase": "final_answer" if role == "assistant" else "",
                    "content": [{"type": "input_text" if role == "user" else "output_text", "text": text}],
                    "internal_chat_message_metadata_passthrough": {"turn_id": f"turn-{ordinal}"},
                },
            }
        )
    return records


# --- 13.3 #11: first full export -> new ------------------------------------------

def test_first_full_export_is_new(tmp_path: Path) -> None:
    archive, cid = make_export(tmp_path)
    pipeline, _, _ = _pipeline(tmp_path, archive, "staging_first")
    results = pipeline.run([cid])
    assert results[0].status == "written"
    assert results[0].reason == "new_source_record"


# --- 13.3 #12: identical full export again -> skip unchanged ----------------------

def test_identical_full_export_reimport_skips(tmp_path: Path) -> None:
    archive, cid = make_export(tmp_path)
    pipeline, _, _ = _pipeline(tmp_path, archive, "staging_repeat")
    assert pipeline.run([cid])[0].status == "written"
    second = pipeline.run([cid])
    assert second[0].status == "skipped"
    assert second[0].reason == "unchanged"


# --- 13.3 #13: same source inside a new archive -> skip + last_seen update --------

def test_same_source_in_new_archive_updates_last_seen(tmp_path: Path) -> None:
    first_dir, second_dir = tmp_path / "a1", tmp_path / "a2"
    first_dir.mkdir()
    second_dir.mkdir()
    archive1, cid = make_export(first_dir)
    # Same rollout entry, different manifest metadata -> different archive
    # bytes with an identical source entry hash.
    archive2, _ = make_export(second_dir, title="Same Session New Export")
    pipeline, _, _ = _pipeline(tmp_path, archive1, "staging_newarch")
    assert pipeline.run([cid])[0].status == "written"

    store = pipeline.identity_store
    record = store.get_source_record(_source_key_for(store, cid))
    first_archive_seen = record.last_seen_archive_sha256

    pipeline.reader = CodexExportReader(archive2)
    result = pipeline.run([cid])[0]
    assert result.status == "skipped"
    assert result.reason == "same_source_in_new_archive"

    refreshed = store.get_source_record(record.source_key)
    assert refreshed.last_seen_archive_sha256 == pipeline.reader.archive_sha256
    assert refreshed.last_seen_archive_sha256 != first_archive_seen


def _source_key_for(store, conversation_id: str) -> str:
    records = store.find_source_records_by_conversation(conversation_id)
    assert len(records) == 1
    return records[0].source_key


# --- 13.3 #14: packaging changed but transcript identical -> no rewrite -----------

def _repackage_with_new_entry(archive: Path, destination: Path) -> Path:
    """Rebuild the export with modified packaging metadata (timestamps only).

    Message texts/roles are untouched, so transcript fingerprints stay equal
    while the raw entry hash changes.
    """
    with zipfile.ZipFile(archive) as source:
        manifest = json.loads(source.read("manifest.json"))
        entry_name = manifest["sessions"][0]["fileEntry"]
        raw = source.read(entry_name).decode("utf-8")
    raw = raw.replace("2026-09-10T01:00:0", "2026-09-11T05:00:0")
    raw = raw.replace("2026-09-10T01:0", "2026-09-11T05:0")
    raw = raw.replace("2026-09-10T02:0", "2026-09-11T06:0")
    manifest["exportedAt"] = "2026-09-12T00:00:00Z"
    session = manifest["sessions"][0]
    session["sha256"] = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    session["sizeBytes"] = len(raw.encode("utf-8"))
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as target:
        target.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
        target.writestr(entry_name, raw)
    return destination


def test_packaging_change_same_transcript_does_not_rewrite(tmp_path: Path) -> None:
    (tmp_path / "p1").mkdir()
    archive1, cid = make_export(tmp_path / "p1")
    archive2 = _repackage_with_new_entry(archive1, tmp_path / "p2" / "export.zip")
    pipeline, manifest, _ = _pipeline(tmp_path, archive1, "staging_pack")
    assert pipeline.run([cid])[0].status == "written"
    note_path = Path(manifest.get_record(cid)["output_path"])
    before = note_path.read_text(encoding="utf-8")

    reader2 = CodexExportReader(archive2)
    ref = reader2.get_session_ref(cid)
    old_ref = pipeline.reader.get_session_ref(cid)
    assert ref.source_sha256 != old_ref.source_sha256  # entry hash changed

    pipeline.reader = reader2
    result = pipeline.run([cid])[0]
    assert result.status == "skipped"
    assert result.reason == "source_packaging_changed"
    assert note_path.read_text(encoding="utf-8") == before


# --- 13.4 #15: strict continuation updates the original path ----------------------

def test_strict_continuation_updates_original_path(tmp_path: Path) -> None:
    first_dir, second_dir = tmp_path / "c1", tmp_path / "c2"
    first_dir.mkdir()
    second_dir.mkdir()
    archive1, cid = make_export(first_dir)
    archive2, _ = make_export(
        second_dir,
        extra_tail_records=_tail_records(
            [("msg-user-2", "Follow-up question", 9), ("msg-assistant-2", "Final continuation", 10)]
        ),
    )
    pipeline, manifest, _ = _pipeline(tmp_path, archive1, "staging_cont")
    assert pipeline.run([cid])[0].status == "written"
    original_path = Path(manifest.get_record(cid)["output_path"])

    pipeline.reader = CodexExportReader(archive2)
    result = pipeline.run([cid])[0]
    assert result.status == "written"
    assert result.reason == "source_continued"
    assert Path(result.output_path) == original_path.resolve()
    assert "Follow-up question" in original_path.read_text(encoding="utf-8")
    # exactly one note: no orphan duplicate
    assert len(list(original_path.parent.parent.rglob(f"*{cid[:8]}*.md"))) == 1


# --- 13.4 #16: title change on continuation keeps original path -------------------

def test_continuation_title_change_keeps_path(tmp_path: Path) -> None:
    first_dir, second_dir = tmp_path / "t1", tmp_path / "t2"
    first_dir.mkdir()
    second_dir.mkdir()
    archive1, cid = make_export(first_dir, title="Original Title")
    archive2, _ = make_export(
        second_dir,
        title="Renamed After Continuation",
        extra_tail_records=_tail_records([("msg-user-9", "New tail", 9)]),
    )
    pipeline, manifest, _ = _pipeline(tmp_path, archive1, "staging_title")
    assert pipeline.run([cid])[0].status == "written"
    original_path = Path(manifest.get_record(cid)["output_path"])

    pipeline.reader = CodexExportReader(archive2)
    result = pipeline.run([cid])[0]
    assert result.status == "written"
    assert Path(result.output_path) == original_path.resolve()
    assert original_path.exists()
    assert len(list(Path(result.output_path).parent.parent.rglob("*.md"))) == 1


# --- 13.4 #17: manual region survives continuation --------------------------------

def test_manual_region_survives_continuation(tmp_path: Path) -> None:
    first_dir, second_dir = tmp_path / "m1", tmp_path / "m2"
    first_dir.mkdir()
    second_dir.mkdir()
    archive1, cid = make_export(first_dir)
    archive2, _ = make_export(
        second_dir,
        extra_tail_records=_tail_records([("msg-user-3", "Tail message", 9)]),
    )
    pipeline, manifest, _ = _pipeline(tmp_path, archive1, "staging_manual")
    assert pipeline.run([cid])[0].status == "written"
    note_path = Path(manifest.get_record(cid)["output_path"])
    note_path.write_text(note_path.read_text(encoding="utf-8") + "\nmanual continuity note\n", encoding="utf-8")

    pipeline.reader = CodexExportReader(archive2)
    result = pipeline.run([cid])[0]
    assert result.status == "written"
    assert "manual continuity note" in note_path.read_text(encoding="utf-8")


# --- 13.4 #18: older snapshot cannot truncate the newer note ----------------------

def test_stale_snapshot_cannot_truncate(tmp_path: Path) -> None:
    first_dir, second_dir = tmp_path / "s1", tmp_path / "s2"
    first_dir.mkdir()
    second_dir.mkdir()
    long_archive, cid = make_export(
        first_dir,
        extra_tail_records=_tail_records([("msg-user-l", "Long tail", 9), ("msg-assistant-l", "Long final", 10)]),
    )
    short_archive, _ = make_export(second_dir)  # older/shorter snapshot
    pipeline, manifest, _ = _pipeline(tmp_path, long_archive, "staging_stale")
    assert pipeline.run([cid])[0].status == "written"
    note_path = Path(manifest.get_record(cid)["output_path"])
    before = note_path.read_text(encoding="utf-8")
    assert "Long tail" in before

    pipeline.reader = CodexExportReader(short_archive)
    result = pipeline.run([cid])[0]
    assert result.status == "skipped"
    assert result.reason == "stale_snapshot"
    assert note_path.read_text(encoding="utf-8") == before
    # forced reimport must not bypass the stale guard either
    forced = pipeline.run([cid], resume=False)[0]
    assert forced.status == "skipped"
    assert forced.reason == "stale_snapshot"
    assert note_path.read_text(encoding="utf-8") == before


# --- 13.4 #19: modified middle message -> divergence, never auto overwrite --------

def test_modified_old_message_diverges_to_conflict(tmp_path: Path) -> None:
    first_dir, second_dir = tmp_path / "d1", tmp_path / "d2"
    first_dir.mkdir()
    second_dir.mkdir()
    archive1, cid = make_export(first_dir)
    # v2 keeps the same message count but rewrites the first user message body
    archive2, _ = make_export(second_dir, extra_user_text="REWRITTEN BODY")
    pipeline, manifest, _ = _pipeline(tmp_path, archive1, "staging_div")
    assert pipeline.run([cid])[0].status == "written"
    note_path = Path(manifest.get_record(cid)["output_path"])
    before = note_path.read_text(encoding="utf-8")

    pipeline.reader = CodexExportReader(archive2)
    result = pipeline.run([cid])[0]
    assert result.status == "conflict"
    assert result.reason == "source_diverged"
    # original note untouched, candidate copy written next to it
    assert note_path.read_text(encoding="utf-8") == before
    candidate = Path(result.output_path)
    assert candidate.name.endswith(".candidate.md")
    assert candidate.exists()


# --- 13.4 #20: branched divergence becomes two independent source records ---------

def test_branch_divergence_creates_independent_records(tmp_path: Path) -> None:
    from tests.synthetic_reader import SyntheticExportReader, SyntheticSession

    session_a = SyntheticSession(
        "conv-1", messages=[("user", "shared"), ("assistant", "branch-a")], branch_id="br-a"
    )
    session_b = SyntheticSession(
        "conv-1", messages=[("user", "shared"), ("assistant", "branch-b")], branch_id="br-b"
    )
    reader_a = SyntheticExportReader(
        tmp_path / "branch-a.zip", source_system="synth", namespace_label="ns", sessions=[session_a]
    )
    reader_b = SyntheticExportReader(
        tmp_path / "branch-b.zip", source_system="synth", namespace_label="ns", sessions=[session_b]
    )
    keys = {
        reader_a.get_session_ref("conv-1").source_identity().source_key,
        reader_b.get_session_ref("conv-1").source_identity().source_key,
    }
    assert len(keys) == 2

    output_root = tmp_path / "staging_branch"
    manifest = ImportManifest(output_root / ".ai-memory" / "manifest.sqlite")
    writer = ObsidianConversationWriter(output_root)
    pipeline = ConversationIngestionPipeline(reader_a, writer, manifest)
    results_a = pipeline.run(["conv-1"])
    assert results_a[0].status == "written"
    note_a = Path(results_a[0].output_path)

    # switch to branch b of the same provider conversation id
    pipeline.reader = reader_b
    results_b = pipeline.run(["conv-1"])
    assert results_b[0].status == "written"
    note_b = Path(results_b[0].output_path)

    assert note_a != note_b
    assert "branch-a" in note_a.read_text(encoding="utf-8")
    assert "branch-a" not in note_b.read_text(encoding="utf-8")
    # two independent source records, no overwrites
    records = pipeline.identity_store.list_source_records()
    assert len(records) == 2
    assert {r.source_branch_id for r in records} == {"br-a", "br-b"}
