# Conversation Memory WebUI + GitHub/GHCR Release Taskbook

任务日期：2026-09-12  
执行项目：`G:\LLM\memory`  
目标版本：`v0.2.2`（`v0.2.0` 因 MCP 2.x 无上限约束导致 CI collection 失败；`v0.2.1` 修复依赖并通过 CI/GHCR，但 NAS 原生实写暴露 Linux 255-byte 文件名限制；`v0.2.2` 追加 UTF-8 字节预算文件名与 manifest fail-closed 修复）
执行性质：**Conversation Memory 使用层补齐 + 正式发布**；不是继续做 322-session 数据 rollout。

---

## 0. 本任务的核心目标

当前 Conversation Memory 数据层和 MCP 使用层已具备：

```text
conversation_search
conversation_read
conversation_recall
```

322-session lexical staging 已全量验证；生产侧已完成 35 场 canonical + BGE-M3 pilot。

本任务不继续 Batch 3/4/5。

本任务只做两件事：

1. 给现有 WebUI 增加 **只读 Conversation Memory 控制台**。
2. 将当前已验收的 Conversation Memory/MCP/WebUI 代码整理、测试、发布到 GitHub + GHCR，供 NAS 拉取。

最终目标：

```text
GitHub main
  -> tag v0.2.x
  -> GitHub Actions
  -> ghcr.io/kettly1260/research-memory-gateway:v0.2.x
  -> NAS pull
  -> MCP + WebUI 实际使用验收
```

---

## 1. 立即冻结 bulk rollout

在本任务完成前：

- **禁止启动 Batch 3（50 sessions）**。
- 禁止继续导入剩余 287 场 canonical conversation。
- 禁止继续 bulk BGE-M3 embedding。
- 已完成的 35 场 production pilot 保持不动。

原因：此时更需要验证 MCP/WebUI 的真实使用价值，而不是继续扩大数据迁移规模。

---

## 2. 当前已知发布状态

远端：

```text
origin = https://github.com/kettly1260/research-memory-gateway.git
```

当前本地分支：

```text
v2/p0-hardening
```

当前旧基线 HEAD：

```text
ba50e6296fb871468d868eda405c6855bfee3353
```

当前 `pyproject.toml` version：

```text
0.1.0
```

Conversation Memory / MCP / Keep-Alive / rollout 相关新代码目前仍有大量未提交改动，因此 NAS 现在直接 `git pull` 或拉旧 GHCR 镜像不能得到最新版。

---

## 3. 发布安全边界

本任务**明确授权**执行 Agent：

- 创建 release 分支。
- 修改代码、测试、README、通用配置示例、WebUI frontend/static dist。
- 更新 `pyproject.toml` version。
- `git commit`。
- `git push` 到 GitHub。
- 在确认 main 合入后创建并 push `v0.2.x` tag。

但严格禁止提交：

```text
config.yaml
config.rollout.yaml
config.conversation-production.yaml
data/
exports/
scratch/
*.sqlite / *.db
原始 conversation ZIP
Research-AI-Hub/Vault 内容
任何 token / password / API key / secret
```

不得：

- force push。
- 重写远端历史。
- 自动删除现有 dirty 工作树中的用户文件。
- 修改/重启 Unraid，除非进入 NAS 部署阶段且用户明确授权。

---

## 4. W0：冻结基线与发布清单

执行并记录：

```powershell
git remote -v
git branch --show-current
git rev-parse HEAD
git status --short
git fetch origin --tags
git log --oneline --decorate -n 20
python -m pytest -q
git diff --check
```

Frontend：

```powershell
cd src/research_memory_gateway/webui/frontend
npm ci
npm run lint
npm run build
```

若现有 frontend lockfile 不支持 `npm ci`，说明原因后使用仓库已有可复现安装方式；不要无理由更新全部依赖。

基线失败则停止发布，不继续 Git 操作。

---

## 5. W1：WebUI Backend Conversation API

### 5.1 只读原则

第一版 WebUI Conversation 页面只提供：

- 状态查看。
- 搜索。
- Recall preview。
- Conversation note / heading 读取。

**不得提供**：

