from __future__ import annotations

from pathlib import Path
import json
import pytest

from research_memory_gateway.conversations import (
    CodexExportReader,
    AttachmentInventory,
    AttachmentInventoryRecord,
)
from research_memory_gateway.conversations.models import (
    AttachmentRef,
    ExportSessionRef,
    NormalizedConversation,
    NormalizedMessage,
)

REAL_ARCHIVE = Path(r"D:\Partition\F\Study\博士文件\Research-AI-Hub\_codex_workspace\tmp\chat-export\codex-sessions-20260912-012538.zip")
SAMPLES = [
    "019e3ad1-05d6-7382-972f-0d377e6092c6",
    "019eab7a-3a54-70b1-afd2-b89c0c98e8b2",
    "01a08fdc-6da5-7f93-9119-ff79d9fea710",
    "01a01289-e1b4-7242-98d9-368a75a16194",
    "019ea2ad-9a74-75c2-bf10-4246ca00ab25",
]


def test_attachment_inventory_unit(tmp_path: Path) -> None:
    allowlist = [tmp_path / "allowed"]
    allowlist[0].mkdir()
    existing_file = allowlist[0] / "sample.pdf"
    existing_file.write_bytes(b"%PDF-1.4 test")

    missing_file = allowlist[0] / "non_existent.docx"
    outside_file = tmp_path / "outside.xlsx"
    outside_file.write_text("outside", encoding="utf-8")

    ref = ExportSessionRef(
        conversation_id="conv-1",
        title="Test",
        cwd=str(tmp_path),
        updated_at="2026-09-12T00:00:00Z",
        source_entry="files/0001/rollout.jsonl",
        source_size_bytes=100,
        source_sha256="abc",
    )
    conv = NormalizedConversation(
        ref=ref,
        archive_path="archive.zip",
        archive_sha256="hash",
        created_at="2026-09-12T00:00:00Z",
        session_meta={},
        messages=[
            NormalizedMessage(
                ordinal=1,
                timestamp="2026-09-12T00:00:00Z",
                role="user",
                text=f"Please check {existing_file} and {missing_file} and {outside_file}",
            )
        ],
        attachments=[
            AttachmentRef(
                attachment_id="att1",
                ordinal=2,
                message_id="msg1",
                content_type="image/png",
                locator="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==",
                content_hash="img-hash",
                size_bytes=85,
            )
        ],
    )

    inventory = AttachmentInventory(allowlist_roots=allowlist)
    records = inventory.scan_conversation(conv)

    # 1. 验证 embedded
    embedded = next(r for r in records if r.locator_type == "embedded")
    assert embedded.status == "embedded"
    assert "data:image/png;base64,...<" in embedded.original_locator
    assert "iVBORw0KGgo" not in embedded.original_locator
    assert embedded.content_hash == "img-hash"

    # 2. 验证 found
    found = next(r for r in records if r.original_locator == str(existing_file))
    assert found.status == "found"
    assert found.size_bytes == len(b"%PDF-1.4 test")
    assert found.resolved_path == str(existing_file.resolve())

    # 3. 验证 missing
    missing = next(r for r in records if r.original_locator == str(missing_file))
    assert missing.status == "missing"
    assert missing.resolved_path is None

    # 4. 验证 outside allowlist
    outside = next(r for r in records if r.original_locator == str(outside_file))
    assert outside.status == "unresolved"
    assert outside.resolved_path is None

    # 5. 验证 JSON 和 CSV 导出
    json_file = tmp_path / "report.json"
    csv_file = tmp_path / "report.csv"
    inventory.export_json(records, json_file)
    inventory.export_csv(records, csv_file)

    assert json_file.exists()
    assert csv_file.exists()
    assert "data:image/png" in json_file.read_text(encoding="utf-8")
    assert "missing" in csv_file.read_text(encoding="utf-8-sig")

    # 6. missing 过滤
    missing_list = inventory.missing_records(records)
    assert len(missing_list) == 1
    assert missing_list[0].original_locator == str(missing_file)


@pytest.mark.skipif(not REAL_ARCHIVE.exists(), reason="Real archive not present")
def test_real_5_samples_attachment_inventory(tmp_path: Path) -> None:
    reader = CodexExportReader(REAL_ARCHIVE)
    convs = [reader.parse(sid) for sid in SAMPLES]
    # 设定允许根目录为 G:\LLM 和 D:\Partition
    inventory = AttachmentInventory(allowlist_roots=["G:\\LLM", "D:\\Partition"])
    records = inventory.scan_many(convs)

    assert len(records) > 0
    # 会话 01a01289 具有 3 个图片附件，均为 embedded
    embedded_records = [r for r in records if r.conversation_id == "01a01289-e1b4-7242-98d9-368a75a16194" and r.locator_type == "embedded"]
    assert len(embedded_records) == 3
    for r in embedded_records:
        assert r.status == "embedded"
        assert r.original_locator == "embedded-data" or "base64" in r.original_locator
        assert len(r.original_locator) < 100

    # 验证 3 个临时 clipboard 路径不应因 / 与 \ 形式被重复计数
    clipboard_records = [
        r for r in records
        if r.conversation_id == "01a01289-e1b4-7242-98d9-368a75a16194" and "codex-clipboard-" in r.original_locator
    ]
    assert len(clipboard_records) == 3
    assert all(r.status == "missing" for r in clipboard_records)
    for r in clipboard_records:
        assert len(r.observed_locators) >= 2

    json_out = tmp_path / "real_inventory.json"
    csv_out = tmp_path / "real_inventory.csv"
    inventory.export_json(records, json_out)
    inventory.export_csv(records, csv_out)
    assert json_out.exists()
    assert csv_out.exists()


