# Research Memory Gateway - MCP 2026-07-28 控制面与可恢复上传全链路验收报告 (v0.2.7)

**项目**: Research Memory Gateway (`research-memory-gateway`)  
**模块**: MCP Agent Surface, Generic Resumable Uploads (tus 1.0.0), Conversation Ingestion & Recall  
**版本**: v0.2.7  
**测试通过率**: 100% (325 / 325 全量自动化测试通过)  
**签署日期**: 2026-09-14  

---

## 1. 核心目标与原则达成情况

根据生产级技术标准，本阶段全面重构了 Agent 会话管理与历史导入体系，彻底消除了历史技术债（包括 CLI 依赖、自定义 WebSocket 上传、手动 staging 复制、暴露本地路径等问题），实现真正供 Agent 日常调用的规范化控制面：

| 验收原则 | 规范要求 | 实际实现与验证结果 | 状态 |
|---|---|---|---|
| **零手动干预** | 严禁要求用户 SSH 到 NAS、运行 CLI、手动拷贝到 staging 或传递本地绝对路径 | 全部流程（会话导入、状态查询、提交、检索、召回）均通过 MCP 2026-07-28 标准工具 + tus 1.0.0 HTTP 协议在客户端完成 | **PASS** |
| **标准协议分离** | 控制面走 MCP，二进制文件流走标准可恢复协议，严禁自定义 WS 切片或 Base64 参数 | 控制面为 MCP 工具集，二进制流走标准 `tus 1.0.0` 协议 (`/uploads/{upload_id}`)，分块上传、自动恢复 | **PASS** |
| **内容驱动探测** | 绝不依赖扩展名或文件名推断格式，以 JSON 内容结构自探测 | `detect_export_format` 检查 byte contents（检测 `"mapping"`、`"schema_version"`、`"history"`、`"chatgpt"` 特征），完美支持 ZIP 归档与单文件 JSON | **PASS** |
| **跨源跨包去重** | 同一会话的 selected backup 与 full export 必须合并至同一 Canonical Family | 从 ZIP 文件名或 metadata 中提取 `source_account_namespace_hash`，多备份、整包与单会话均解析至统一身份空间 | **PASS** |
| **严格幂等保障** | 二次上传或重复导入必须严格幂等，零笔记覆写、零 FTS 重复索引 | 经真实 11.4MB 会话测试：二次上传提交 `written=0, skipped=1`，已索引小节完全保持原样 | **PASS** |
| **Fail-Closed** | 上传未完成、大小不符、哈希不匹配时立刻失败，严禁落盘半成品或污染存储库 | `verify_and_finalize` 校验物理字节数与 SHA-256，不匹配即刻抛出异常并不进入 CAS blob 库 | **PASS** |

---

## 2. 系统架构与协议交互流程

```
+-----------------------------------------------------------------------------------------+
|                                      Agent 交互场景                                      |
+-----------------------------------------------------------------------------------------+
       |                                                            |
       | [场景 A: 历史 ChatGPT/Codex 导出导入]                       | [场景 B: 日常会话即时记录]
       v                                                            v
+------------------------------------+                      +------------------------------------+
| MCP: conversation_upload_create    |                      | MCP: conversation_ingest_turn      |
| -> 参数: filename, size, sha256    |                      |      / conversation_ingest_snapshot|
| <- 返回: upload_id, upload_url     |                      | -> 参数: session_id, role, content |
+------------------------------------+                      | <- 自动生成 Markdown 并即时 FTS 索引 |
       |                                                    +------------------------------------+
       v (标准 tus 1.0.0 客户端)                                     |
+------------------------------------+                             |
| PATCH /uploads/{upload_id}         |                             |
| -> 分块流式传输 (支持断点恢复)       |                             |
+------------------------------------+                             |
       |                                                           |
       v                                                           |
+------------------------------------+                             |
| MCP: conversation_upload_status    |                             |
| <- offset, is_complete, progress   |                             |
+------------------------------------+                             |
       |                                                           |
       v (上传完成后)                                                |
+------------------------------------+                             |
| MCP: conversation_upload_commit     |                             |
| 1. 物理大小与 SHA-256 强校验 (CAS)  |                             |
| 2. 格式自探测 (ChatGPT / Codex)     |                             |
| 3. IngestionPipeline 生成 Obsidian |                             |
| 4. 自动全量入库 SQLite FTS 索引     |                             |
+------------------------------------+                             |
       |                                                           |
       +-----------------------------+-----------------------------+
                                     v
+-----------------------------------------------------------------------------------------+
|                               Conversation Store & Retrieval                            |
|  - MCP conversation_search: 混合向量与 SQLite FTS5 BM25 检索                             |
|  - MCP conversation_read: 完整 Markdown 阅读与小节结构追踪                               |
|  - MCP conversation_recall: 基于 Token 预算自动组装注入上下文 (含 Source Anchors / 去重)   |
+-----------------------------------------------------------------------------------------+
```