- 一键全量 import。
- 一键 bulk embedding。
- 删除 conversation。
- 修改 canonical Markdown。
- 修改 manifest。

这些生产操作继续由 CLI + 显式确认门控负责。

### 5.2 新增 API

建议在 `src/research_memory_gateway/webui/app.py` 增加：

```text
GET /admin/api/conversations/status
GET /admin/api/conversations/search
GET /admin/api/conversations/recall
GET /admin/api/conversations/read
```

#### `GET /admin/api/conversations/status`

返回至少：

```json
{
  "enabled": true,
  "documents": 35,
  "sections": 1748,
  "embeddings": 1553,
  "sections_with_embedding": 1748,
  "sections_without_embedding": 0,
  "vector_coverage": 1.0,
  "embedding_model": "bge-m3",
  "embedding_version": "v1",
  "embedding_dimension": 1024
}
```

数字示例不可硬编码，必须从当前 index DB / active metadata 实时读取。

如果 embedding disabled：

- status 仍可正常返回 documents/sections。
- embedding 字段明确 disabled/empty。
- 不得报 500。

如果 `conversation_archive.enabled=false`：

返回明确可识别状态，例如：

```text
404/409 conversation_archive_disabled
```

不要让 WebUI 因该功能关闭而整体失败。

#### `GET /admin/api/conversations/search`

参数：

```text
query
project?
conversation_id?
parent_thread_id?
limit?
```

直接复用：

```text
service.conversation_retrieval.search(...)
```

不要复制一套 WebUI 专用 ranking 逻辑。

#### `GET /admin/api/conversations/recall`

参数：

```text
query
token_budget=1500
project?
conversation_id?
parent_thread_id?
```

直接复用：

```text
service.conversation_retrieval.recall(...)
```

WebUI 展示的 preview 必须和 MCP `conversation_recall` 使用同一返回逻辑。

#### `GET /admin/api/conversations/read`

参数：

```text
file_path
heading?
```

直接复用：

```text
service.conversation_retrieval.read(...)
```

必须继续由 `allowed_roots` 做路径安全验证。

禁止 WebUI API 自己直接 `Path(file_path).read_text()` 绕过安全检查。

### 5.3 Index stats helper

如果当前 `ConversationIndexDatabase` 没有合适的只读统计接口，可增加一个小型：

```python
stats() -> dict[str, Any]
```

只读查询：

- `conversation_documents`
- `conversation_sections`
- `conversation_embeddings`
- active live embedding identity coverage

禁止为了 WebUI stats 改 schema。

---

## 6. W2：Frontend Conversations 页面

### 6.1 新路由与导航

新增：

```text
/admin/conversations
```

源码路由：

```text
/conversations
```

在：

```text
frontend/src/router.tsx
frontend/src/components/layout/navigation.ts
```

加入 `Conversations` 菜单。

建议图标：

```text
MessagesSquare / MessageSquareText
```

不要复用 `Database` 以免与 Research Memories 混淆。

### 6.2 页面结构

新增：

```text
frontend/src/pages/Conversations.tsx
```

第一版分四块。

#### A. Status cards

展示：

```text
Documents
Sections
Embeddings
Vector coverage
Active model
Version
Dimension
```

embedding disabled 时显示 `Lexical only`，不要显示红色错误。

#### B. Search

输入框支持真实查询：

```text
B36
Fe3+
TOC_Focused.png
错误码 / 文件名 / URL / UUID
```

结果至少显示：

```text
title
conversation_id
heading
date
score / score_type
parent_thread_id
thread_source
source anchors
excerpt
```

### 6.2.1 来源身份必须是一等字段（Release Blocker）

当前 35-session pilot 已暴露一个实际可用性问题：WebUI 虽然在结果底部显示
`thread_source`，但用户无法一眼判断“这是 Codex / 其他 Agent / 哪种客户端产生的会话”。

这不是单纯 UI 文案问题。Codex 原始 `session_meta` 已包含比当前 index/UI 更丰富的来源信息，例如：

```text
originator      = Codex Desktop
source          = vscode
thread_source   = user / subagent / guardian_review
model_provider  = custom
cli_version     = 0.153.4
base_instructions.provenance.model = gpt-5.6-sol
```

