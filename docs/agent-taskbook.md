# Agent Taskbook: Research Memory Gateway Roadmap

本文档用于在新窗口中分发给多个 agent 并行实现。每个任务包应在独立分支或独立 Agent Manager worktree 中完成，完成后提交小而清晰的 PR 或 commit。

## 当前已实现

- MCP 服务入口：`propose_save`、`save_research_memory`、`search_research_memory`、`check_overlap`、`open_source_ref`、`audit_unverified`、`export_memories`。
- SQLite 持久化：`memories`、`memories_fts`、`memory_embeddings`。
- 强证据 schema：`claims`、`evidence`、`source_refs`、`entities`、`relations`。
- 用户确认写入策略：默认 `require_user_confirmation=true`。
- SQLite FTS 检索和 hyphenated scientific terms 回退。
- 可选 hybrid retrieval：FTS + embedding vector candidates + rerank。
- OpenAI 风格 embedding 请求和基础 rerank 请求。
- Docker/GHCR/NAS compose 文件。
- 中文 README、部署文档、客户端配置文档、schema 文档、skill 和 system prompt。

## 当前未实现或半实现

- Nocturne backend 只是占位，`save/search/list_all/get` 未映射真实 Nocturne MCP/HTTP 契约。
- embedding/rerank 只支持最小 HTTP 契约，没有多厂商兼容、健康检查、错误可观测性和重试策略。
- 已有 SQLite 记忆没有批量向量回填工具，开启 embedding 后只会给新保存的记忆生成向量。
- 没有显式管理工具：删除、更新、合并、标记 superseded/retracted、冲突解决、批量导入。
- SSE 模式只警告缺少 `RESEARCH_MEMORY_TOKEN`，没有服务端强制鉴权中间件。
- 没有 `/health`、`/metrics` 或 MCP 工具级诊断输出。
- 没有 Docker/NAS 端到端测试；本地 Docker 不可用时只能依赖 GitHub Actions。
- 没有数据库迁移框架；schema 变更靠 `CREATE TABLE IF NOT EXISTS`。
- 没有向量表重建、损坏检测、维度一致性检测。
- 没有 source_refs 的深度集成测试，当前更多是 allowlist resolver 基础能力。
- 没有 CLI 管理命令。
- 没有导入/导出迁移到 Obsidian/Zotero/AnythingLLM/Nocturne 的正式流程。
- 没有 GitHub release/versioning 自动化说明。

## 推荐执行顺序

1. Agent A：生产化 hybrid retrieval。
2. Agent B：批量回填和 CLI 管理工具。
3. Agent C：鉴权、健康检查、可观测性。
4. Agent D：记忆管理工具集。
5. Agent E：数据库迁移和数据完整性。
6. Agent F：文档、NAS 部署手册和客户端配置矩阵。
7. Agent G：Nocturne backend 适配调研与接口设计。
8. Agent H：端到端测试和 GitHub Actions 发布增强。

Nocturne 相关任务应晚于 A-F，除非先确认 Nocturne 的真实部署接口。

## Agent A: Hybrid Retrieval Productionization

### Goal

让当前 embedding/rerank 接入从“基础可用”变成“生产可用”，重点适配已部署的向量模型和重排模型。

### Scope

- 支持 OpenAI-compatible embeddings：`/v1/embeddings`、`/embeddings` 两种 base URL/path 组合。
- 支持常见 rerank 响应格式：`results[].index/relevance_score`、`results[].document.index/score`、`data[]`。
- 增加模型服务健康检查函数或 MCP tool，例如 `retrieval_health`。
- 增加错误日志，不要静默吞掉所有错误。
- 增加超时、重试、禁用后降级策略。
- 检查 embedding 维度一致性，发现维度不一致时跳过该向量并报告。
- 优化 hybrid merge 逻辑，让 FTS rank、vector score、rerank score 的含义清晰。

### Out Of Scope

- 不引入外部向量数据库。
- 不实现 Nocturne。
- 不改变 memory schema。

### Acceptance Criteria

- 未配置 embedding/rerank 时，所有现有测试通过，行为保持 `keyword`。
- 配置 embedding 但服务失败时，搜索自动退回 FTS，并有可诊断信息。
- 配置 rerank 但服务失败时，返回未重排 hybrid 结果，并有可诊断信息。
- 新增单元测试覆盖至少 3 种 embedding/rerank 响应格式。
- `pytest` 通过。

### Suggested Prompt

```text
Implement Agent A from docs/agent-taskbook.md. Focus on productionizing hybrid retrieval without adding external vector DBs or changing the public MCP tool schema. Add tests and update docs. Run pytest before final response.
```

## Agent B: Backfill And CLI Management

### Goal

为已有 SQLite 记忆补生成向量，并提供基础 CLI 管理命令。

