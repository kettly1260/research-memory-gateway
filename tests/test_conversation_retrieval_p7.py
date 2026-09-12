from __future__ import annotations

import hashlib
import math
from pathlib import Path
from unittest.mock import MagicMock
import pytest

from research_memory_gateway.conversations.index import ConversationIndexDatabase
from research_memory_gateway.conversations.retrieval import ConversationRetrievalService

FIXTURE_NOTE_1 = """---
type: ai-conversation
source: codex
conversation_id: 019e3ad1-05d6-7382-972f-0d377e6092c6
created: '2026-05-18'
updated: '2026-05-18'
projects:
- research-memory-gateway
parent_thread_id: parent-thread-999
---

# Nihao Session

## 背景

- 工作目录：`G:\\LLM\\memory`
- 任务代号：`P0-4`

## 用户目标

<!-- source ordinal=2 message_id=msg-user-1 turn_id=turn-1 -->
### 用户

请验证紫外波长 324 nm 与反应时长 60 min。

## 最终结果

<!-- source ordinal=5 message_id=msg-ast-1 turn_id=turn-1 -->
### 助手 / final_answer

在 G:\\LLM\\memory 目录下完成测试，P0-4 目标达成。
"""

FIXTURE_NOTE_2 = """---
type: ai-conversation
source: codex
conversation_id: 019eab7a-3a54-70b1-afd2-b89c0c98e8b2
created: '2026-06-09'
updated: '2026-06-09'
projects:
- research-memory-gateway
parent_thread_id: parent-thread-888
---

# Tool Execution Session

## 工具活动

| ordinal | 方向 | 工具 | call_id | 字符数 | SHA-256 | 摘要 |
|---:|---|---|---|---:|---|---|
| 4 | output | exec_command | call-tool-1 | 55 | `sha` | ERROR_NO_SYSTEM_RESOURCES 35984 |

## 最终结果

命令由于系统资源不足失败，错误码为 35984。
"""


@pytest.fixture
def setup_retrieval_service(tmp_path: Path):
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    note1 = staging_dir / "note1.md"
    note2 = staging_dir / "note2.md"
    note1.write_text(FIXTURE_NOTE_1, encoding="utf-8")
    note2.write_text(FIXTURE_NOTE_2, encoding="utf-8")

    def make_mock_vector(text: str) -> list[float]:
        digest = hashlib.md5(text.encode("utf-8")).digest()
        vec = [(digest[i % len(digest)] - 128) / 128.0 for i in range(1024)]
        norm = math.sqrt(sum(x * x for x in vec)) or 1.0
        return [x / norm for x in vec]

    db_path = tmp_path / "index.sqlite"
    mock_emb = MagicMock()
    mock_emb.enabled = True
    mock_emb.model = "bge-m3"
    mock_emb.embed.side_effect = make_mock_vector

    idx = ConversationIndexDatabase(db_path, embedding_client=mock_emb)
    idx.index_file(note1)
    idx.index_file(note2)

    service = ConversationRetrievalService(
        index_db=idx,
        allowed_roots=[staging_dir],
        embedding_client=mock_emb,
    )
    return service, staging_dir, note1, note2, mock_emb


def test_fixed_query_set_retrieval(setup_retrieval_service) -> None:
    service, staging_dir, note1, note2, mock_emb = setup_retrieval_service

    # 1. P0-4
    res_p0 = service.search("P0-4")
    assert res_p0["count"] > 0
    assert any("P0-4" in r["content"] for r in res_p0["results"])

    # 2. 324 nm
    res_324 = service.search("324 nm")
    assert res_324["count"] > 0
    assert any("324 nm" in r["content"] for r in res_324["results"])

    # 3. 60 min
    res_60 = service.search("60 min")
    assert res_60["count"] > 0
    assert any("60 min" in r["content"] for r in res_60["results"])

    # 4. G:\LLM\memory
    res_path = service.search('"G:\\LLM\\memory"')
    assert res_path["count"] > 0
    assert any("G:\\LLM\\memory" in r["content"] for r in res_path["results"])

    # 5. ERROR_NO_SYSTEM_RESOURCES
    res_err = service.search("ERROR_NO_SYSTEM_RESOURCES")
    assert res_err["count"] > 0
    assert any("ERROR_NO_SYSTEM_RESOURCES" in r["content"] for r in res_err["results"])

    # 6. 35984
    res_code = service.search("35984")
    assert res_code["count"] > 0
    assert any("35984" in r["content"] for r in res_code["results"])

    # 7. conversation ID 精确过滤
    res_scoped = service.search("P0-4", conversation_id="019e3ad1-05d6-7382-972f-0d377e6092c6")
    assert res_scoped["count"] > 0
    res_scoped_empty = service.search("P0-4", conversation_id="019eab7a-3a54-70b1-afd2-b89c0c98e8b2")
    assert res_scoped_empty["count"] == 0

    # 8. 验证溯源信息
    item = res_324["results"][0]
    assert item["vault_path"] == str(note1.resolve())
    assert len(item["source_anchors"]) > 0
    assert item["source_anchors"][0]["ordinal"] == "2"


def test_retrieval_fallback_on_embedding_error(setup_retrieval_service) -> None:
    service, staging_dir, note1, note2, mock_emb = setup_retrieval_service
    mock_emb.embed.side_effect = RuntimeError("NAS service 503 error")

    # 当 embedding 服务异常时，自动优雅降级为纯 lexical 并返回诊断 metadata
    res = service.search("324 nm")
    assert res["count"] > 0
    assert res["fallback_to_lexical"] is True
    assert "503" in (res["fallback_reason"] or "")
    assert any("324 nm" in r["content"] for r in res["results"])