而当前 Markdown 仅稳定保存 `source: codex`、`thread_source`、`parent_thread_id` 等；
conversation index 目前也只把 `thread_source` / `parent_thread_id` 作为一等字段。

在 v0.2.x 发布前必须补齐一个**通用 Source Identity 层**，避免未来接入 ChatGPT / Claude /
Gemini / Cursor 等来源后全部混在一起。

建议 canonical 字段（命名可小幅调整，但语义必须保持）：

```text
source_system      codex / chatgpt / claude / gemini / cursor / unknown
source_originator  Codex Desktop / ...
source_surface     vscode / desktop / web / cli / api / ...
source_version     0.153.4 / ...
model_provider     custom / openai / anthropic / google / ...
model_name         gpt-5.6-sol / ...
thread_source      user / subagent / guardian_review / ...
parent_thread_id
agent_path
```

其中：

- `source_system` 是用户最关心的“这段历史来自哪个 Agent/平台”。
- `thread_source` 只描述该平台内部的线程角色，**不能拿它代替来源平台**。
- 对现有 Codex export，`source_system` 必须稳定为 `codex`。
- `thread_source=user` 在 UI 中不要直接翻译成“用户”，应显示为“主会话 / Root session”；
  `subagent` 显示“子 Agent”；`guardian_review` 显示“Guardian Review”。

实现要求：

1. `NormalizedConversation` 提供上述可恢复 source identity 属性。
2. `ObsidianConversationWriter` 将可恢复字段写入 machine-managed frontmatter。
3. `ConversationIndexDatabase` 用 additive migration 增加必要字段；不得破坏现有 DB。
4. `SearchResult` / WebUI API 返回来源身份字段。
5. Search 结果标题附近必须显示醒目的来源 badge，例如：

```text
[CODEX] [主会话]
[CODEX] [子 Agent] Parent: 01a08c1c...
[CODEX] [Guardian Review]
```

而不是只在卡片最底部显示 `(user)` / `(subagent)`。

6. Search 至少增加：

```text
source_system filter
thread_source filter
```

可选增加：

```text
model_name filter
source_originator filter
```

7. Conversation Detail 顶部显示完整来源元数据；不要要求用户打开 Markdown frontmatter 才能判断来源。
8. Status 区至少返回并展示：

```text
source_system_distribution
thread_source_distribution
```

例如当前 35-session pilot 的 thread role 应能明确看出主会话 / subagent / guardian 的数量，
而来源平台应明确标识为 Codex。

9. Recall item 明细同样显示来源平台 + thread role，防止用户把 guardian/subagent 内容误认为主会话。

### 6.2.2 现有 35 场的兼容迁移

当前已写入的 Markdown frontmatter 已有：

```text
source: codex
thread_source
parent_thread_id
```

因此至少可以在不读取 raw ZIP 的情况下，为旧 note 恢复：

```text
source_system = codex
thread_source
parent_thread_id
```

更丰富的 `originator/source_surface/source_version/model_name` 若旧 Markdown 未保存，不得猜测。
Windows pilot 可通过同一 immutable raw ZIP 做一次受控 metadata refresh；NAS 自包含重建时则直接从 raw ZIP
按新 parser/writer 生成完整来源身份。

禁止为了补来源字段把旧数据伪装成已知值。

明确区分：

```text
Top result
Anchored provenance
```

不要把无 anchor 的 top result 假装成 anchored result。

#### C. Recall Preview

输入：

```text
query
token_budget
```

展示：

```text
final context
context chars / budget chars
items
fallback_to_lexical
fallback_reason
```

该区域的目的就是让用户肉眼看到 MCP `conversation_recall` 实际会塞给 Agent 什么。

#### D. Conversation Detail / Reader

点击 Search result 后：

- 使用 `/conversations/read`。
- 展示完整 note 或指定 heading。
- 显示 file path，但不提供任意路径编辑。
- source anchor 以可读 JSON/表格方式显示。

可以用右侧 Drawer/Sheet 或页面下方 Detail panel，不需要新建复杂富文本编辑器。