### Scope

- 新增 CLI 子命令或模块函数：`backfill-embeddings`。
- 支持参数：config path、project filter、memory_type filter、dry-run、limit、force。
- 对已有 `memories` 逐条生成 embedding 并写入 `memory_embeddings`。
- 统计成功、跳过、失败、维度不一致、服务错误。
- 新增 CLI 命令：`inspect-db`，显示 memory 数量、embedding 数量、缺失向量数量、项目分布、类型分布。
- 文档写明 NAS 上如何进入容器执行回填。

### Out Of Scope

- 不做删除/合并记忆。
- 不做 UI。

### Acceptance Criteria

- 可以对测试数据库 dry-run，不写入向量。
- 可以 force 重建已有向量。
- embedding 服务不可用时不会损坏数据库。
- 新增测试覆盖 dry-run、force、project filter。
- `pytest` 通过。

### Suggested Prompt

```text
Implement Agent B from docs/agent-taskbook.md. Add CLI utilities for embedding backfill and SQLite inspection. Keep SQLite as the only storage backend. Add tests and docs. Run pytest before final response.
```

## Agent C: Auth, Health, And Observability

### Goal

让 NAS/SSE 部署更安全、可诊断。

### Scope

- 为 SSE/HTTP transport 增加可选 Bearer token 鉴权。
- `RESEARCH_MEMORY_TOKEN` 设置后必须校验请求 Authorization header。
- 增加 `/health` 或等效 MCP tool：服务状态、backend 状态、DB 可写性、retrieval 配置状态。
- 增加结构化日志：保存、搜索、open_source_ref、export、retrieval fallback。
- 避免记录敏感 token、完整 API key、完整未发表内容。
- 文档更新：NAS + reverse proxy + token 配置。

### Out Of Scope

- 不实现用户系统。
- 不实现 OAuth。
- 不改变 MCP tool 输入输出，除非新增诊断 tool。

### Acceptance Criteria

- token 未设置时保持当前内网友好模式，并有明确警告。
- token 设置时，缺失或错误 token 的 HTTP/SSE 请求被拒绝。
- 健康检查能报告 SQLite 可连接和 retrieval 是否启用。
- 新增测试或至少可运行的集成验证脚本。
- `pytest` 通过。

### Suggested Prompt

```text
Implement Agent C from docs/agent-taskbook.md. Add token auth, health diagnostics, and safe structured logging for NAS deployment. Preserve current default behavior when token is absent. Add tests/docs and run pytest.
```

## Agent D: Research Memory Management Tools

### Goal

补齐长期记忆生命周期管理能力，避免数据库长期变成不可清理的堆积物。

### Scope

- 新增 MCP tools：`get_research_memory`、`update_research_memory`、`delete_research_memory`、`mark_memory_status`、`merge_research_memories`。
- 删除和覆盖必须要求 `user_confirmed=true`。
- `mark_memory_status` 支持把 claims 标记为 `superseded`、`retracted`、`conflicting`，并写入原因 metadata。
- `merge_research_memories` 支持保留 source_refs、claims、evidence，并将旧 memory 标记 superseded，而不是直接删除。
- 更新 tests 和 README tool table。

### Out Of Scope

- 不实现全文 diff UI。
- 不实现自动冲突解决。

### Acceptance Criteria

- 误删保护：未确认时删除失败。
- merge 后旧 memory 可追溯，新 memory 保留来源。
- update 后 FTS 和 embedding 状态正确刷新或标记需回填。
- `pytest` 通过。

### Suggested Prompt

```text
Implement Agent D from docs/agent-taskbook.md. Add lifecycle management MCP tools with user confirmation safeguards and evidence/source_refs preservation. Add tests and update docs. Run pytest.
```

## Agent E: Database Migration And Integrity

### Goal

为后续 schema 演进提供安全迁移机制和完整性检查。

### Scope

- 新增 `schema_migrations` 表。
- 把当前表结构初始化迁移为版本化 migration。
- 新增 integrity check：memories 与 FTS 同步、embedding orphan、missing source fields、invalid JSON。
- 新增 MCP tool 或 CLI：`audit_database_integrity`。
- 支持自动修复 FTS index，可选修复 orphan embeddings。

### Out Of Scope

- 不迁移到 PostgreSQL。
- 不引入 Alembic，除非必要；优先轻量自研 migration runner。

### Acceptance Criteria

- 新数据库可从 migration 初始化。
- 旧数据库打开时自动补齐新表，不丢数据。
- integrity check 能发现并报告 FTS 缺失和 orphan embeddings。
- `pytest` 通过。

### Suggested Prompt

```text
Implement Agent E from docs/agent-taskbook.md. Add lightweight SQLite migrations and integrity auditing/repair without external migration frameworks unless absolutely necessary. Preserve old databases. Add tests and docs.
```

