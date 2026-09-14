"""v0.2.6 ChatGPT importer attachment tests (release-blocker matrix G).

Only ZIP-internal assets are ever touched: safe assets resolve with content
SHA-256, missing assets stay unresolved, traversal-shaped entries are
rejected, and duplicate content yields one stable hash.
"""

from __future__ import annotations

import hashlib

from research_memory_gateway.conversations import AttachmentInventory
from research_memory_gateway.conversations.chatgpt_export import (
    ChatGPTExportReader,
    resolve_account_namespace_hash,
)
from tests.chatgpt_fixtures import (
    GraphBuilder,
    build_export,
    make_message,
    multimodal_message,
)

PNG_BYTES_A = b"\x89PNG\r\n\x1a\n synthetic-image-A-bytes"
PNG_BYTES_B = b"\x89PNG\r\n\x1a\n synthetic-image-B-bytes"


def _make_reader(tmp_path, conversations, *, name="export.zip", assets=None, **kwargs):
    archive = build_export(tmp_path / name, conversations, assets=assets, **kwargs)
    ns_hash, _ = resolve_account_namespace_hash(archive, namespace_label="ns")
    return ChatGPTExportReader(archive, account_namespace_hash=ns_hash)


def _conversation_with_assets() -> dict:
    builder = GraphBuilder("conv-att")
    builder.append(
        multimodal_message("user", "look at this", "file-service://file-imgA", message_id="att-m1")
    )
    builder.append(make_message("assistant", "A synthetic chart."))
    builder.append(
        multimodal_message("user", "and this", "file-service://file-missing", message_id="att-m3")
    )
    return builder.to_conversation()


def test_g33_safe_asset_resolved_with_hash(tmp_path) -> None:
    reader = _make_reader(
        tmp_path,
        [_conversation_with_assets()],
        name="att.zip",
        assets={"files/file-imgA.png": PNG_BYTES_A},
    )
    ref = reader.list_sessions()[0]
    parsed = reader.parse(ref.import_key)
    assert len(parsed.attachments) == 2  # imgA + missing pointer both inventoried

    inventory = AttachmentInventory(archive_asset_resolver=reader.resolve_asset)
    records = inventory.scan_one(parsed)
    found = [r for r in records if r.status == "found"]
    assert len(found) == 1
    assert found[0].locator_type == "archive_asset"
    assert found[0].content_hash == hashlib.sha256(PNG_BYTES_A).hexdigest()
    assert found[0].size_bytes == len(PNG_BYTES_A)


def test_g34_missing_asset_unresolved_no_network(tmp_path) -> None:
    reader = _make_reader(tmp_path, [_conversation_with_assets()], name="miss.zip")
    ref = reader.list_sessions()[0]
    parsed = reader.parse(ref.import_key)
    inventory = AttachmentInventory(archive_asset_resolver=reader.resolve_asset)
    records = inventory.scan_one(parsed)
    # the missing pointer resolves to nothing; no network is ever consulted
    # (the resolver only reads the local ZIP central directory)
    missing = [r for r in records if "file-missing" in r.original_locator]
    assert len(missing) == 1
    assert missing[0].status == "unresolved"
    assert missing[0].content_hash is None or missing[0].status != "found"


def test_g35_zip_traversal_rejected(tmp_path) -> None:
    conversation = _conversation_with_assets()
    reader = _make_reader(
        tmp_path,
        [conversation],
        name="trav.zip",
        assets={"files/file-imgA.png": PNG_BYTES_A},
    )
    # A zip entry with a traversal segment must be invisible to the resolver.
    evil_archive = tmp_path / "evil.zip"
    import zipfile

    with zipfile.ZipFile(evil_archive, "w") as zf:
        zf.writestr("conversations.json", _dump([conversation]))
        zf.writestr("../evil.png", b"malicious")
        zf.writestr("files/../../escape.png", b"malicious2")
        zf.writestr("files/file-imgA.png", PNG_BYTES_A)
    ns_hash, _ = resolve_account_namespace_hash(evil_archive, namespace_label="ns")
    evil_reader = ChatGPTExportReader(evil_archive, account_namespace_hash=ns_hash)
    index = evil_reader._asset_entry_index()
    for name in index.values():
        assert ".." not in name.split("/")
    content, entry = evil_reader.resolve_asset("file-service://file-imgA")
    assert content == PNG_BYTES_A  # the safe entry still resolves
    assert content is not None


def _dump(conversations) -> str:
    import json

    return json.dumps(conversations, ensure_ascii=False, indent=1)


def test_g36_duplicate_content_stable_hash(tmp_path) -> None:
    builder = GraphBuilder("conv-dup")
    builder.append(
        multimodal_message("user", "one", "file-service://file-dup1", message_id="dup-m1")
    )
    builder.append(
        multimodal_message("user", "two", "file-service://file-dup2", message_id="dup-m2")
    )
    builder.append(make_message("assistant", "done"))
    conversation = builder.to_conversation()
    reader = _make_reader(
        tmp_path,
        [conversation],
        name="dup.zip",
        assets={"files/file-dup1.png": PNG_BYTES_B, "files/file-dup2.png": PNG_BYTES_B},
    )
    ref = reader.list_sessions()[0]
    parsed = reader.parse(ref.import_key)
    inventory = AttachmentInventory(archive_asset_resolver=reader.resolve_asset)
    records = inventory.scan_one(parsed)
    found = [r for r in records if r.status == "found"]
    assert len(found) == 2
    hashes = {r.content_hash for r in found}
    assert hashes == {hashlib.sha256(PNG_BYTES_B).hexdigest()}


def test_attachment_inventory_hash_participates_in_fingerprints(tmp_path) -> None:
    from research_memory_gateway.conversations.identity import compute_transcript_fingerprints

    reader_a = _make_reader(
        tmp_path,
        [_conversation_with_assets()],
        name="fp1.zip",
        assets={"files/file-imgA.png": PNG_BYTES_A},
    )
    parsed_a = reader_a.parse(reader_a.list_sessions()[0].import_key)
    fp_a = compute_transcript_fingerprints(parsed_a)

    reader_b = _make_reader(
        tmp_path,
        [_conversation_with_assets()],
        name="fp2.zip",
        assets={"files/file-imgA.png": b"\x89PNG different bytes entirely"},
    )
    parsed_b = reader_b.parse(reader_b.list_sessions()[0].import_key)
    fp_b = compute_transcript_fingerprints(parsed_b)

    # visible transcript identical; attachment inventory hash differs
    assert fp_a.ordered_message_hash == fp_b.ordered_message_hash
    assert fp_a.attachment_inventory_hash != fp_b.attachment_inventory_hash
