# 会话记忆架构与 NAS 服务审计

审计日期：2026-09-12  
状态：完成现状调查与目标设计；未修改 Unraid 配置，未写入正式 Obsidian Vault，未启动全量会话导入或批量 embedding。

## 1. 结论

现有 `G:\LLM\memory` 适合继续扩展，不需要新建平行的 memory 项目。它已经具备 SQLite、FTS、可选 embedding/rerank、MCP、WebUI、来源引用和审计能力；本次增加的 Codex export reader、标准化模型、增量 manifest 和 Vault writer 可以作为独立 ingestion 子模块接入。

推荐的长期边界是：

```text
原始 ZIP / 原始附件                 immutable archive
        ↓
Research Memory Gateway ingestion   解析、标准化、去重、增量判断
        ↓
Obsidian Markdown / Assets           canonical data
        ↓
SQLite FTS + embeddings/vector       derived cache，可全部重建
        ↓
MCP search/read/recall               Agent 查询接口
```

## 2. 现有 Research Memory Gateway 审计

### 2.1 已有能力

| 层 | 现有实现 | 复用判断 |
|---|---|---|
| 配置 | `config.py` 和 YAML；SQLite/Nocturne、keyword/hybrid、embedding/rerank 均可配置 | 复用 |
| 存储 | `SQLiteMemoryBackend`、schema migration、memory/claim/evidence/source-ref 表 | 复用，但会话 archive 不应被压扁成现有单条 research memory |
| 精确检索 | SQLite FTS | 复用并扩展到 Vault section/chunk |
| 语义检索 | OpenAI-compatible embedding client、SQLite 中保存向量 | 可复用客户端；chunk 索引需独立表或适配层 |
| 重排 | 可选 rerank client | 接口保留，第一版关闭 |
| Agent | capture/recall/verify/project-state MCP surface | 复用，稳定后增加 Vault-backed search/read/recall |
| 来源追溯 | source refs 和 allowlist resolver | 复用概念；ZIP ordinal/message ID 需新的 resolver |
| 运维 | admin audit、FTS repair、embedding backfill、WebUI | 复用 |

### 2.2 新增模块的位置

当前 prototype 放在：

```text
src/research_memory_gateway/conversations/
  models.py
  codex_export.py
  manifest.py
  vault_writer.py
  pipeline.py
```

这与现有架构兼容。后续只有在 reader 种类增多时，才值得把它拆成 `ingestion/readers`、`ingestion/normalize` 等更细目录。现在为了匹配建议目录而搬动现有模块，收益不足。

### 2.3 需要保持分离的两类对象

1. **Conversation Archive**：完整、可读、可追溯的会话记录，主数据在 Vault Markdown。
2. **Research Memory**：经用户确认、证据优先的稳定科研结论或决策，继续使用 Gateway 现有模型。

不能把 322 场历史会话自动转成 322 条已确认 research memory。后续可以从会话 archive 中提议候选项目事实或研究决策，但仍应遵守人工确认和证据状态。

## 3. Unraid 与模型服务检查

### 3.1 Research Memory Gateway

| 地址 | 实测结果 | 解释 |
|---|---|---|
| `192.168.22.102:18787` | MCP 端点可达；`/mcp`、`/sse` 未授权访问返回 401 | 服务在线并启用认证 |
| `192.168.22.102:18788` | WebUI 登录页可达 | 管理界面在线 |

通过 live MCP/API 审计确认：

- backend：SQLite
- schema version：5
- retrieval mode：keyword
- embedding：关闭且未配置
- rerank：关闭且未配置
- 数据库中已有 4 条旧 embedding，维度 1024
- FTS、一致性、orphan embedding、backfill 队列和 JSON 数据均正常
- 存在 1 条无效 conversation source ref：`mem_8ebe3632b7854f8f9a6cc457dc0b4e75`，缺 source ID/path；本次没有修改

### 3.2 NAS 模型服务

模型入口：`192.168.22.102:28001/v1`

| 模型/接口 | 实测 | 决策 |
|---|---|---|
| `bge-m3` embeddings | HTTP 200；输出 1024 维向量 | 可作为第一版 embedding provider |
| `qwen-reranker` | HTTP 503 | 第一版不得依赖；保持关闭 |

Gateway 已支持 base URL 以服务根路径或 `/v1` 结尾，并可调用 `/embeddings`。配置应通过环境变量或 WebUI secret 保存，不把 token 写入 Vault 或生成文档。

建议配置形态：

```yaml
retrieval:
  mode: hybrid
  embedding:
    enabled: true
    base_url_env: EMBEDDING_BASE_URL
    api_key_env: EMBEDDING_API_KEY
    model_env: EMBEDDING_MODEL
    endpoint_path: /embeddings
  rerank:
    enabled: false
```

本次没有修改正在运行的 Unraid 配置。启用前应先对 staging Markdown 建立可删除的测试索引，并验证向量维度和模型 identity。

### 3.3 向量数据库发现结果

从当前客户端网络未发现可达的 Qdrant、pgvector/PostgreSQL、Milvus、OpenSearch/Elasticsearch 或 Meilisearch 服务；常用端口均未暴露。这个结果只能说明“当前网络不可达”，不能证明 Unraid 内部没有未映射容器。

第一版不需要额外引入向量数据库。现有 SQLite FTS 加 SQLite 保存 1024 维 embedding 足以验证数据模型和 hybrid retrieval。只有在 chunk 规模、延迟或并发达到明确瓶颈后，再将 vector adapter 替换为 Qdrant 等服务。

## 4. Obsidian 主数据设计

### 4.1 正式目录尚未确定

用户指定的 `G:\LLM\obsidian` 当前不存在。实际存在 `G:\LLM\Research-AI-Hub\Vault`，但尚不能证明两者是同一个 Vault 或预期挂载目标。因此：

