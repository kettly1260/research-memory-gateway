from __future__ import annotations

import hashlib
import math
from pathlib import Path
from unittest.mock import MagicMock
import pytest
from starlette.testclient import TestClient

from research_memory_gateway.config import (
    AppConfig,
    ConversationArchiveConfig,
)
from research_memory_gateway.conversations.index import ConversationIndexDatabase
from research_memory_gateway.backends import SQLiteMemoryBackend
from research_memory_gateway.service import ResearchMemoryService
from research_memory_gateway.webui.app import build_webui_app
from test_webui import login_webui

NOTE_UV = """---
type: ai-conversation
source: codex
conversation_id: 019e3ad1-05d6-7382-972f-0d377e6092c6
created: '2026-05-18'
updated: '2026-05-18'
projects:
- research-memory-gateway
parent_thread_id: parent-thread-999
---

# UV Photocatalysis Session

## 背景

- 工作目录：`G:\\LLM\\memory`
- 任务代号：`P0-4`

## 用户目标

<!-- source ordinal=2 message_id=msg-user-1 turn_id=turn-1 -->
### 用户

请验证紫外波长 324 nm 与反应时长 60 min 对光催化的影响。

## 最终结果

<!-- source ordinal=5 message_id=msg-ast-1 turn_id=turn-1 -->
### 助手 / final_answer

在 G:\\LLM\\memory 目录下完成测试，光催化实验条件确认。
"""

