# Conversation Memory Pipeline 实施任务书

任务日期：2026-09-12  
执行项目：`G:\LLM\memory`  
执行 Agent：Flash Agent  
任务性质：在现有 Research Memory Gateway 内扩展会话归档、Obsidian 主数据、增量索引和 Agent recall；不得新建平行 memory 项目。

## 1. 最终目标

将 Codex/ChatGPT 等 AI 会话导出转成长期可维护的记忆系统：

```text
Immutable Raw Export
  -> conversation ingestion / normalization
  -> Obsidian Markdown canonical archive
  -> heading-aware lexical + vector derived index
  -> MCP search / read / recall
```

必须满足：

1. 用户能直接在 Obsidian 浏览会话、项目和时间线。
2. Agent 能检索历史记录恢复上下文。
3. Markdown 和原始附件是 source of truth。
4. FTS、embedding、vector DB 都能删除并从 Markdown 重建。
5. embedding 通过 NAS API 调用，本机不加载大模型。
6. 新导出只处理 new/changed conversation。
7. 所有事实能追踪到原始 ZIP entry、ordinal、message ID 或附件。

## 2. 已有基线，禁止重做

当前工作树已有未提交实现，必须在其上继续，不得删除、回退或另写一套：

```text
src/research_memory_gateway/conversations/
  __init__.py
  models.py
  codex_export.py
  manifest.py
  vault_writer.py
  pipeline.py

scripts/prototype_codex_conversations.py
tests/test_conversation_ingestion.py
```

已有审计：

- `docs/EXPORT_FORMAT_AUDIT.md`
- `docs/CONVERSATION_MEMORY_ARCHITECTURE_AUDIT.md`
- `docs/CONVERSATION_INGESTION_PROTOTYPE.md`

当前 parser version：`codex-export-v1.3`。

当前验证基线：

- 5 场 representative sessions 已写 staging。
- 5/5 source hash、ordinal、message ID 回溯通过。
- 5/5 重复运行返回 `skipped: unchanged`。
- 完整测试：`125 passed`。

执行前先阅读上述三份文档及现有 conversation 模块。不得假设 ZIP 格式，需要以现有审计和真实 fixture 为准。

## 3. 不可违反的边界

### 3.1 数据安全

- 不修改原始 ZIP：
  `D:\Partition\F\Study\博士文件\Research-AI-Hub\_codex_workspace\tmp\chat-export\codex-sessions-20260912-012538.zip`
- 不删除或覆盖现有 Gateway 数据库、旧 embedding、人工笔记或 Vault 文件。
- 不把 base64、大型 stdout、网页全文、DOM、tool schema、system prompt 或 encrypted reasoning 写入 Markdown/embedding。
- 不把 conversation archive 自动保存成“已确认科研结论”。Research Memory 仍遵循 evidence/confirmation policy。
- 不记录 API key、token、cookie 或其他凭据。

### 3.2 批量操作门控

本任务只完成代码、测试和少量 staging 验证。不得：

- 全量导入 322 场会话
- 批量调用 embedding
- 写入正式 Vault
- 修改 Unraid 正在运行的配置
- 部署/重启容器
- commit、push 或创建 release

这些操作必须由用户另行确认。

### 3.3 Vault 路径门控

`G:\LLM\obsidian` 当前不存在；`G:\LLM\Research-AI-Hub\Vault` 存在，但尚未确认是否为正式 canonical Vault。

在用户确认前，只能输出到：

`G:\LLM\memory\exports\conversation-staging`

配置和 CLI 必须拒绝把不存在或未显式确认的目录当正式 Vault 自动创建。

## 4. 目标模块边界

在现有架构内演进，推荐边界如下；无需为了目录外观搬动当前模块：

```text
conversations/
  readers/ or codex_export.py    raw export adapters
  models.py                      normalized conversation model
  filters.py                     injected/duplicate/oversize policy
  attachments.py                 attachment inventory and missing report
  manifest.py                    incremental ledger
  vault_writer.py                canonical Markdown writer
  chunking.py                    heading/section chunks
  index.py                       FTS/vector derived index
  retrieval.py                   hybrid search/read/recall
  pipeline.py                    orchestration
```