### 6.3 API client 与 types

在：

```text
frontend/src/lib/api.ts
frontend/src/types/api.ts
```

增加强类型接口。

不要在页面里散落 raw `fetch()`。

### 6.4 i18n

同步更新：

```text
frontend/src/i18n/zh-CN.json
frontend/src/i18n/en.json
```

至少新增：

```text
nav.conversations
conversations.title
conversations.search
conversations.recallPreview
conversations.status
conversations.vectorCoverage
conversations.lexicalOnly
```

---

## 7. W3：WebUI 自动化测试

### 7.1 Backend tests

扩充 `tests/test_webui.py` 或新建：

```text
tests/test_webui_conversations.py
```

使用 `tmp_path` 创建小型 conversation Markdown/index，不读取用户真实 Vault。

最低覆盖：

1. disabled archive -> 明确状态，不 500。
2. status documents/sections 计数正确。
3. embedding disabled status 正常。
4. embedding enabled coverage/model/version/dim 正确。
5. search 返回 expected conversation。
6. search `conversation_id` filter。
7. search `parent_thread_id` filter。
8. recall 最终 context budget。
9. read allowed path PASS。
10. read outside allowed root FAIL。
11. unauthenticated API -> 401。
12. Codex note/search result 返回 `source_system=codex`。
13. `thread_source=user` 在 API 保持机器值 `user`，前端显示“主会话 / Root session”。
14. subagent/guardian_review 的 parent lineage 和来源 badge 正确。
15. `source_system` filter 只返回指定来源。
16. additive index migration 不破坏旧 conversation index。

### 7.2 MCP regression

继续跑：

```text
tests/test_mcp_http_integration.py
tests/test_conversation_*.py
```

确认 WebUI 接入没有改变：

```text
conversation_search
conversation_read
conversation_recall
```

的 MCP schema/行为。

### 7.3 Frontend

必须：

```powershell
npm run lint
npm run build
```

如果项目已有前端测试框架则补页面测试；如果目前没有，不为本任务额外引入大型 E2E 框架。

至少通过 TypeScript build 保证 API types/route 编译正确。

### 7.4 全库

最终：

```powershell
python -m pytest -q
git diff --check
```

0 failed / 0 error。

---

## 8. W4：本地真实 35-session WebUI smoke

只读 smoke；**不继续导入新 session**。

使用当前已经 production 化的 35 场，至少验证：

```text
status 页面能显示真实 documents/sections/coverage
Search: Fe3+
Search: TOC_Focused.png
Search: B36
Search: 一个 parent_thread UUID
Recall: token_budget=100
Recall: token_budget=1500
点击 result -> read note
```

记录：

```text
query
top result
anchored provenance
context budget
reader path validation
```

不得在 smoke 中编辑 Vault note。

---

## 9. W5：Release Hygiene

### 9.1 Git ignore

当前 `.gitignore` 已忽略 `config.yaml/data/exports`，但发布前必须确认以下不会被 staged：

```text
config.rollout.yaml
config.conversation-production.yaml
scratch/
```

如未忽略，补充 `.gitignore`：

```gitignore
config.rollout.yaml
config.conversation-production.yaml
scratch/
```

不要忽略 `config.example.yaml`。

### 9.2 Version

将：

```toml
version = "0.1.0"
```

升级到：

```toml
version = "0.2.0"
```

若远端已有 `v0.2.0`，使用下一个未占用的 `v0.2.x`。

### 9.3 README

至少补充：

- Conversation Memory MCP 三工具说明。
- `conversation_archive.enabled` 示例。
- WebUI `/admin/conversations` 说明。
- WebUI 默认仍为可选启用。
- NAS Conversation 数据/索引挂载说明。
- 不建议通过 WebUI bulk import/embedding。

### 9.4 Static dist

执行 frontend build 后，确认：

```text
src/research_memory_gateway/webui/static/dist/
```

包含新版 Conversations chunk / index assets。

如果仓库当前跟踪 static dist，则提交构建产物；保持与现有项目发布方式一致。

---

## 10. W6：Git 发布流程

### 10.1 创建 release 分支

建议：