---

## 3. MCP 工具集规范 (MCP 2026-07-28)

| 工具名称 | 类型 | 只读/修改 | 功能描述与输入参数 |
|---|---|---|---|
| `conversation_upload_create` | Upload Control | Non-destructive | 创建断点续传会话。参数：`filename`, `size_bytes`, `sha256`, `content_type`, `source_hint`, `expiry_hours`。返回 `upload_id` 和标准 tus `upload_url` (`/uploads/{upload_id}`)。 |
| `conversation_upload_status` | Upload Control | Read-Only | 查询上传进度。参数：`upload_id`。返回 `offset`, `total_bytes`, `is_complete`, `state`。 |
| `conversation_upload_abort` | Upload Control | Destructive | 取消并清理未完成上传。参数：`upload_id`。清理临时切片并释放磁盘空间。 |
| `conversation_upload_commit` | Ingestion Control | Non-destructive | 提交上传并导入会话库。参数：`upload_id`, `vault_confirmed`, `target_vault`, `format_hint`, `account_namespace`, `dry_run`。执行 SHA-256 校验 -> CAS 入库 -> 格式探测 -> 笔记生成 -> FTS 索引。 |
| `conversation_ingest_turn` | Live Ingestion | Non-destructive | 单轮实时录入。参数：`session_id`, `role`, `content`, `title`, `model`, `timestamp`, `metadata`。直接落盘 staging 并即时 FTS 索引。 |
| `conversation_ingest_snapshot` | Live Ingestion | Non-destructive | 多轮/完整会话快照直接录入。参数：`session_id`, `messages`, `title`, `model`, `metadata`, `overwrite`。结构化落盘并即时 FTS 索引。 |
| `conversation_search` | Retrieval | Read-Only | 关键词或语义搜索。参数：`query`, `project`, `conversation_id`, `parent_thread_id`, `source_system`, `limit`。 |
| `conversation_read` | Retrieval | Read-Only | 笔记正文阅读。参数：`file_path`, `heading`。安全校验路径是否在 staging/canonical 内。 |
| `conversation_recall` | Retrieval | Read-Only | 上下文召回。参数：`query`, `token_budget`, `collapse_canonical`。返回可以直接灌入 Agent System/Context 的 `context` 字符串和小节明细。 |

---

## 4. 真实 ChatGPT 导出包端到端验证测试报告

测试代码位置: `tests/test_conversation_real_upload_e2e.py`  
真实样本归档: `D:\Download\chatgpt_business_backup_selected_fe828cbb-7312-4979-8049-9a25c3362b7c_2026-09-14.zip`  
归档文件指纹:
- 物理大小: 1,062,241 字节 (~1.01 MB)
- 原始解压体积: 11,438,924 字节 (~11.4 MB)
- SHA-256 校验和: `9b2098a190b161e34e7a07b70b4adf251fd0d19361bca0c5e373732a1e8b6a7a`
- 结构特征: 包含单份 `审核实施报告_2aeea129deca.json`（共 4,269 个 mapping 节点）

### 4.1 阶段测试结果

1. **会话创建 (`conversation_upload_create`)**:
   - 客户端通过 MCP Streamable HTTP 协议调用 `conversation_upload_create`。
   - 成功生成 `upload_id`，并分配 `upload_url: http://127.0.0.1:{port}/uploads/{upload_id}`。
2. **tus 1.0.0 分块传输**:
   - 使用标准 `tusclient.uploader` 分块写入（256 KB/块），通过 HTTP `PATCH` 发送数据，服务器端准确维护 `Upload-Offset`。
3. **实时状态查询 (`conversation_upload_status`)**:
   - 查询返回 `received_bytes=1062241`, `total_bytes=1062241`, `is_complete=True`。
4. **提交与自动索引 (`conversation_upload_commit`)**:
   - 流式计算物理文件 SHA-256 与注册哈希一致；
   - 自动探测格式为 `chatgpt`；
   - 从 ZIP 名称正则探测出账号 GUID `fe828cbb-7312-4979-8049-9a25c3362b7c`，计算出 `source_account_namespace_hash: 1e077ae2ad2f...`；
   - 执行生成 Markdown 笔记（包含完整 YAML Frontmatter、多分支小节标题与 Source Anchors）；
   - 自动将所有生成笔记写入 SQLite FTS5 索引。返回 `status: committed`, `written: 1`, `failed: 0`。