- 不创建新的空 `G:\LLM\obsidian`
- 不向 `G:\LLM\Research-AI-Hub\Vault` 擅自写入
- prototype 继续留在 `G:\LLM\memory\exports\conversation-staging`
- 正式导入前必须确认真实 Vault 根目录及 Research Hub 的挂载方式

### 4.2 推荐逻辑结构

在真实 Vault 确认后，建议使用：

```text
90_System/AI-Memory/
  Conversations/YYYY/
  Projects/
  Topics/
  Assets/
  Manifests/
```

如果 Vault 已有正式项目笔记，`Projects/` 只保存链接和机器维护的状态索引，不复制第二份项目主库。Topics 只创建稳定、跨会话有复用价值的主题，不按每个关键词生成笔记。

### 4.3 Conversation Note 粒度

第一版以“一场 session/thread 一篇 Markdown”为默认粒度，并保留 parent thread。超长或多主题会话可以在后续语义分段阶段拆成多个主题 section，但必须共享同一个 `conversation_id` 和 source anchors。

每篇 note 至少保留：

- `conversation_id`、parent/thread source、创建/更新时间
- source archive、ZIP entry、entry SHA-256、parser/schema version
- completion status
- 人类可读的用户目标、过程、精确事实、结果、未解决问题
- 文件路径、命令、错误、参数、日期、实验数值等精确信息
- 每条原始内容的 ordinal/message ID/turn ID
- 工具 payload 的 hash、大小、短摘要和 call ID
- 附件 hash、MIME、大小、source ordinal；大附件不内嵌

Markdown 是长期证据副本之一，但原始 ZIP 仍需永久保留，尤其用于恢复未进入正文的工具输出、注入 metadata 和附件 base64。

## 5. Manifest 与增量导入

manifest ledger 至少记录：

```text
source archive path/hash
source entry path/hash
conversation ID
output Markdown path/hash
parser version
schema version
last imported/indexed
embedding model/version
```

增量规则：

1. conversation ID 不存在：`new`。
2. ID 相同且 source entry hash 相同：`unchanged`，跳过解析后的写入和 embedding。
3. ID 相同但 source hash 改变：`changed`，重新生成机器管理内容。
4. 输出 Markdown hash 与 manifest 不一致：`conflict`，不得静默覆盖人工修改。
5. 仅 metadata 变化而 section 内容 hash 不变：更新 metadata/FTS，复用 embedding。
6. embedding identity 使用 `hash(normalized_content + model + model_version)`。

## 6. 索引与 Agent 接口

### 6.1 Chunking

索引源只能是生成并校验后的 Markdown：

```text
file -> heading -> subheading -> oversize token split
```

每个 chunk 至少保存：`vault_path`、heading、conversation ID、project、date、content hash、chunk index、source anchors、embedding identity。

默认排除：system/developer 注入、重复 event 镜像、base64、巨型 stdout、网页全文、DOM/tool schema、加密 reasoning/compaction。

### 6.2 Hybrid retrieval

第一版排序只需：

```text
SQLite FTS/BM25
  + cosine similarity（可选）
  + metadata filters
  -> 去重和简单加权
```

精确字符串如 `P0-4`、`324 nm`、`ERROR_NO_SYSTEM_RESOURCES` 和 Windows 路径必须由 FTS/lexical 通道召回，不能只依赖 embedding。

### 6.3 MCP

在 ingestion 和索引稳定后，为现有 agent surface 增加：

- `memory_search`：返回 path、title、heading、excerpt、score、date、project、conversation ID
- `memory_read`：读取 Vault 文件或指定 heading
- `memory_recall`：hybrid search、去重、可选 rerank、token budget

MCP 不承担扫描 300 MB ZIP、生成 Markdown 或批量 backfill；这些属于 CLI/后台批处理。

## 7. 分阶段实施计划

| 阶段 | 交付 | 门控 |
|---|---|---|
| 0. 原始归档 | ZIP hash、只读 archive registry | ZIP hash 稳定，禁止原地修改 |
| 1. Parser prototype | 5 场不同类型会话恢复、测试、审计文档 | ordinal/message/tool/attachment 可追溯 |
| 2. Vault staging | 可读 Markdown、冲突保护、manifest | 人工抽查通过；确认正式 Vault 根目录 |
| 3. Attachment inventory | 路径发现、hash 去重、missing report | 不复制大附件，不丢失缺失状态 |
| 4. Incremental import | new/changed/unchanged/conflict | 重复运行幂等；人工编辑不被覆盖 |
| 5. Lexical index | heading-aware SQLite FTS | 精确字符串查询通过 |
| 6. Embedding cache | `bge-m3` provider、content/model identity | 删除索引后能从 Markdown 重建 |
| 7. Hybrid retrieval | 简单融合、metadata filter | 与 lexical-only 做固定查询集对照 |
| 8. MCP recall | search/read/recall | 所有返回结果可追踪到 Vault/source ZIP |
| 9. 全量导入 | 322 场分批写入 staging，再迁移正式目录 | 抽样 QA、缺失数据报告、备份和回滚计划 |

## 8. 当前阻断与剩余风险

1. 正式 Vault 根目录未确认，是批量写入的硬门控。
2. 外部附件不在 ZIP 内，后续必须扫描原路径并报告缺失。
3. 85 个外层 compacted 和加密 reasoning 无法从导出恢复全文。
4. `qwen-reranker` 当前 503，不能作为必需组件。
5. 已有 4 条旧 embedding 的模型 identity 需要在启用新 backfill 前确认，不能和新 `bge-m3` 向量混用而不标版本。
6. 无效 source ref 应单独审计，不能在本任务中顺手修复。

