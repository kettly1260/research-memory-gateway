# Research Memory Gateway

面向科研工作的轻量 MCP 记忆网关，用来在 Codex、Cherry Studio、KiloCode 等 AI 工具之间共享长期科研记忆，同时避免上下文溢出、重复会话、摘要幻觉和本地资源过度消耗。

这个项目不是重型 RAG，也不是完整替代 Nocturne Memory、Engram 或其它长期记忆系统。它的定位是一个“自包含科研记忆 MCP 服务 + 可选向量/重排检索增强”：默认用内置 SQLite 保存和关键词检索记忆，Docker 部署后 AI 工具可以直接调用；如果你已经部署了 embedding 模型和 rerank 模型，可以把它们接进来做混合检索。

## 重要说明：当前不需要 Nocturne

本项目 Docker 镜像只部署 `research-memory-gateway` 本身，不部署 Nocturne。

默认模式已经可以满足调用：

```text
AI 客户端 -> research-memory-gateway MCP -> SQLite 数据库
```

也就是说，不接 Nocturne 时，仍然可以正常使用：

- `propose_save` 生成保存建议。
- `save_research_memory` 写入 SQLite。
- `search_research_memory` 从 SQLite 检索。
- `check_overlap` 检查重复/相似记忆。
- `audit_unverified` 审计未验证结论。
- `export_memories` 导出 Markdown/JSON。

如果你已经部署了向量模型和重排模型，推荐模式是：

```text
AI 客户端 -> research-memory-gateway MCP -> SQLite 数据库
                                      |-> embedding 模型（可选）
                                      |-> rerank 模型（可选）
```

因此当前推荐直接用 SQLite 自包含模式上线，再按需开启 embedding/rerank 混合检索。Nocturne 不在当前部署路径内。

## 设计目标

- 跨 Codex、Cherry Studio、KiloCode 等工具保存和检索科研结论。
- 只在出现可复用科研资产时提醒保存，避免长期记忆变成聊天垃圾桶。
- 写入长期记忆必须经过用户确认。
- 摘要只作为检索入口，科研结论必须写入 `claims` 并尽量绑定 `evidence`。
- 每条记忆保存 `source_refs`，可回溯原会话、论文、文件、DOI 或 URL。
- 使用轻图谱字段 `entities` 和 `relations`，支持材料、论文、合成路线、实验条件和机理假设之间的关系检索。
- 支持 Markdown 和 JSON 导出，便于 Obsidian、备份和迁移。
- 默认 SQLite 后端可直接在 NAS/VPS 上长期运行，并通过 SSE 暴露给 AI 客户端。

## 推荐架构

```text
Codex / Cherry Studio / KiloCode / 其它 MCP 客户端
        |
        |  优先 SSE / Streamable HTTP
        |  不支持远程 MCP 时使用本地 stdio 代理
        v
Research Memory Gateway MCP
        |
        |  schema 校验 / source_refs / 去重 / 审计 / 导出 / 轻图谱
        v
SQLite 默认后端
        |
        |  可选：embedding 向量召回 + rerank 重排
        v
已部署的向量模型 / 重排模型
```

## MCP 工具

| 工具 | 作用 |
|---|---|
| `propose_save` | 生成保存建议，不直接写入长期记忆。 |
| `save_research_memory` | 用户确认后保存记忆，要求 `user_confirmed=true`。 |
| `search_research_memory` | 按关键词、项目、记忆类型检索科研记忆。 |
| `check_overlap` | 检查当前内容是否和已有记忆重复、相似或冲突。 |
| `get_research_memory` | 按 `memory_id` 读取单条记忆。 |
| `update_research_memory` | 用户确认后更新记忆，并刷新 SQLite FTS/embedding。 |
| `delete_research_memory` | 用户确认后删除记忆、FTS 和向量。 |
| `mark_memory_status` | 用户确认后标记 `superseded`、`retracted` 或 `conflicting`。 |
| `merge_research_memories` | 合并多条记忆并把旧记忆标记为 superseded。 |
| `open_source_ref` | 根据白名单 source ref 回溯本地文件、会话、DOI 或 URL。 |
| `audit_unverified` | 找出缺证据、未验证或推断型 claim。 |
| `health` | 报告服务、SQLite、retrieval 和记忆策略状态。 |
| `retrieval_health` | 报告 SQLite、embedding、rerank、向量维度和最近降级原因。 |
| `audit_database_integrity` | 检查/修复 FTS 缺失、orphan embeddings 和 JSON/source_refs 问题。 |
| `export_memories` | 导出 Markdown 和 JSON。 |

## 记忆类型

第一版固定 8 类，避免标签漂移：