只有当新增第二种 export reader 后，才拆 `readers/`。保持模块小而明确，不做与任务无关的重构。

## 5. 工作包 P0：基线保护

### 任务

1. 检查 git root、branch、status，记录当前未提交文件。
2. 阅读三份审计文档和 conversation 模块。
3. 运行 `tests/test_conversation_ingestion.py` 和完整 pytest。
4. 不因权限问题修改生产代码。测试中的相对 `exports/` 应重定向到受控临时目录。

### 验收

- 当前 125 项测试保持通过。
- 没有修改原始 ZIP、正式 Vault、运行中 NAS 配置。
- 任务报告记录基线和任何既有异常。

## 6. 工作包 P1：配置和路径安全

### 任务

在 `config.py` 增加向后兼容的 conversation archive 配置，例如：

```yaml
conversation_archive:
  enabled: false
  staging_dir: ./exports/conversation-staging
  vault_root: null
  canonical_subdir: 90_System/AI-Memory
  manifest_path: ./data/conversation-imports.sqlite
  index_path: ./data/conversation-index.sqlite
  require_explicit_vault_confirmation: true
  copy_embedded_images: false
```

要求：

- 默认关闭，不改变现有部署行为。
- staging 和 canonical vault 分开。
- 路径解析后必须验证是否位于配置允许的根目录。
- 目标同名人工文件存在时返回 conflict。
- 禁止自动创建一个新的空 Vault 根目录。

### 验收

- 旧 config 不加任何字段仍能启动。
- 路径越界、未确认 Vault、人工文件碰撞均有明确错误。
- 添加配置解析和安全边界测试。

## 7. 工作包 P2：Parser/Normalizer 生产化

### 任务

在现有 `CodexExportReader` 上补齐：

- archive、manifest、entry hash 校验结果结构化输出。
- message/tool/attachment/special object 的稳定 normalized schema。
- 注入内容分类理由，不只保存一个布尔值。
- event mirror 去重统计。
- incomplete/aborted/final 状态和依据。
- parent thread、thread source、agent path、cwd、时间字段语义。
- 巨型 payload 只保存摘要、长度、hash 和 source anchor。
- 单条 JSON 错误不导致整场静默丢失；记录 error ordinal/line。

不要恢复或输出隐藏 thinking。encrypted reasoning/compaction 只记录存在性和来源。

### 验收

- 现有 5 场真实样本仍可解析。
- fixture 覆盖普通会话、subagent、guardian、tool、image、compaction、abort、损坏 JSONL。
- `response_item/message` 与 event mirror 不重复进入正文。
- 注入型 user message 不进入“用户目标”。

## 8. 工作包 P3：附件 inventory 与缺失报告

### 任务

新增附件清点模块，区分：

- embedded data URI
- 会话提到的本地绝对路径
- ZIP 内 entry
- URL
- tool/artifact reference
- 无法解析的 locator

每条记录至少包含：

```text
conversation_id
message_id / tool_call_id
source ordinal
locator type
original locator
mime/extension
size（可得时）
content hash（可得时）
status: found / missing / embedded / remote / unresolved
resolved path（只在 allowlist 内）
```

默认只生成 inventory，不复制附件。后续复制到 `AI-Memory/Assets` 时必须 content hash 去重，大文件有 size threshold，且保留原路径。

### 验收

- 能对 5 场样本输出 JSON/CSV missing report。
- 不把 base64 写入 report 或 Markdown，只写 hash/size/MIME。
- 外部文件不存在时明确标 `missing`，不能忽略。
- 不扫描 allowlist 之外的任意磁盘路径。

## 9. 工作包 P4：Obsidian Writer 与人工可维护性

### 任务

增强 writer，使 archive note 同时适合人工浏览和机器重建：

