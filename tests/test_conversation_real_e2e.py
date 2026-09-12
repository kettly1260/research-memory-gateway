from __future__ import annotations

import json
from pathlib import Path
import pytest

from research_memory_gateway.conversations.attachments import AttachmentInventory
from research_memory_gateway.conversations.cli import main
from research_memory_gateway.conversations.codex_export import CodexExportReader
from research_memory_gateway.conversations.index import ConversationIndexDatabase
from research_memory_gateway.conversations.manifest import ImportManifest
from research_memory_gateway.conversations.pipeline import ConversationIngestionPipeline
from research_memory_gateway.conversations.retrieval import ConversationRetrievalService
from research_memory_gateway.conversations.vault_writer import ObsidianConversationWriter

REAL_ARCHIVE = Path(
    r"D:\Partition\F\Study\博士文件\Research-AI-Hub\_codex_workspace\tmp\chat-export\codex-sessions-20260912-012538.zip"
)

SESSIONS_5 = [
    "019e3ad1-05d6-7382-972f-0d377e6092c6",  # Nihao
    "019eab7a-3a54-70b1-afd2-b89c0c98e8b2",  # MCP list & config
    "01a08fdc-6da5-7f93-9119-ff79d9fea710",  # TOC QA (child of 01a08c1c)
    "01a01289-e1b4-7242-98d9-368a75a16194",  # Synthetic routes & TG (3 clipboard files)
    "019ea2ad-9a74-75c2-bf10-4246ca00ab25",  # PhD topic suggestions
]

pytestmark = pytest.mark.skipif(
    not REAL_ARCHIVE.exists(),
    reason="Real archive codex-sessions-20260912-012538.zip not accessible",
)


def test_real_5_sessions_isolated_e2e(tmp_path: Path) -> None:
    # 隔离 staging 目录
    staging_dir = (tmp_path / "real-e2e-staging").resolve()
    assert "remediation-validation" not in str(staging_dir)
    manifest_db = staging_dir / ".ai-memory" / "manifest.sqlite"
    index_db = staging_dir / "index.sqlite"
    assert not staging_dir.exists()
    assert not manifest_db.exists()
    assert not index_db.exists()
    staging_dir.mkdir(parents=True, exist_ok=False)

    # 1. 导入 5 场指定会话
    reader = CodexExportReader(REAL_ARCHIVE)
    writer = ObsidianConversationWriter(staging_dir)
    manifest = ImportManifest(manifest_db)
    pipeline = ConversationIngestionPipeline(reader, writer, manifest)

    results = pipeline.run(SESSIONS_5)
    assert len(results) == 5
    assert sum(r.status == "written" for r in results) == 5
    assert sum(r.status == "skipped" for r in results) == 0
    for r in results:
        assert Path(r.output_path).exists()

    second_results = pipeline.run(SESSIONS_5)
    assert sum(r.status == "written" for r in second_results) == 0
    assert sum(r.status == "skipped" for r in second_results) == 5

    # 2. 检查生成的 Markdown 文件
    md_files = sorted(list(staging_dir.rglob("*.md")))
    assert len(md_files) >= 5
    for f in md_files:
        text = f.read_text(encoding="utf-8")
        assert "data:image/" not in text
        assert ";base64," not in text
        assert "conversation_id:" in text

    # 3. 附件清点 Windows 路径去重测试（01a01289 恰好包含 3 个 clipboard 文件）
    conv_01a01289 = reader.parse("01a01289-e1b4-7242-98d9-368a75a16194")
    inventory = AttachmentInventory(allowlist_roots=["D:\\Partition\\TEMP"])
    records = inventory.scan_one(conv_01a01289)
    # 绝不能因 / 与 \\ 混用而产生多于 3 条记录
    clipboard_records = [r for r in records if "codex-clipboard-" in r.original_locator]
    assert len(clipboard_records) == 3
    for rec in clipboard_records:
        assert rec.locator_type == "local_path"
        assert rec.status == "missing"
        assert len(rec.observed_locators) >= 2  # 验证同时记录了 / 与 \\ 的写法

    # 4. 建立索引
    idx = ConversationIndexDatabase(index_db)
    indexed_chunks = 0
    for f in md_files:
        indexed_chunks += idx.index_file(f)
    assert indexed_chunks > 0

    service = ConversationRetrievalService(idx, allowed_roots=[staging_dir])

    # 5. 真实查询验证（完全基于真实 note 内容，无 synthetic fixture 假数据）

    # 查询 1: 父级线程 UUID 查询 -> 必须命中子会话 01a08fdc
    res_parent = service.search("01a08c1c-b49b-70a2-b1a4-cad68e4b1cd4")
    assert res_parent["count"] > 0
    assert any("01a08fdc" in r["conversation_id"] for r in res_parent["results"])
    hit_parent = next(r for r in res_parent["results"] if "01a08fdc" in r["conversation_id"])
    assert hit_parent["parent_thread_id"] == "01a08c1c-b49b-70a2-b1a4-cad68e4b1cd4"

    # 查询 2: 图片路径查询 -> 命中 01a08fdc
    res_img = service.search("TOC_Focused.png")
    assert res_img["count"] > 0
    assert any("01a08fdc" in r["conversation_id"] for r in res_img["results"])

    # 查询 3: 化学术语查询 -> 命中 01a08fdc
    res_chem = service.search("Fe3+")
    assert res_chem["count"] > 0
    assert any("01a08fdc" in r["conversation_id"] for r in res_chem["results"])

    # 查询 4: MCP 配置文件查询 -> 命中 019eab7a
    res_mcp = service.search("codex-mcp-config.dedup.toml")
    assert res_mcp["count"] > 0
    assert any("019eab7a" in r["conversation_id"] for r in res_mcp["results"])

    # 查询 5: 本地 MCP bridge 地址查询 -> 命中 019eab7a
    res_bridge = service.search('"http://127.0.0.1:23120/mcp"')
    assert res_bridge["count"] > 0
    assert any("019eab7a" in r["conversation_id"] for r in res_bridge["results"])

    # 查询 6: source anchor 闭环回溯检验
    user_chunks_01a08 = [
        r for r in res_img["results"]
        if "01a08fdc" in r["conversation_id"] and "用户" in " > ".join(r.get("heading_path") or [])
    ]
    assert len(user_chunks_01a08) > 0
    first_chunk = user_chunks_01a08[0]
    assert len(first_chunk["source_anchors"]) > 0
    assert first_chunk["source_anchors"][0]["ordinal"] != ""
    assert first_chunk["source_anchors"][0]["message_id"] != ""

    # 6. 验证 --changed-only 跳过
    skipped_count = 0
    for f in md_files:
        if not idx.check_changed(f):
            skipped_count += 1
    assert skipped_count == len(md_files)

    # 7. 验证人工区域编辑共存
    target_note = md_files[0]
    orig_text = target_note.read_text(encoding="utf-8")
    target_note.write_text(orig_text + "\n- 人工添加的研究待办 [[TODO]]\n", encoding="utf-8")
    # 再次运行 pipeline，不能报错且不能触发 conflict
    rerun_results = pipeline.run([SESSIONS_5[0]])
    assert rerun_results[0].status == "skipped"
    # 验证人工追加的内容仍然存在
    assert "人工添加的研究待办 [[TODO]]" in target_note.read_text(encoding="utf-8")

    # 8. 验证 Markdown 索引全量重建
    index_db.unlink()
    idx_rebuilt = ConversationIndexDatabase(index_db)
    rebuilt_count = idx_rebuilt.rebuild_from_markdown(md_files)
    assert rebuilt_count == indexed_chunks