- `literature_review`：文献综述、方向调研、技术路线比较。
- `paper_note`：单篇或少量论文的结构化笔记。
- `synthesis_route`：合成路线、反应条件、试剂、风险和替代路线。
- `experiment_plan`：实验设计、对照组、表征方案和预期结果。
- `mechanism_hypothesis`：机理假设、证伪条件、支持或冲突证据。
- `material_system`：材料体系、组成、结构、性能和适用场景。
- `presentation_outline`：PPT、报告、组会、答辩结构。
- `research_decision`：影响后续工作的关键研究决策。

## 防幻觉规则

长期记忆采用强证据 schema。核心原则是：`summary` 只是检索入口，不是科研事实本身。

科研结论应写入 `claims`：

```json
{
  "claim": "硫掺杂碳点可能通过软酸软碱相互作用提高 Hg2+ 亲和力。",
  "confidence": "medium",
  "verification_status": "evidence_backed",
  "evidence_ids": ["ev_demo_001"]
}
```

如果没有证据，必须标记为：

```text
unverified
```

支持的验证状态：

- `evidence_backed`：有证据支撑。
- `inferred`：由证据推断，但原文未直接证明。
- `unverified`：尚无证据。
- `conflicting`：与已有记忆或来源冲突。
- `superseded`：已被更新证据或决策取代。
- `retracted`：不应继续使用。

## 本地快速启动

```powershell
cd G:\LLM\memory
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
Copy-Item config.example.yaml config.yaml
research-memory-gateway --config config.yaml --transport stdio
```

Streamable HTTP 模式适合远程 MCP 客户端，也是 Docker 镜像默认启动方式：

```powershell
research-memory-gateway --config config.yaml --transport streamable-http --host 0.0.0.0 --port 8787
```

客户端访问地址通常是：

```text
http://<nas-ip>:8787/mcp
```

如果客户端只支持 legacy SSE，可以显式启动 `--transport sse` 或 `--transport both`，然后使用 `http://<nas-ip>:8787/sse`。

HTTP/SSE 安全规则：设置 `RESEARCH_MEMORY_TOKEN` 后所有远程 MCP 请求必须携带 `Authorization: Bearer <token>`。未设置该环境变量时，只允许来自 `127.0.0.1`、`::1` 的本机 HTTP/SSE 请求免 token；非本机请求仍会被拒绝，除非使用 WebUI 创建的 active API key。

## Docker 本地构建

```powershell
cd G:\LLM\memory
Copy-Item config.example.yaml config.yaml
docker compose up -d --build
```

## 推送到 GitHub 并发布镜像

项目已包含 GitHub Actions workflow：`.github/workflows/docker-publish.yml`。

推送到 GitHub 后，每次推送到 `main` 或创建 tag 时，会自动构建并推送镜像到 GitHub Container Registry：

```text
ghcr.io/<你的GitHub用户名或组织名>/research-memory-gateway:latest
ghcr.io/<你的GitHub用户名或组织名>/research-memory-gateway:<tag>
```

推荐仓库名保持为：

```text
research-memory-gateway
```

首次发布步骤：

```powershell
git init
git add .
git commit -m "initial research memory gateway"
git branch -M main
git remote add origin https://github.com/<你的用户名>/research-memory-gateway.git
git push -u origin main
```

如果要发布版本镜像：

```powershell
git tag v0.1.0
git push origin v0.1.0
```

## NAS 直接拉取镜像

如果使用当前仓库的公开镜像，可直接把 `docker-compose.nas.yml` 复制到 NAS：

```bash
cp config.example.yaml config.yaml
docker compose -f docker-compose.nas.yml pull
docker compose -f docker-compose.nas.yml up -d
```

这个 compose 只部署 `research-memory-gateway` 和 SQLite 自包含后端，不包含 Nocturne。embedding/rerank 是外部服务，通过环境变量连接。

如果你 fork 了仓库或换了 GitHub 用户名，则使用 `docker-compose.ghcr.yml`，并修改镜像名：

```yaml
image: ghcr.io/<你的GitHub用户名或组织名>/research-memory-gateway:latest
```

然后运行：

```bash
cp config.example.yaml config.yaml
docker compose -f docker-compose.ghcr.yml pull
docker compose -f docker-compose.ghcr.yml up -d
```

以后更新只需要：

```bash
docker compose -f docker-compose.ghcr.yml pull
docker compose -f docker-compose.ghcr.yml up -d
```

## 配置文件

复制配置模板：

```powershell
Copy-Item config.example.yaml config.yaml
```

默认后端是 SQLite：

```yaml
backend:
  type: sqlite
  sqlite_path: ./data/research_memory.db
```

## 可选 WebUI 管理台