NOTE_TOOL = """---
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


def make_mock_vector(text: str) -> list[float]:
    digest = hashlib.md5(text.encode("utf-8")).digest()
    vec = [(digest[i % len(digest)] - 128) / 128.0 for i in range(1024)]
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


def setup_conversation_env(tmp_path: Path, monkeypatch, *, archive_enabled: bool = True, embedding_enabled: bool = True):
    monkeypatch.setenv("WEBUI_SECRET_KEY", "dev-key")
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir(parents=True, exist_ok=True)
    note_uv = staging_dir / "note_uv.md"
    note_tool = staging_dir / "note_tool.md"
    note_uv.write_text(NOTE_UV, encoding="utf-8")
    note_tool.write_text(NOTE_TOOL, encoding="utf-8")

    index_path = tmp_path / "conversation_idx.sqlite"

    mock_emb = MagicMock()
    mock_emb.enabled = embedding_enabled
    mock_emb.model = "bge-m3" if embedding_enabled else None
    mock_emb.dimension = 1024 if embedding_enabled else None
    mock_emb.embed.side_effect = make_mock_vector if embedding_enabled else None

    idx = ConversationIndexDatabase(index_path, embedding_client=mock_emb if embedding_enabled else None)
    idx.index_file(note_uv)
    idx.index_file(note_tool)

    config = AppConfig()
    config.backend.sqlite_path = str(tmp_path / "memory.db")
    config.memory.require_user_confirmation = False
    config.webui.enabled = True
    config.webui.initial_password = "admin-pass"
    config.webui.auth_store_path = str(tmp_path / "auth.json")
    config.webui.web_config_path = str(tmp_path / "web_config.yaml")
    config.webui.secret_store_path = str(tmp_path / "secrets.json.enc")

    config.conversation_archive = ConversationArchiveConfig(
        enabled=archive_enabled,
        staging_dir=staging_dir.as_posix(),
        index_path=index_path.as_posix(),
    )

    backend = SQLiteMemoryBackend(config.backend.sqlite_path, config.retrieval)
    if embedding_enabled:
        backend.embedding_client = mock_emb
    service = ResearchMemoryService(config, backend)

    app = build_webui_app(config, service)
    client = TestClient(app)
    return client, app, staging_dir, note_uv, note_tool, mock_emb


def test_conversations_api_disabled(tmp_path: Path, monkeypatch) -> None:
    client, app, staging_dir, note_uv, _, _ = setup_conversation_env(tmp_path, monkeypatch, archive_enabled=False)
    login_webui(client)

    # Status returns 404 with disabled indicator and doesn't 500
    res_status = client.get("/admin/api/conversations/status")
    assert res_status.status_code == 404
    data_status = res_status.json()
    assert data_status["enabled"] is False
    assert data_status["error"] == "conversation_archive_disabled"

    # Search returns 404
    res_search = client.get("/admin/api/conversations/search?query=紫外")
    assert res_search.status_code == 404
    assert res_search.json()["error"] == "conversation_archive_disabled"

    # Recall returns 404
    res_recall = client.get("/admin/api/conversations/recall?query=紫外")
    assert res_recall.status_code == 404
    assert res_recall.json()["error"] == "conversation_archive_disabled"

    # Read returns 404
    res_read = client.get(f"/admin/api/conversations/read?file_path={note_uv.as_posix()}")
    assert res_read.status_code == 404
    assert res_read.json()["error"] == "conversation_archive_disabled"


def test_conversations_api_status_counts(tmp_path: Path, monkeypatch) -> None:
    client, _, _, _, _, _ = setup_conversation_env(tmp_path, monkeypatch, archive_enabled=True, embedding_enabled=True)
    login_webui(client)

    res = client.get("/admin/api/conversations/status")
    assert res.status_code == 200
    data = res.json()
    assert data["enabled"] is True
    assert data["documents"] == 2
    assert data["sections"] > 0
    assert data["embeddings"] > 0
    assert data["sections_with_embedding"] == data["sections"]
    assert data["sections_without_embedding"] == 0
    assert data["vector_coverage"] == 1.0
    assert data["embedding_model"] == "bge-m3"
    assert data["embedding_dimension"] == 1024
    assert data["embedding_version"] == "v1"


def test_conversations_api_status_embedding_disabled(tmp_path: Path, monkeypatch) -> None:
    client, _, _, _, _, _ = setup_conversation_env(tmp_path, monkeypatch, archive_enabled=True, embedding_enabled=False)
    login_webui(client)

    res = client.get("/admin/api/conversations/status")
    assert res.status_code == 200
    data = res.json()
    assert data["enabled"] is True
    assert data["documents"] == 2
    assert data["sections"] > 0
    assert data["vector_coverage"] == 0.0
    assert data["embedding_model"] is None
    assert data["embedding_version"] is None
    assert data["embedding_dimension"] is None


def test_conversations_api_search_and_filters(tmp_path: Path, monkeypatch) -> None:
    client, _, _, _, _, _ = setup_conversation_env(tmp_path, monkeypatch, archive_enabled=True, embedding_enabled=True)
    login_webui(client)

    # Missing query -> 400
    res_missing = client.get("/admin/api/conversations/search")
    assert res_missing.status_code == 400

    # Search returns expected conversation
    res = client.get("/admin/api/conversations/search?query=324+nm")
    assert res.status_code == 200
    data = res.json()
    assert data["count"] >= 1
    top = data["results"][0]
    assert top["conversation_id"] == "019e3ad1-05d6-7382-972f-0d377e6092c6"
    assert "324 nm" in top["content"]

    # Filter by conversation_id
    res_match_cid = client.get("/admin/api/conversations/search?query=324+nm&conversation_id=019e3ad1-05d6-7382-972f-0d377e6092c6")
    assert res_match_cid.status_code == 200
    assert res_match_cid.json()["count"] >= 1

    res_wrong_cid = client.get("/admin/api/conversations/search?query=324+nm&conversation_id=019eab7a-3a54-70b1-afd2-b89c0c98e8b2")
    assert res_wrong_cid.status_code == 200
    assert res_wrong_cid.json()["count"] == 0

    # Filter by parent_thread_id
    res_match_pid = client.get("/admin/api/conversations/search?query=324+nm&parent_thread_id=parent-thread-999")
    assert res_match_pid.status_code == 200
    assert res_match_pid.json()["count"] >= 1

    res_wrong_pid = client.get("/admin/api/conversations/search?query=324+nm&parent_thread_id=parent-thread-888")
    assert res_wrong_pid.status_code == 200
    assert res_wrong_pid.json()["count"] == 0


def test_conversations_api_recall_budget(tmp_path: Path, monkeypatch) -> None:
    client, _, _, _, _, _ = setup_conversation_env(tmp_path, monkeypatch, archive_enabled=True, embedding_enabled=True)
    login_webui(client)

    # Missing query -> 400
    res_missing = client.get("/admin/api/conversations/recall")
    assert res_missing.status_code == 400

    # Small token budget
    res_small = client.get("/admin/api/conversations/recall?query=紫外&token_budget=50")
    assert res_small.status_code == 200
    data_small = res_small.json()
    assert len(data_small["context"]) <= data_small["context_char_budget"]
    assert data_small["token_budget"] == 50

    # Normal token budget
    res_large = client.get("/admin/api/conversations/recall?query=紫外&token_budget=1500")
    assert res_large.status_code == 200
    data_large = res_large.json()
    assert len(data_large["context"]) <= data_large["context_char_budget"]
    assert len(data_large["items"]) >= 1


def test_conversations_api_read_path_guard(tmp_path: Path, monkeypatch) -> None:
    client, _, staging_dir, note_uv, _, _ = setup_conversation_env(tmp_path, monkeypatch, archive_enabled=True, embedding_enabled=True)
    login_webui(client)

    # Missing file_path -> 400
    res_missing = client.get("/admin/api/conversations/read")
    assert res_missing.status_code == 400

    # Allowed path PASS
    res_ok = client.get(f"/admin/api/conversations/read?file_path={note_uv.as_posix()}")
    assert res_ok.status_code == 200
    data_ok = res_ok.json()
    assert "UV Photocatalysis Session" in data_ok["content"]
    assert data_ok["heading"] is None

    # Read specific heading PASS
    res_heading = client.get(f"/admin/api/conversations/read?file_path={note_uv.as_posix()}&heading=最终结果")
    assert res_heading.status_code == 200
    data_heading = res_heading.json()
    assert "在 G:\\LLM\\memory 目录下完成测试" in data_heading["content"]

    # Non-existent file inside allowed staging -> 404
    non_existent = staging_dir / "not_found.md"
    res_404 = client.get(f"/admin/api/conversations/read?file_path={non_existent.as_posix()}")
    assert res_404.status_code == 404

    # Outside allowed root FAIL -> 403
    outside = tmp_path / "outside.md"
    outside.write_text("Secret outside vault", encoding="utf-8")
    res_outside = client.get(f"/admin/api/conversations/read?file_path={outside.as_posix()}")
    assert res_outside.status_code == 403


def test_conversations_api_unauthenticated_returns_401(tmp_path: Path, monkeypatch) -> None:
    client, _, _, note_uv, _, _ = setup_conversation_env(tmp_path, monkeypatch, archive_enabled=True, embedding_enabled=True)
    # Notice: do NOT call login_webui(client)

    assert client.get("/admin/api/conversations/status").status_code == 401
    assert client.get("/admin/api/conversations/search?query=test").status_code == 401
    assert client.get("/admin/api/conversations/recall?query=test").status_code == 401
    assert client.get(f"/admin/api/conversations/read?file_path={note_uv.as_posix()}").status_code == 401