```text
release/conversation-memory-webui-v0.2.0
```

不要丢失当前 dirty changes。

### 10.2 Stage 白名单

提交前用：

```powershell
git status --short
git diff --cached --name-only
```

人工检查 staged files。

允许范围主要为：

```text
src/research_memory_gateway/**
tests/**
scripts/conversation_cli.py
scripts/smoke_bge_m3.py
README.md
config.example.yaml
pyproject.toml
.gitignore
.github/workflows/**（仅确有必要）
docs 中正式架构/实施/rollout 文档
```

不得把整个工作树 `git add -A` 后不检查就 commit。

### 10.3 Commit

推荐一个清晰的 release commit，必要时拆成 2–3 个逻辑 commit：

```text
feat: add conversation memory retrieval and web console
```

### 10.4 Push / Merge

先：

```text
push release branch
```

然后比较 `origin/main`。

要求：

- 不 force push。
- 如果 main 已有新提交，先正常 rebase/merge 并重新测试。
- 如果仓库保护规则要求 PR，则创建 PR 合入 main。
- 如果允许直接合并，也必须保证最终 main 包含完整 release commit。

最终必须记录：

```text
origin/main SHA
release commit SHA
```

### 10.5 Tag

只有 main 合入并且 CI/test 通过后：

```text
v0.2.0
```

tag 必须落在 main 的 release commit 上。

push tag 后不得移动/重写 tag。

---

## 11. W7：GHCR 发布验收

当前 workflow：

```text
.github/workflows/docker-publish.yml
```

支持：

```text
main push
v* tag
linux/amd64
linux/arm64
```

发布后必须确认至少存在：

```text
ghcr.io/kettly1260/research-memory-gateway:v0.2.x
ghcr.io/kettly1260/research-memory-gateway:latest
```

并记录：

```text
GitHub Actions run status
image tag
image digest（如可获得）
architectures
```

如果 GitHub Actions/GHCR 未成功，不得声称“已发布”。

---

## 12. W8：NAS 部署前的数据路径门控

这是本任务的关键部署注意事项。

**GitHub/GHCR 只发布代码和镜像，不会自动迁移 Windows 上的 Conversation 数据。**

Windows 当前 canonical archive 位于：

```text
D:\Partition\F\Study\博士文件\Research-AI-Hub\Vault\90_System\AI-Memory
```

生产 conversation index 当前也是独立数据文件。

NAS 上 MCP 要真正使用：

```text
conversation_search
conversation_read
conversation_recall
```

则 NAS 容器必须同时可见：

1. conversation Markdown archive。
2. conversation index SQLite。
3. 正确的 NAS/Linux 路径版配置。

Windows `D:\...` 路径不能直接写进 NAS 容器配置。

因此在 NAS `docker compose pull` 之前必须明确选一种部署架构。

### 方案 A：NAS 保存 Conversation 数据副本（推荐用于稳定服务）

例如：

```text
/mnt/user/appdata/research-memory-gateway/conversations
/mnt/user/appdata/research-memory-gateway/data/conversation-index.sqlite
```

将需要的 canonical Markdown 同步/复制到 NAS，只读挂载 archive；index 放在持久化 data volume。

优点：

- MCP 不依赖 Windows 电脑在线。
- NAS 服务稳定。
- Docker 路径明确。

### 方案 B：NAS 挂载同步后的 Research-AI-Hub/Vault

如果用户已经有可靠的 NAS-side Vault mirror/sync，可把 NAS 上的 Vault 路径只读 bind mount 给容器。

必须验证：

- 路径实时存在。
- 同步不会产生部分写文件。
- container 内 `conversation_read` 能读。
- index 的 `vault_path` 与 NAS 路径语义一致；如果 index 记录的是 Windows absolute path，需要重建/迁移 index，不能直接复制后假定可用。

### 禁止

- 在 Linux/NAS 配置中继续使用 `D:/Partition/...`。
- 为了让旧 absolute path 工作而做不安全的任意路径映射。
- 把整个 Research-AI-Hub 以可写方式暴露给 Gateway，除非有明确需求。

---

## 13. W9：NAS Compose / Config 示例