WebUI 是单管理员私有管理台，用于管理记忆、运行时 API 配置、检索状态、JSON 导入导出和受控 embedding backfill。它不替代 MCP；AI 客户端仍通过 MCP tools 调用服务。

默认关闭：

```yaml
webui:
  enabled: false
  host: 127.0.0.1
  port: 8788
```

首次开启时必须提供初始密码或 `WEBUI_PASSWORD_HASH`：

```yaml
webui:
  enabled: true
  initial_password: "change-this-immediately"
```

首次启动会把密码 hash 写入 `./data/webui-auth.json`。确认登录成功后，应从 `config.yaml` 删除明文 `initial_password`，后续登录只使用 auth store。WebUI session 使用固定过期 HttpOnly cookie，不提供“记住我”。所有写操作需要 CSRF。

如果要在 WebUI 保存 embedding/rerank API key 或 Nocturne token，必须设置：

```text
WEBUI_SECRET_KEY=<long-random-secret>
```

密钥会加密写入 `./data/webui-secrets.json.enc`，不会写入 `web_config.yaml`，也不会在 API、HTML、日志或导出中明文返回。非密钥运行时配置写入 `./data/web_config.yaml`，例如 retrieval mode、base URL、model、timeout 和 retry。配置优先级为：密钥 `env > webui-secrets > unset`，非密钥运行时配置 `env > web_config.yaml > config.yaml > defaults`。

WebUI 默认端口为 `8788`。是否暴露给宿主机或公网由 Docker compose、NAS、防火墙或反向代理配置决定。建议只在本机、Tailscale/WireGuard/ZeroTier 或受认证反向代理后访问。

WebUI v1 的 Nocturne 页面只支持配置保存、token 加密保存和连接测试。不会执行 SQLite 同步、Nocturne 导入、双写或直接编辑 Nocturne 记忆。

Embedding backfill 会消耗模型 API 额度。建议先 dry-run，限制 batch/concurrency/timeout，并确认只有一个 backfill job 运行。Hard delete 仅允许在 deleted 详情页执行，且不清理历史备份、导出文件或 Nocturne 远端数据。

如果只用 SQLite FTS，保持默认：

```yaml
retrieval:
  mode: keyword
```

如果已经部署了向量模型和重排模型，可以开启混合检索：

```yaml
retrieval:
  mode: hybrid
  embedding:
    enabled: true
    endpoint_path: /embeddings
    timeout_seconds: 30
    max_retries: 1
  rerank:
    enabled: true
    endpoint_path: /rerank
    timeout_seconds: 30
    max_retries: 1
```

然后在 Docker Compose 环境变量里填入模型服务地址：

```yaml
environment:
  EMBEDDING_BASE_URL: "http://<embedding-host>:<port>/v1"
  EMBEDDING_MODEL: "你的向量模型名"
  RERANK_BASE_URL: "http://<rerank-host>:<port>/v1"
  RERANK_MODEL: "你的重排模型名"
```

接口默认兼容 OpenAI 风格 embedding 响应：

```json
{"data": [{"embedding": [0.1, 0.2, 0.3]}]}
```

也支持直接返回：

```json
{"embedding": [0.1, 0.2, 0.3]}
```

`EMBEDDING_BASE_URL` 可以填服务根路径或 `/v1` 路径。默认 `endpoint_path: /embeddings`，如果服务根路径下的 `/embeddings` 返回 404，客户端会再尝试 `/v1/embeddings`。

重排接口默认请求格式为：

```json
{"query": "检索问题", "documents": ["候选1", "候选2"], "top_n": 10, "model": "模型名"}
```

响应支持：

```json
{"results": [{"index": 0, "relevance_score": 0.98}]}
```

也支持：

```json
{"results": [{"document": {"index": 0}, "score": 0.98}]}
```

以及：

```json
{"data": [{"index": 0, "score": 0.98}]}
```

如果 embedding 服务不可用，搜索会自动退回 SQLite FTS。保存记忆时如果 embedding 失败，记忆仍会保存到 SQLite，并在 `audit_database_integrity` / `inspect-db` 中标记需要回填的记忆。如果 embedding 被关闭但已有向量的记忆被更新，也会标记为需要 backfill，避免旧向量与新文本不一致。如果 rerank 服务不可用，搜索会返回未重排的 hybrid 合并结果。所有降级都会写入日志，并可通过 `retrieval_health` 查看最近错误、HTTP 状态、已有向量数量和维度分布。

混合检索的分数含义：SQLite FTS 的 `bm25` 结果会按排序位置转换为合并分数，向量结果使用 cosine similarity，重排结果使用模型返回的 score。`match_reason` 会标出 `fts:rank_position=...`、`vector:cosine=...` 和 `rerank:score=...`。

已有记忆开启 embedding 后可以回填向量：