def test_path_traversal_protection(setup_retrieval_service, tmp_path: Path) -> None:
    service, staging_dir, note1, note2, mock_emb = setup_retrieval_service

    # 合法读取
    read_ok = service.read(note1, heading="背景")
    assert "G:\\LLM\\memory" in read_ok["content"]

    # 目录遍历攻击被严格拦截拒绝
    outside_file = tmp_path / "secret.env"
    outside_file.write_text("SECRET_KEY=123456", encoding="utf-8")

    with pytest.raises(PermissionError, match="outside allowed roots"):
        service.read(outside_file)

    with pytest.raises(PermissionError, match="outside allowed roots"):
        service.read(staging_dir / ".." / "secret.env")


def test_recall_and_token_budget(setup_retrieval_service) -> None:
    service, staging_dir, note1, note2, mock_emb = setup_retrieval_service

    for budget in (10, 100, 200):
        recall_res = service.recall("324 nm", token_budget=budget)
        assert "context" in recall_res
        assert len(recall_res["items"]) > 0
        assert len(recall_res["context"]) <= budget * 2
        assert recall_res["context_char_budget"] == budget * 2
    assert "Source:" in service.recall("324 nm", token_budget=200)["context"]


def test_recall_first_oversize_result_is_truncated(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    note = staging / "large.md"
    note.write_text(
        "---\nconversation_id: recall-large\n---\n\n# Large\n\n"
        "<!-- source ordinal=1 message_id=m1 turn_id=t1 -->\n"
        "## User\n\nFe3+ " + ("very-long-payload " * 500),
        encoding="utf-8",
    )
    idx = ConversationIndexDatabase(tmp_path / "large.sqlite")
    idx.index_file(note)
    service = ConversationRetrievalService(idx, allowed_roots=[staging])

    for budget in (10, 100):
        result = service.recall("Fe3+", token_budget=budget)
        assert result["items"]
        assert len(result["context"]) <= budget * 2


def test_recall_budget_includes_long_metadata_and_anchors(tmp_path: Path) -> None:
    staging = tmp_path / ("very-long-staging-name-" * 4)
    staging.mkdir()
    note = staging / ("very-long-source-file-name-" * 4 + ".md")
    long_heading = "Long Heading " * 40
    note.write_text(
        "---\nconversation_id: recall-metadata-budget\n---\n\n"
        f"# {long_heading}\n\n"
        "<!-- source ordinal=12345 message_id=message-with-a-long-id turn_id=turn-with-a-long-id -->\n"
        "## User\n\nFe3+ " + ("payload " * 200),
        encoding="utf-8",
    )
    idx = ConversationIndexDatabase(tmp_path / "metadata-budget.sqlite")
    idx.index_file(note)
    service = ConversationRetrievalService(idx, allowed_roots=[staging])

    for budget in (10, 100):
        result = service.recall("Fe3+", token_budget=budget)
        assert result["items"]
        assert result["items"][0]["vault_path"] == str(note.resolve())
        assert result["items"][0]["source_anchors"][0]["ordinal"] == "12345"
        assert len(result["context"]) <= budget * 2


def test_mcp_surface_with_conversation_tools(tmp_path: Path) -> None:
    import anyio
    from research_memory_gateway.config import AppConfig
    from research_memory_gateway.server import build_mcp

    config = AppConfig()
    config.backend.sqlite_path = str(tmp_path / "mem.db")
    config.conversation_archive.enabled = True
    config.conversation_archive.staging_dir = str(tmp_path / "staging")
    config.conversation_archive.index_path = str(tmp_path / "conv_idx.sqlite")

    mcp = build_mcp(config)
    tool_names = {t.name for t in anyio.run(mcp.list_tools)}
    assert "conversation_search" in tool_names
    assert "conversation_read" in tool_names
    assert "conversation_recall" in tool_names
    assert "recall_memory" in tool_names

    # 验证 MCP 工具包含 parent_thread_id 参数
    tools_list = anyio.run(mcp.list_tools)
    search_tool = next(t for t in tools_list if t.name == "conversation_search")
    assert "parent_thread_id" in search_tool.inputSchema.get("properties", {})

    recall_tool = next(t for t in tools_list if t.name == "conversation_recall")
    assert "parent_thread_id" in recall_tool.inputSchema.get("properties", {})


def test_parent_thread_retrieval_and_filtering(setup_retrieval_service) -> None:
    service, staging_dir, note1, note2, mock_emb = setup_retrieval_service

    # 1. 过滤 parent_thread_id=parent-thread-999
    res_parent_1 = service.search("324 nm", parent_thread_id="parent-thread-999")
    assert res_parent_1["count"] > 0
    assert res_parent_1["results"][0]["conversation_id"] == "019e3ad1-05d6-7382-972f-0d377e6092c6"
    assert res_parent_1["results"][0]["parent_thread_id"] == "parent-thread-999"

    # 2. 匹配错误 parent_thread_id 应返回 0 条
    res_wrong_parent = service.search("324 nm", parent_thread_id="parent-thread-888")
    assert res_wrong_parent["count"] == 0

    # 3. recall 支持 parent_thread_id 约束
    recall_res = service.recall("324 nm", parent_thread_id="parent-thread-999")
    assert "324 nm" in recall_res["context"]
    assert len(recall_res["items"]) > 0
    assert recall_res["items"][0]["parent_thread_id"] == "parent-thread-999"

    recall_empty = service.recall("324 nm", parent_thread_id="non-existent")
    assert len(recall_empty["items"]) == 0

