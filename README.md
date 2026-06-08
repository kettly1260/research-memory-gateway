# Research Memory Gateway

面向科研工作的轻量 MCP 记忆网关，用来在 Codex、Cherry Studio、KiloCode 等 AI 工具之间共享长期科研记忆，同时避免上下文溢出、重复会话、摘要幻觉和本地资源过度消耗。

这个项目不是重型 RAG，也不是完整替代 Nocturne Memory、Engram 或其它长期记忆系统。它的定位是一个“科研记忆网关”：负责保存提醒、用户确认、强证据 schema、source_refs 回溯、重复检测、轻知识图谱和导出；底层可以先用内置 SQLite，后续再接 Nocturne Memory 等远程记忆底座。

## 设计目标

- 跨 Codex、Cherry Studio、KiloCode 等工具保存和检索科研结论。
- 只在出现可复用科研资产时提醒保存，避免长期记忆变成聊天垃圾桶。
- 写入长期记忆必须经过用户确认。
- 摘要只作为检索入口，科研结论必须写入 `claims` 并尽量绑定 `evidence`。
- 每条记忆保存 `source_refs`，可回溯原会话、论文、文件、DOI 或 URL。
- 使用轻图谱字段 `entities` 和 `relations`，支持材料、论文、合成路线、实验条件和机理假设之间的关系检索。
- 支持 Markdown 和 JSON 导出，便于 Obsidian、备份和迁移。
- 默认本地 SQLite 可直接验证，NAS/VPS 部署时可通过 SSE 暴露给 AI 客户端。

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
SQLite 默认后端，或后续接 Nocturne Memory on NAS
```

## MCP 工具

| 工具 | 作用 |
|---|---|
| `propose_save` | 生成保存建议，不直接写入长期记忆。 |
| `save_research_memory` | 用户确认后保存记忆，要求 `user_confirmed=true`。 |
| `search_research_memory` | 按关键词、项目、记忆类型检索科研记忆。 |
| `check_overlap` | 检查当前内容是否和已有记忆重复、相似或冲突。 |
| `open_source_ref` | 根据白名单 source ref 回溯本地文件、会话、DOI 或 URL。 |
| `audit_unverified` | 找出缺证据、未验证或推断型 claim。 |
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

SSE 模式：

```powershell
research-memory-gateway --config config.yaml --transport sse --host 0.0.0.0 --port 8787
```

客户端访问地址通常是：

```text
http://<nas-ip>:8787/sse
```

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

把 `docker-compose.ghcr.yml` 复制到 NAS，并修改镜像名：

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

后续如需接 Nocturne Memory，可切换为：

```yaml
backend:
  type: nocturne
  nocturne_url_env: NOCTURNE_URL
  nocturne_token_env: NOCTURNE_TOKEN
```

当前 Nocturne 适配器保留了边界，但还没有绑定具体部署的 Nocturne MCP/HTTP 契约。建议先用 SQLite 验证保存、检索、审计和导出流程，再根据 NAS 上 Nocturne 的实际接口做映射。

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

已覆盖：

- `evidence_backed` claim 必须绑定 evidence。
- `unverified` claim 可无 evidence。
- SQLite 检索支持 `sulfur-doped`、`Hg2+` 等含连字符或符号的科研术语。

## 目录结构

```text
src/research_memory_gateway/  # 网关源码
prompts/                      # AI 客户端系统提示
skills/                       # 可复制到 Kilo/Codex/Cherry 的调用策略
docs/                         # 部署、客户端配置、schema 文档
examples/                     # 示例记忆
.github/workflows/            # GitHub Actions 镜像发布
```