def test_attachment_locator_types_and_canonical_dedup(tmp_path: Path) -> None:
    import zipfile

    # 创建一个测试 zip 归档
    test_zip = tmp_path / "test_archive.zip"
    with zipfile.ZipFile(test_zip, "w") as zf:
        zf.writestr("files/0001/doc.pdf", b"pdf content")

    allow_dir = tmp_path / "allowed"
    allow_dir.mkdir()
    real_local = allow_dir / "real.png"
    real_local.write_bytes(b"png-data")

    ref = ExportSessionRef(
        conversation_id="conv-dedup-test",
        title="Dedup & Types Test",
        cwd=str(tmp_path),
        updated_at="2026-09-12T00:00:00Z",
        source_entry="files/0001/rollout.jsonl",
        source_size_bytes=100,
        source_sha256="abc",
    )

    conv = NormalizedConversation(
        ref=ref,
        archive_path=str(test_zip),
        archive_sha256="zip_hash",
        created_at="2026-09-12T00:00:00Z",
        session_meta={},
        messages=[
            NormalizedMessage(
                ordinal=1,
                timestamp="2026-09-12T00:00:00Z",
                role="user",
                text=(
                    "Check D:/Partition/TEMP/sample.png and also D:\\Partition\\TEMP\\sample.png! "
                    "Also check files/0001/doc.pdf from zip. "
                    "Also check https://example.com/spec.pdf. "
                    f"Also check {real_local} and artifact:report_v1. "
                ),
            )
        ],
        attachments=[
            AttachmentRef(
                attachment_id="att1",
                ordinal=2,
                message_id="msg1",
                content_type="image/png",
                locator="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==",
                content_hash="img-hash-1",
                size_bytes=85,
            ),
            AttachmentRef(
                attachment_id="att2",
                ordinal=3,
                message_id="msg1",
                content_type="application/octet-stream",
                locator="tool_artifact:chart_42",
                content_hash="art-hash",
                size_bytes=120,
            ),
        ],
    )

    inventory = AttachmentInventory(allowlist_roots=[allow_dir, "D:\\Partition"])
    records = inventory.scan_conversation(conv)

    # 1. 验证 Windows 路径斜杠去重：D:/... 与 D:\... 只能生成一条记录
    sample_records = [r for r in records if "sample.png" in r.original_locator]
    assert len(sample_records) == 1
    sample_rec = sample_records[0]
    assert sample_rec.status == "missing"
    assert sample_rec.canonical_locator == "d:/partition/temp/sample.png"
    assert len(sample_rec.observed_locators) == 2
    assert "D:/Partition/TEMP/sample.png" in sample_rec.observed_locators
    assert "D:\\Partition\\TEMP\\sample.png" in sample_rec.observed_locators

    # 2. 验证 ZIP entry
    zip_records = [r for r in records if r.locator_type == "zip_entry"]
    assert len(zip_records) == 1
    assert zip_records[0].status == "found"
    assert zip_records[0].canonical_locator == "zip:files/0001/doc.pdf"

    # 3. 验证 remote URL
    url_records = [r for r in records if r.locator_type == "remote_url"]
    assert len(url_records) == 1
    assert url_records[0].status == "remote"

    # 4. 验证 tool_artifact
    artifact_records = [r for r in records if r.locator_type == "tool_artifact"]
    assert len(artifact_records) >= 1
    assert all(r.status == "referenced" for r in artifact_records)

    # 5. 验证 embedded
    embedded_records = [r for r in records if r.locator_type == "embedded"]
    assert len(embedded_records) == 1
    assert embedded_records[0].status == "embedded"

    # 6. 验证 local_path found
    found_records = [r for r in records if r.status == "found" and r.locator_type == "local_path"]
    assert len(found_records) == 1
    assert found_records[0].size_bytes == len(b"png-data")


def test_attachment_inventory_hash_is_deterministic_and_sensitive() -> None:
    base = AttachmentInventoryRecord(
        conversation_id="conv-hash",
        message_id="msg-1",
        tool_call_id="",
        source_ordinal=1,
        locator_type="local_path",
        original_locator="D:/tmp/a.png",
        canonical_locator="d:/tmp/a.png",
        mime_or_extension=".png",
        size_bytes=12,
        content_hash="abc",
        status="found",
        observed_locators=["D:/tmp/a.png", "D:\\tmp\\a.png"],
    )
    other = AttachmentInventoryRecord(
        conversation_id="conv-hash",
        message_id="",
        tool_call_id="call-2",
        source_ordinal=2,
        locator_type="remote_url",
        original_locator="https://example.com/b.pdf",
        canonical_locator="https://example.com/b.pdf",
        mime_or_extension=".pdf",
        size_bytes=None,
        content_hash=None,
        status="remote",
        observed_locators=["https://example.com/b.pdf"],
    )

    hash_a = AttachmentInventory.inventory_hash([base, other])
    hash_b = AttachmentInventory.inventory_hash([other, base])
    assert hash_a == hash_b

    # observed_locators order is deliberately excluded from identity.
    base.observed_locators.reverse()
    assert AttachmentInventory.inventory_hash([base, other]) == hash_a

    # Stable lifecycle fields must change the identity.
    base.status = "missing"
    assert AttachmentInventory.inventory_hash([base, other]) != hash_a