## Agent F: Documentation And Client Configuration Matrix

### Goal

把项目变成用户可直接部署和接入的文档完整版本。

### Scope

- 更新 `README.md` 为“快速启动、NAS、客户端、模型配置、维护、故障排查”结构。
- 增加 `docs/client-config.md` 的实际示例：Kilo、Cherry Studio、Codex、本地 stdio、远程 SSE。
- 增加 embedding/rerank 服务配置示例：OpenAI-compatible、Ollama/vLLM/TEI/Jina/BGE-reranker 类服务的可填模板。
- 增加“已有记忆如何 backfill embeddings”。
- 增加“什么时候不要保存长期记忆”的规范。
- 增加“未发表科研数据安全提醒”。

### Out Of Scope

- 不改代码，除非发现 docs 与实际配置不一致。

### Acceptance Criteria

- 新用户能按 README 在 NAS 上启动服务。
- 文档清楚区分 `keyword` 与 `hybrid`。
- 文档不再暗示当前必须部署 Nocturne。
- 所有命令与文件名匹配仓库实际内容。

### Suggested Prompt

```text
Implement Agent F from docs/agent-taskbook.md. Improve docs and client configuration examples for NAS, Kilo, Cherry Studio, Codex, SQLite keyword mode, and hybrid embedding/rerank mode. Do not make unnecessary code changes.
```

## Agent G: Nocturne Backend Research And Design

### Goal

不要直接写死 Nocturne，先产出清晰接口设计和风险评估。

### Scope

- 调研项目中现有 `NocturneMemoryBackend` 占位边界。
- 定义需要用户提供的 Nocturne 信息：URL、transport、tool names、auth、namespace、URI scheme。
- 设计 `NocturneMemoryBackend` 映射：`save/search/list_all/get`。
- 设计数据映射：ResearchMemory JSON 如何存到 Nocturne，如何恢复。
- 设计 fallback：Nocturne 失败时是否写 SQLite cache。
- 输出 ADR 文档，不强制实现代码。

### Out Of Scope

- 在未确认 Nocturne 实际契约前，不实现生产代码。
- 不改变默认 backend。

### Acceptance Criteria

- 新增 `docs/adr/` 中一篇 Nocturne backend ADR。
- 列出至少 3 种接入方案和推荐方案。
- 明确哪些信息必须由用户确认后才能实现。
- 不破坏现有测试。

### Suggested Prompt

```text
Implement Agent G from docs/agent-taskbook.md. Do not write Nocturne production code yet. Create an ADR that designs the Nocturne backend mapping, required user-provided contract, risks, and recommended approach. Run tests if code changes are made.
```

## Agent H: End-To-End Tests And Release Automation

### Goal

让 CI、Docker 镜像和 NAS 部署更可验证。

### Scope

- 增加 GitHub Actions test workflow：安装依赖、运行 `pytest`。
- Docker publish workflow 在 build 前运行 tests。
- 增加 smoke script：启动服务后调用 MCP/HTTP 或最低限度 import/build 验证。
- 增加 release tagging 文档。
- 检查 GitHub Actions Node 20 deprecation warning，升级 action versions。

### Out Of Scope

- 不依赖本地 Docker。
- 不要求真实 NAS 环境。

### Acceptance Criteria

- PR/push 时 tests 自动运行。
- Docker 镜像只在 tests 通过后发布。
- Actions 不再使用明显过期版本。
- README 说明如何 tag 发布版本。

### Suggested Prompt

```text
Implement Agent H from docs/agent-taskbook.md. Add CI tests, make Docker publishing depend on test success, update action versions if needed, and document release tagging. Do not require local Docker. Verify workflows syntactically.
```

## Cross-Agent Rules

- 不要改动无关文件。
- 不要提交 secrets、tokens、`config.yaml`、`data/`、`exports/`。
- 默认保留 SQLite backend。
- 不要把 Nocturne 设为默认路径。
- 所有 destructive tools 必须要求 `user_confirmed=true`。
- 所有科研 claim 仍必须遵守 evidence policy。
- 每个 agent 完成后必须运行 `pytest`；如果无法运行，说明原因。
- 每个 agent 应提交一个清晰 commit，例如 `Add retrieval health diagnostics`。

## Suggested Agent Manager Setup

建议创建 8 个独立 worktree：

- `agent-a-hybrid-retrieval`
- `agent-b-backfill-cli`
- `agent-c-auth-health`
- `agent-d-memory-management`
- `agent-e-migrations-integrity`
- `agent-f-docs-client-config`
- `agent-g-nocturne-adr`
- `agent-h-ci-release`

如果只想先做第一批，优先启动 A、B、C、F、H。