不要把真实生产 secret 提交 GitHub。

README/config example 可加入 NAS 示例，如：

```yaml
conversation_archive:
  enabled: true
  vault_root: /conversation-vault
  canonical_subdir: 90_System/AI-Memory
  index_path: /app/data/conversation-index.sqlite

retrieval:
  mode: hybrid
  embedding:
    enabled: true
    model: bge-m3
```

Compose 示意：

```yaml
volumes:
  - ./config.yaml:/app/config.yaml:ro
  - ./data:/app/data
  - /NAS/PATH/TO/Vault:/conversation-vault:ro
```

真实 NAS path 由用户确认后再写生产 compose。

WebUI 如启用：

```yaml
webui:
  enabled: true
  host: 0.0.0.0
  port: 8788
```

8788 只发布到受信任 LAN / Tailscale / authenticated reverse proxy；不要默认暴露公网。

---

## 14. W9.5：NAS 自包含运行架构（已由用户确认，硬要求）

用户已明确选择：

```text
方案 A：NAS 独立副本 / 自包含运行
```

最终目标不是“Windows 上保存数据、NAS 只跑一个壳”，而是：

```text
Windows 关机
-> NAS 上的 MCP 仍可正常启动
-> conversation_search 正常
-> conversation_recall 正常
-> conversation_read 正常
-> WebUI Conversations 正常
```

因此生产 NAS 不得在运行时依赖：

- `D:\Partition\...` 路径。
- Windows SMB 盘必须在线这一前提。
- Windows 本机 `conversation-index.sqlite`。
- Windows Obsidian Vault 实时挂载。
- Windows 本机 Python/Gateway 进程。

### 14.1 NAS 必须持久化的四层数据

建议 Unraid host 侧使用独立持久化根，例如：

```text
/mnt/user/appdata/research-memory-gateway/
```

至少包含：

```text
config/
  config.yaml

data/
  research-memory.db
  conversation-index.sqlite

conversations/
  Conversations/...
  .ai-memory/manifest.sqlite

imports/
  codex-sessions-20260912-012538.zip
```

其中：

- `imports/`：原始导出源，只读保存，用于以后补导/重建。
- `conversations/`：NAS 原生 Conversation archive，MCP/WebUI 的读取根。
- `data/conversation-index.sqlite`：NAS 原生 FTS + embedding cache/index。
- `config/`：NAS 专用运行配置；不得提交 GitHub。

如 Research Memory 主库已有独立持久化目录，可保持现有目录布局，但 Conversation archive/index/source ZIP 必须同样具备 NAS 本地持久化副本。

### 14.2 不直接复制 Windows conversation index

禁止把 Windows 当前：

```text
G:\LLM\memory\data\conversation-index.sqlite
```

原样作为 NAS production index 使用。

原因：`conversation_documents.vault_path` / sections path 等派生状态可能包含 Windows 绝对路径：

```text
D:/Partition/...
```

Linux 容器中这些路径不可直接读取。

首选迁移方式：

```text
复制 immutable raw ZIP 到 NAS
-> NAS 原生 import 生成 Markdown
-> NAS 原生 index
-> NAS 原生 BGE-M3 embedding/cache
```

这样所有 path 从一开始就是 Linux/container-native path，不需要事后 SQL path rewrite。

### 14.3 先迁移现有 35-session pilot，不继续 Batch 3

发布后的第一轮 NAS 数据初始化只重建当前已验证的 35-session pilot。

要求：

```text
Windows production pilot = 35 sessions
NAS production pilot     = 同一 35 sessions
```

不得借“迁移 NAS”名义继续导入第 36~322 场。

迁移顺序：

```text
1. 将 raw ZIP 复制到 NAS imports/，计算 SHA-256
2. 与 Windows 已登记 raw ZIP SHA-256 对比，必须完全一致
3. 在 NAS 专用 conversation root 对同一 35 IDs 执行 import
4. 二次 import 必须 35 skipped / unchanged
5. 在 NAS 上重建 conversation index
6. 执行 BGE-M3 embedding
7. 二次 changed-only index 必须无不必要 embedding 请求
8. 用 MCP/WebUI 做真实 search/recall/read smoke
```