- YAML 保留 source/archive/parser/schema/hash 字段。
- 正文按真实内容选择：背景、目标、过程、事实、决策、结果、未解决问题、文件/路径/命令/参数、关键原始内容、Related、Source。
- 精确数值、实验参数、错误文本、版本、路径不得被摘要丢失。
- 每个关键 section 或消息保留 source anchor。
- 超长会话允许按 semantic topic 形成多个 section，但不能丢失 conversation identity。
- 自动生成 project/timeline 候选，只使用可解释规则；低置信度不得强写项目归属。
- 允许人工 frontmatter/related link 与机器管理内容共存。

建议使用明确的 machine-managed markers，更新时只替换管理区域；若当前 note hash 与 manifest 不一致且不能可靠合并，返回 conflict 并生成 `.candidate.md`，绝不覆盖。

### 验收

- 5 篇 staging note 在 Obsidian 中结构清楚。
- Windows 路径、`324 nm`、`60 min`、错误码等精确查询文本仍存在。
- Markdown 无 base64、巨型 tool stdout、重复 system prompt。
- 重复导入不改变文件 hash。
- 人工增补 Related/项目链接后不会被静默删除。

## 10. 工作包 P5：Manifest 和增量状态机

### 任务

扩展当前 manifest，至少记录：

```text
archive path/hash
source entry path/hash
conversation ID
parent thread ID
output path/hash
parser/schema version
attachment inventory hash
last imported/indexed
content section hashes
embedding model/version/dimension
status/error
```

状态机：

- `new`
- `unchanged`
- `changed_source`
- `parser_upgrade`
- `output_missing`
- `managed_output_modified`
- `attachment_changed`
- `index_stale`
- `failed_retryable`

SQLite 是操作 ledger；同时导出人类可读 CSV/JSON。manifest 本身不是 canonical content。

### 验收

- 同一 ZIP 连续运行完全幂等。
- 新 archive 中 source hash 相同的 conversation 不重算正文和 embedding。
- parser version 改变时能选择 dry-run/重建，不默认覆盖。
- 失败可以续跑，不需要重做已成功项。

## 11. 工作包 P6：Markdown 派生索引

### 任务

索引输入只能是已生成的 Markdown，不直接把 ZIP tool payload 送 embedding。

实现 heading-aware chunking：

```text
Markdown file
  -> heading
  -> subheading
  -> oversize token split
```

SQLite 建议表：

```text
conversation_documents
conversation_sections
conversation_sections_fts
conversation_embeddings
conversation_index_runs
```

每个 section/chunk 保存：

```text
vault_path
heading path
conversation_id
project/date/topics
content_hash
chunk_index
source anchors
embedding identity
```

embedding identity：

```text
sha256(normalized_content + embedding_model + embedding_version)
```

metadata 改变而 normalized content 不变时，不重算 embedding。

### NAS 模型

- base URL：`192.168.22.102:28001/v1`
- embedding model：`bge-m3`
- 已验证维度：1024
- `qwen-reranker` 当前 HTTP 503，保持关闭

通过现有 OpenAI-compatible client 调用。不要在代码中写死 IP/model/token，使用 config/env。

### 验收

- 删除 conversation index DB 后可完全从 staging Markdown 重建。
- FTS 在 embedding 关闭/服务失败时独立可用。
- 模型维度不一致时拒绝混写并报告。
- dry-run 不调用模型、不修改 index。
- 仅对内容 hash/model identity 改变的 chunk 调 embedding。

## 12. 工作包 P7：Hybrid Retrieval 与 MCP

### 任务

实现基础融合：

```text
SQLite FTS/BM25
  + vector cosine candidates（可选）
  + metadata filters
  -> dedup
  -> 简单、可解释的 score fusion
  -> token budget
```

不引入新 vector DB，不依赖 reranker。

新增 MCP 工具，名称可在不破坏现有 `recall_memory` 的前提下采用：

### `memory_search`

返回：path、title、heading、excerpt、lexical/vector/final score、date、project、conversation ID。

### `memory_read`

读取指定 Vault note 或 heading；只允许已配置 Vault/staging 根目录内路径。

### `memory_recall`

执行 hybrid search、dedup、metadata filter、token budget，输出可直接作为 Agent context 的片段，并保留 source anchors。

若现有 MCP tool naming 会冲突，可使用 `conversation_search/read/recall`，同时在 service 层保留上述语义；不得重命名或破坏现有公开工具。