5. **检索测试 (`conversation_search`)**:
   - 检索关键词: `"CONVERSATION_MEMORY_IMPLEMENTATION_TASKBOOK"`。
   - 命中真实内容，返回小节路径 `审核实施报告 > ChatGPT 审核实施报告 (2026-08-01 10:14:48 UTC)`，正确附带 `source_system: chatgpt` 与原始时间戳。
6. **阅读测试 (`conversation_read`)**:
   - 安全读取笔记指定标题小节，正文包含详细架构任务书与代码片段。
7. **召回测试 (`conversation_recall`)**:
   - 在 1000 Token 预算下，精准返回压缩格式上下文（`context` 长度 > 200 字符），附带 `source_anchors` 来源溯源。
8. **幂等性验证 (Idempotent Re-upload)**:
   - 再次上传完全相同的 1MB ZIP 包并调用 commit；
   - 返回 `counts: {"written": 0, "skipped": 1, "failed": 0}`；
   - 零笔记覆写，零重复索引，验证通过！

### 4.2 传输中断与恢复验证 (`test_real_interruption_and_resumption_e2e`)

1. 上传第一块数据（200,000 字节）后主动中断；
2. 查询 status: `offset=200000`, `is_complete=False`；
3. 此时尝试提前调用 `conversation_upload_commit`，服务端严格 Fail-Closed 抛出 `Cannot commit upload: upload is incomplete (200000/1062241 bytes received)`；
4. 客户端从 200,000 偏移量继续上传剩余 862,241 字节；
5. 服务端无缝拼接，最终状态 `offset=1062241`, `is_complete=True`。验证通过！

---

## 5. MCP 官方 Python SDK v2 迁移要点与向后兼容

本版本已平滑升级至官方 `mcp>=2.2.0`：

1. **核心服务端迁移**:
   - 由旧版非官方 `FastMCP` 重构为官方推荐的 `MCPServer`；
   - 采用标准装饰器 `@mcp.tool(annotations=ToolAnnotations(...))` 注册工具，规范标注 `readOnlyHint` 与 `destructiveHint`；
   - 标准化 Streamable HTTP 协议挂载（`/mcp`）与 SSE 传输通道（`/sse`）。
2. **协议字段命名平滑兼容**:
   - 官方 MCP SDK v2 采用严格 snake_case 命名（如 `CallToolResult.is_error`, `ToolAnnotations.read_only_hint`）；
   - 在 `research_memory_gateway` 模块中通过 monkey patch 与 attribute fallback 为 `mcp_types.ToolAnnotations` 与 `CallToolResult` 提供了对旧版 camelCase 字段（`readOnlyHint`, `isError`）的双向别名支持，确保第三方客户端或旧版调用方零阻碍。
3. **tus 协议挂载**:
   - 基于 `asgi-tus` 标准中间件实现，自动挂载在 `/uploads` 端点；
   - 配合 `BearerAuthMiddleware` 智能放行 CORS `OPTIONS` 预检与认证拦截。

---

## 6. 自动化测试回归清单

全量测试套件执行通过（共 325 项用例）：

```
tests/test_agent_surface.py .........................                    [ 7%]
tests/test_audit_manifest.py ...........                                 [ 11%]
tests/test_auxiliary.py ............                                     [ 15%]
tests/test_chatgpt_export.py ...................                         [ 21%]
tests/test_cli.py .................                                      [ 26%]
tests/test_conversation_import_pipeline.py ....................          [ 32%]
tests/test_conversation_real_upload_e2e.py ..                            [ 33%]
tests/test_conversation_retrieval_p7.py .......................          [ 40%]
tests/test_lifecycle.py .......................                          [ 47%]
tests/test_mcp_http_integration.py ...                                   [ 48%]
tests/test_models.py ...................................                 [ 59%]
tests/test_multi_source_dedup.py ....................                     [ 65%]
tests/test_obsidian_conversation_writer.py ................              [ 70%]
tests/test_readers.py .......................                            [ 77%]
tests/test_server.py .................                                   [ 82%]
tests/test_service.py ...............                                    [ 87%]
tests/test_upload_tools.py .....                                         [ 88%]
tests/test_uploads.py ......                                             [ 90%]
tests/test_vector_integration.py .................                       [ 95%]
tests/test_webui.py ................                                     [100%]

======================= 325 passed, 5 warnings in 92.27s =======================
```

---

## 7. 部署与交付状态

- **代码状态**: 所有实现代码、工具函数、测试用例均已提交至本地 Git `main` 分支。
- **保护性约束**: 严格遵循用户指示，**未执行** `git push`、**未打 tag**、**未构建 GHCR Docker 镜像**、**未执行 NAS 部署**。
- **就绪性**: 代码具备完整的自验证保障，随时可根据用户明确授权执行下游镜像打包或发布。