NAS 可重新请求这 35 场的 BGE-M3 embeddings；35-session pilot 规模足够小，优先换取路径和生命周期的干净性。不要为了复用 Windows cache 而把 Windows SQLite 整库搬到 NAS。

### 14.4 Docker volume contract

NAS compose 必须显式挂载持久化数据，而不是只挂 `./data` 后假定其他内容存在。

目标容器内语义建议统一为：

```text
/app/config.yaml                  # read-only config
/data/research-memory.db          # persistent
/data/conversation-index.sqlite   # persistent
/data/conversations/              # persistent conversation archive
/data/imports/                    # raw ZIP source, preferably read-only
```

可采用等价 host 路径，但生产 config 中不得出现 Windows drive letter。

建议 volume 形态：

```yaml
volumes:
  - /mnt/user/appdata/research-memory-gateway/config/config.yaml:/app/config.yaml:ro
  - /mnt/user/appdata/research-memory-gateway/data:/data
  - /mnt/user/appdata/research-memory-gateway/conversations:/data/conversations
  - /mnt/user/appdata/research-memory-gateway/imports:/data/imports:ro
```

如实际 Unraid appdata 根不同，执行 Agent 应先探测并记录真实路径，不得凭空创建第二套重复目录。

### 14.5 NAS production config 语义

NAS 配置至少应解析为 Linux/container path，例如：

```yaml
conversation_archive:
  enabled: true
  staging_dir: /data/conversations
  vault_root: /data/conversations
  canonical_subdir: .
  require_explicit_vault_confirmation: true
  index_path: /data/conversation-index.sqlite

retrieval:
  mode: hybrid
  embedding:
    enabled: true
    model: bge-m3
```

上述仅表示目标语义；执行 Agent 必须以当前 config schema 的真实字段为准生成最终 NAS 配置，不能机械复制未知字段。

NAS 上的 Conversation archive 不要求作为 Obsidian Vault 使用；它是 MCP 自包含数据层。Windows 的 `Research-AI-Hub/Vault` 仍可继续作为用户本地 Obsidian 使用环境，但不再是 NAS MCP 的运行依赖。

### 14.6 原始 ZIP 也必须脱离 Windows

如果只把 35 篇 Markdown 放到 NAS，而 raw ZIP 仍只存在 Windows，则将来扩展剩余 287 场仍需要 Windows。

因此本方案要求将 immutable raw ZIP 同步一份到 NAS：

```text
/data/imports/codex-sessions-20260912-012538.zip
```

NAS 副本必须验证：

```text
SHA-256 == E1A853E494856163A0CC7493DE4EC0C73480487A7A1EFB9C2C902E83CB97018E
```

该 ZIP 只读，不允许 pipeline 修改。

以后剩余 287 场 backfill 可以直接由 NAS 完成，无需 Windows 在线。

### 14.7 Windows-off independence test（硬门槛）

正式宣布 NAS MCP READY 前，必须做一次“无 Windows 依赖”验证：

1. 确认 container mount 中不存在 `D:/` / `G:/`。
2. 确认 config 中不存在 Windows drive path。
3. 检查 conversation index live document path 全部为 `/data/...` 或实际 NAS/container 路径。
4. 禁用/断开任何 Windows share 后重启容器。
5. 重新执行：

```text
conversation_search("Fe3+")
conversation_search("B36")
conversation_recall("TOC_Focused.png")
conversation_read(<NAS path>)
```

6. WebUI `/admin/conversations` 同样可用。

只有上述通过才能写：

```text
NAS MCP WINDOWS-INDEPENDENT: PASS
```

否则只能报告部署未闭环。

---

## 15. W10：NAS 部署 smoke（用户授权后）

发布完成后，等待用户在 NAS 拉取，或明确授权 Agent 操作 NAS。

推荐使用**版本 tag**而非只依赖 `latest`：

```text
ghcr.io/kettly1260/research-memory-gateway:v0.2.x
```

Smoke 验收：

```text
container healthy
MCP endpoint reachable
WebUI /admin reachable
Conversations nav visible
conversation_search returns expected pilot data
conversation_recall obeys budget
conversation_read reads only allowed root
Research Memory 原有 tools 仍正常
```