```powershell
research-memory-admin backfill-embeddings --config config.yaml --dry-run
research-memory-admin backfill-embeddings --config config.yaml
```

数据库维护命令：

```powershell
research-memory-admin inspect-db --config config.yaml
research-memory-admin audit-integrity --config config.yaml --repair-fts --repair-orphan-embeddings
```

更多客户端矩阵、NAS 容器内回填、模型模板和安全规范见 `docs/operations.md`。

后续如需接 Nocturne Memory，需要先单独部署 Nocturne，并把本项目的 Nocturne 适配器映射到你的 Nocturne MCP/HTTP 契约。配置上可预留为：

```yaml
backend:
  type: nocturne
  nocturne_url_env: NOCTURNE_URL
  nocturne_token_env: NOCTURNE_TOKEN
```

当前 Nocturne 适配器只保留边界，还没有绑定具体部署的 Nocturne MCP/HTTP 契约。原因是 Nocturne 可通过 stdio、SSE、Streamable HTTP 或 Nginx 反代暴露，不同部署的调用契约不同。建议先用 SQLite 自包含模式验证保存、检索、审计和导出流程，再根据 NAS 上 Nocturne 的实际接口做映射。

## 是否需要 Nocturne

第一版不需要 Nocturne。SQLite 模式已经是完整 MCP 记忆服务。

建议选择：

| 场景 | 推荐 |
|---|---|
| 单人科研、多工具共享、NAS 部署 | 直接用 SQLite 模式 |
| 想要 Nocturne Dashboard、PostgreSQL、复杂审计 | 单独部署 Nocturne 后再接入 |
| 只是想让 Codex/Cherry/KiloCode 能保存和检索长期科研记忆 | 不需要 Nocturne |
| 想做完整人格/长期 agent memory 系统 | 可考虑 Nocturne |

## 安全建议

- NAS 优先通过 Tailscale、ZeroTier 或 WireGuard 访问。
- 即使在内网，也建议设置 `RESEARCH_MEMORY_TOKEN`。
- 如果通过 VPS + Nginx 暴露公网，必须使用 HTTPS 和鉴权。
- 写入长期记忆必须用户确认。
- 删除、覆盖、批量导入应二次确认。
- 未发表课题、实验路线和投稿内容不要导出到公开仓库。
- `data/`、`exports/`、`config.yaml` 默认已加入 `.gitignore`。

## 客户端提示词

把下面文件内容加入 Codex、Cherry Studio、KiloCode 或其它 AI 客户端的系统提示中：

```text
prompts/research-memory-system-prompt.md
```

它会约束 AI：只有产生可复用科研资产时才调用 `propose_save`，并且必须等待用户确认后才能调用 `save_research_memory`。

## 推荐安装 Skill

项目内置了一个可移植 skill：

```text
skills/research-memory-gateway/SKILL.md
```

它的作用是让 agent 更稳定地调用 MCP：什么时候该保存、先查重还是先提议、如何区分摘要和证据、何时调用 `open_source_ref`、何时审计未验证结论。

Kilo 全局安装示例：

```powershell
New-Item -ItemType Directory -Force "$env:USERPROFILE\.config\kilo\skills\research-memory-gateway"
Copy-Item "G:\LLM\memory\skills\research-memory-gateway\SKILL.md" "$env:USERPROFILE\.config\kilo\skills\research-memory-gateway\SKILL.md"
```

也可以把该 `SKILL.md` 的正文复制到 Codex、Cherry Studio 或其它客户端的系统提示中使用。MCP 提供工具能力，skill 提供调用策略；两者一起使用效果最好。

## 测试

```powershell
pytest
```

轻量 smoke 验证：

```powershell
./scripts/smoke.ps1
```

已覆盖：

- `evidence_backed` claim 必须绑定 evidence。
- `unverified` claim 可无 evidence。
- SQLite 检索支持 `sulfur-doped`、`Hg2+` 等含连字符或符号的科研术语。

## 发布版本

GitHub Actions 会在 PR/push 时运行 `pytest`。Docker 镜像发布 workflow 也会先运行测试，只有测试通过才 build/push GHCR 镜像。

发布 tag 示例：

```powershell
git tag v0.1.0
git push origin v0.1.0
```

tag 触发后会发布 `ghcr.io/<owner>/<repo>:v0.1.0` 和对应 sha 标签；默认分支会额外发布 `latest`。

## 目录结构

```text
src/research_memory_gateway/  # 网关源码
prompts/                      # AI 客户端系统提示
skills/                       # 可复制到 Kilo/Codex/Cherry 的调用策略
docs/                         # 部署、客户端配置、schema 文档
examples/                     # 示例记忆
.github/workflows/            # GitHub Actions 镜像发布
```