### 固定查询集

至少测试：

- `P0-4`
- `324 nm`
- `60 min`
- `G:\LLM\memory`
- `ERROR_NO_SYSTEM_RESOURCES`
- `35984`
- conversation ID 精确过滤
- parent thread 查询

### 验收

- exact string 由 FTS 稳定召回。
- embedding 服务故障自动降级为 lexical，并返回诊断 metadata。
- 每个结果都能追到 Markdown 和原始 ZIP。
- path traversal 被拒绝。
- MCP 不执行 ingestion 或批量 embedding。

## 13. 工作包 P8：CLI、测试和文档

### CLI

新增或扩展稳定 CLI：

```text
conversation audit-export
conversation import --staging --dry-run
conversation inventory-attachments
conversation rebuild-index
conversation index --changed-only
conversation search
conversation validate
```

所有批处理支持：config、session filter、limit、dry-run、resume、JSON report。危险操作必须显式参数确认。

### 自动化测试

至少覆盖：

- parser 和损坏输入
- 注入/重复过滤
- attachment found/missing/unresolved
- manifest 全状态机
- writer 冲突与人工区域保留
- heading chunking
- FTS exact query
- embedding cache/model identity/dimension mismatch
- hybrid fallback
- MCP path allowlist
- 删除 index 后重建

测试不得连接真实 NAS；使用 mock HTTP。真实 `bge-m3` 只做一个显式、可跳过的 smoke script。

### 文档

更新：

- `config.example.yaml`
- `README.md`
- conversation ingestion/operator 文档
- schema/migration 文档
- NAS embedding smoke test 说明
- 数据丢失与 missing attachment 报告说明

## 14. 交付物

Flash Agent 完成后必须交付：

1. 修改文件清单和架构说明。
2. 数据库 schema/migration 说明。
3. CLI 和 MCP 工具示例。
4. 单元/集成测试实际结果。
5. 5 场 staging validation 报告。
6. attachment missing report 样例。
7. exact query 检索结果表。
8. 未完成项、风险和正式批量导入前门控。

报告写入：

`docs/CONVERSATION_MEMORY_IMPLEMENTATION_REPORT.md`

## 15. 执行顺序与停止条件

按 `P0 -> P1 -> P2 -> P3 -> P4 -> P5 -> P6 -> P7 -> P8` 执行。每个工作包完成后先运行相关测试，再继续下一包。

出现以下情况必须停止相关写操作并报告，不得自行绕过：

- 真实 Vault 路径仍未确认
- 原始 ZIP hash 与审计不一致
- 同 conversation ID 出现不可解释的不同 source
- 需要覆盖人工 Markdown
- 需要修改现有生产数据库 schema 且没有 migration/backup
- embedding 维度与现存记录不一致
- 需要凭据、部署、容器重启、批量导入或批量 embedding

## 16. 给执行 Agent 的直接指令

```text
在 G:\LLM\memory 中执行 docs/CONVERSATION_MEMORY_IMPLEMENTATION_TASKBOOK.md。

先读取 EXPORT_FORMAT_AUDIT.md、CONVERSATION_MEMORY_ARCHITECTURE_AUDIT.md、CONVERSATION_INGESTION_PROTOTYPE.md 和当前 conversations/ 实现。保留所有现有未提交修改，在现有 Research Memory Gateway 内扩展，不新建平行项目。

依次完成 P0-P8 的代码、测试、文档和 5 场 staging 验证。禁止全量导入 322 场会话、禁止写正式 Vault、禁止批量 embedding、禁止修改或重启 Unraid 服务、禁止 commit/push。NAS bge-m3 只允许显式 smoke test；自动化测试必须 mock 网络。遇到 Vault 路径、生产数据库迁移、凭据、批量操作或人工文件冲突时停止相关写操作并报告。

最终生成 docs/CONVERSATION_MEMORY_IMPLEMENTATION_REPORT.md，列出实际完成项、测试结果、精确查询验证、缺失附件样例和剩余门控。不要只给设计建议；在安全边界内直接实现并验证。
```