至少用：

```text
Fe3+
TOC_Focused.png
B36
```

做真实查询。

---

## 16. Release 最终门槛

只有全部通过，才允许报告：

```text
Conversation Memory v0.2.x RELEASED
MCP READY FOR NAS DEPLOYMENT
WEBUI CONVERSATIONS READY
```

要求：

- Python tests 全绿。
- frontend lint/build PASS。
- WebUI Conversation backend tests PASS。
- 35-session read-only smoke PASS。
- staged file 清单无 secret/local config/data。
- main 包含 release commit。
- tag 指向 main release SHA。
- GitHub Actions PASS。
- GHCR tag/architectures 可见。
- NAS 使用本地持久化 conversation archive/index，而非 Windows live path。
- NAS raw ZIP 副本 SHA-256 与源一致。
- 35-session NAS-native rebuild PASS。
- Windows-off independence test PASS。

NAS 尚未拉取时只能写：

```text
READY FOR NAS DEPLOYMENT
```

不能写：

```text
NAS DEPLOYED
```

---

## 17. 本任务结束后的数据 rollout

等 MCP + WebUI 在真实使用中验证后，再决定剩余 287 场。

届时剩余数据的工作性质是：

```text
数据迁移 / backfill
```

不再是核心架构验收。

恢复 bulk rollout 前至少先积累一轮真实 MCP 使用反馈。

---

## 18. 给执行 Agent 的直接指令

```text
在 G:\LLM\memory 当前工作树执行：

docs/CONVERSATION_MEMORY_WEBUI_RELEASE_TASKBOOK.md

目标：先补 Conversation Memory WebUI 只读控制台，再把当前 Conversation Memory + MCP + WebUI 发布到 GitHub/GHCR，供 NAS 拉取。

立即暂停 Batch 3，不再继续导入/embedding 剩余 287 场。

WebUI 第一版只能做：
- status
- search
- recall preview
- conversation read/detail

禁止加入：
- bulk import button
- bulk embedding button
- conversation delete/edit

WebUI 必须复用现有 service.conversation_retrieval，不得复制 ranking/recall 逻辑。

新增 /admin/conversations 页面和只读 /admin/api/conversations/* API，补 backend tests、frontend types/i18n、npm lint/build。

完成后用现有 35-session production pilot 做只读 smoke，不导入新 session。

发布阶段本任务明确允许 git commit/push/tag，但禁止提交本机配置、数据、exports、scratch、Vault、ZIP、数据库、secrets。发布前必须人工检查 git diff --cached --name-only。

目标 release：v0.2.0；如远端已存在则顺延到未占用的 v0.2.x。

最终必须让 release commit 进入 origin/main，再在该 main commit 上打 tag。禁止 force push。

确认 GitHub Actions 成功构建：
ghcr.io/kettly1260/research-memory-gateway:v0.2.x
以及 latest。

特别注意：GitHub/GHCR 不会迁移 Windows Conversation 数据。NAS 真正运行 conversation MCP 前，必须另外确认 NAS 能看到 Markdown archive + index SQLite，并把 Windows D:/ 路径转换成 NAS/Linux 路径；不要假装镜像 pull 后数据自动存在。

用户已经明确选择 NAS 自包含方案 A。不得设计成 NAS 运行时读取 Windows Vault/SMB share。

发布后只把当前 35-session pilot 在 NAS 上从 immutable raw ZIP 原生重建；不要继续 Batch 3。

不要复制 Windows conversation-index.sqlite 作为 NAS production index。NAS 必须自己生成 Linux-native Markdown path、manifest、FTS 和 embedding cache。

将 raw ZIP 复制到 NAS 持久化 imports 目录并核对 SHA-256，使未来剩余 287 场 backfill 也无需 Windows 在线。

最终必须实际验证 Windows share 不存在/不可用时，MCP conversation_search/read/recall 和 WebUI Conversations 仍可工作。

所有实施/发布结果写入：
docs/CONVERSATION_MEMORY_WEBUI_RELEASE_REPORT.md
```

