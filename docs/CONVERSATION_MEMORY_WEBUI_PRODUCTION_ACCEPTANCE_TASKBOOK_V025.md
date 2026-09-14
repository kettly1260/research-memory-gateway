# Conversation Memory WebUI v0.2.5 Production Acceptance — Agent Taskbook

日期：2026-09-13  
项目：`G:\LLM\memory`  
代码基线：`49c39694a191267d13974e00ce58773440d65361`  
正式生产：`ghcr.io/kettly1260/research-memory-gateway:v0.2.5`  
OCI revision：`a97f80b655ee5024abe2d609fb39fa0c54896313`  
NAS production container：`research-memory-gateway`  
回滚 container：`research-memory-gateway-pre-v024` (`v0.2.3`, stopped/preserved)  
目标：对 WebUI 做**真实生产验收**，重点覆盖 auth、Dashboard、Memories、Proposals、Conversations、Config、Import/Export、Audit、Security、降级行为、无空耗、视觉与交互。默认只读 production；发现问题可在独立本地 worktree 修复，但禁止直接部署生产。

---

# 0. 与 ChatGPT Importer Agent 的并发边界

可与 `CONVERSATION_MEMORY_CHATGPT_IMPORTER_TASKBOOK_V026.md` 同时执行。

本 Agent 默认只读 NAS production。

允许本地修改范围（仅当验收发现明确 WebUI bug 时）：

```text
src/research_memory_gateway/webui/**
tests/test_webui*.py
tests/*conversation*webui*.py
docs/CONVERSATION_MEMORY_WEBUI_PRODUCTION_ACCEPTANCE_REPORT_V025.md
```

禁止修改：

```text
src/research_memory_gateway/conversations/chatgpt_export.py
conversation identity schema
NAS live manifest
NAS live conversation index
322 Markdown notes
raw ZIP
production container/config
```

除非用户再次明确授权生产修复。

建议独立 worktree：

```powershell
git worktree add G:\LLM\worktrees\rmg-webui-acceptance -b qa/webui-v025 49c3969
```

任务书从主工作区读取即可。

---

# 1. 已知生产基线，不要重复迁移

开始前只读确认，预期：

```text
container                    research-memory-gateway
image                        v0.2.5
revision                     a97f80b
MCP                          18787 -> 8787
WebUI                        18788 -> 8788
network                      ai_network
restart                      unless-stopped

conversation legacy imports 322
source records               322
active canonical             322
documents                    322
sections                     26,269
embeddings                   1,559
pending duplicate candidates 0
```

不可变校验：

```text
Markdown aggregate SHA-256
508b7038f05b01fdea73f44b5d3f728f81b2f4f81ae35728548e1e0971153e1a

raw Codex ZIP SHA-256
e1a853e494856163a0cc7493de4ec0c73480487a7a1efb9c2c902e83cb97018e
```

如果生产不符合这些基线：停止写操作，记录差异，先判断是否另一个 Agent/用户刚做了授权变更。

---

# 2. 硬规则

1. **Production acceptance 默认只读。**
2. 不在 production 创建/编辑/删除 Memory，不 approve Proposal，不启动 backfill，不保存 Config。
3. 不修改 WebUI password、token、provider secret。
4. 不 stop/restart production container。
5. 不改 live SQLite、manifest、index。
6. 不为了测试 degraded mode 故意停 BGE-M3/数据库/网络。
7. 不在报告、截图文件名、console dump 中泄露 token/password/secret。
8. 登录凭据只从现有安全来源读取，绝不打印。
9. 浏览器截图若含敏感 memory 正文，只保存在本机 `.local/webui-acceptance/`，默认不 commit。
10. 如果发现 P0/P1 bug：保留证据，停止危险操作；只在本地 worktree 修复和测试，不直接上线。

---

# 3. 验收工具优先级

优先使用真实浏览器（Playwright/Chrome/Edge/Agent browser）完成交互与截图。

同时使用 API/curl 做可重复的后端证据。

如果 Agent 没有浏览器能力：

- API/HTML/静态 bundle 检查可以继续；
- `VISUAL ACCEPTANCE` 必须标 `NOT RUN`；
- 不能声称 WebUI 全验收 PASS。

浏览器建议 viewport：

```text
desktop primary  1440x900
desktop small    1280x720
mobile smoke      390x844
```

---

# 4. WU0 — 基线与证据目录

本地：

```powershell
git status --short
git rev-parse HEAD
python -m pytest -q
```

前端：

```powershell
cd src\research_memory_gateway\webui\frontend
npm ci
npm run build
npm run lint
```

如当前 package scripts 没有某项，记录 N/A，不自行发明命令。

本机证据目录：

```text
.local/webui-acceptance/
  screenshots/
  network/
  console/
  timings/
```

该目录不 commit。

---

# 5. WU1 — Authentication / session / route protection

## 5.1 未登录

验证：

```text
GET /admin                     -> login redirect / login shell
GET /admin/conversations       -> protected
GET /admin/api/stats           -> unauthorized/redirect
GET /admin/api/conversations/* -> protected
```

不能从未登录 API 拿到 memory/conversation data。

## 5.2 登录

使用已有 production WebUI credential，但：

- 不打印 password；
- 不写进 report；
- 不截图密码框明文；
- 不保存浏览器 password manager。

验证：

- 正确登录；
- 错误 password 有明确错误；
- 登录后 landing page 正常；
- 刷新保持 session；
- logout 后受保护页面不可访问；
- back navigation 不展示已退出的敏感缓存页面。

## 5.3 Cookie/session

检查 cookie 至少：

```text
HttpOnly
SameSite
Path
Max-Age/expiry 与 config 一致
```

如果 production 当前通过 HTTP 而非 HTTPS，`Secure=false` 不自动判 P0，但报告必须指出部署边界；如果外部暴露互联网则升为安全问题。

---

# 6. WU2 — 全局 SPA / 导航 / 静态资源

逐页检查：

```text
Dashboard
Memories
Memory Detail (只读打开)
Proposals
Conversations
Config
Import
Export
Audit
Security
```

每页验证：

- 无白屏；
- 无 React crash；
- 无 console error；
- 主 API 无 500；
- 浏览器 refresh 深链接仍可加载；
- sidebar 当前项高亮正确；
- 页面 title/header 正确；
- 中文与英文字符串无 raw key 泄漏；
- 1440/1280 下无关键按钮溢出；
- 390px 下至少能读取/导航，不要求完整桌面功能都舒适。

静态资源：

- JS/CSS/favicon 200；
- 无旧 hash 404；
- 浏览器 hard refresh 后仍能拿到一致 bundle。

---

# 7. WU3 — Dashboard / Stats

只读验收。

验证 UI 与 API 一致：

```text
Gateway 状态
Memory counts
Proposal counts
Conversation count
backend/retrieval state
embedding/rerank state
recent audit/error summary（如 UI 有）
```

Conversation 总数不应仍显示 pilot 的 35。

若 UI 显示的是 322 source/canonical/document 中某一种，label 必须语义正确。

禁止把：

```text
322 source records
322 canonical conversations
26,269 sections
```

混成一个不明确的 “Memories” 数字。

---

# 8. WU4 — Memories / Memory Detail（只读 production）

验证：

- 列表加载；
- filter/search/sort（如存在）；
- pagination；
- detail 打开；
- evidence/source refs 正常；
- verification status 正常；
- archived/deleted 状态不误显示；
- 长文本折行；
- Unicode/CJK；
- empty state。

**不要在 production 点击 Save / Archive / Restore / Delete / Hard Delete。**

这些 destructive/mutating flow 的功能正确性使用现有 pytest 或本地临时 SQLite 测试，不用 production 数据验证。

---

# 9. WU5 — Proposals（只读 production）

验证：

- proposal list 能加载；
- status/category/source metadata 正常；
- detail 能打开；
- batch selection UI 不误触自动提交；
- approval/save 按钮不会在页面加载时自动请求；
- 0 proposal 时 empty state 清楚。

禁止 production：

```text
approve
reject
batch save
capture into trusted memory
```

如果需要测试 mutation，使用本地临时 DB/测试服务。

---

# 10. WU6 — Conversations：本次重点验收

这是本任务最高优先级页面。

## 10.1 Status

`/admin/api/conversations/status` 与页面必须反映 production：

```text
documents/source conversations ~ 322
sections 26,269（如果状态 API 暴露）
source_system codex 322
identity/canonical migration healthy
```

如果 UI 仍显示“35 sessions”或旧 pilot 文案，判 P1。

## 10.2 Search

至少测试：

```text
Fe3+
Research-AI-Hub
一个普通非科研关键词
一个不存在关键词
```

要求：

- loading state；
- 0 result state；
- result count；
- title；
- source_system；
- thread_source；
- model_name（有数据时）；
- snippet；
- path/read action；
- source/canonical identity API 非空。

最终 `Fe3+` production hybrid 已知基线：

```text
count >= 1
fallback_to_lexical = false（BGE healthy 时）
source_key != ""
canonical_conversation_id != ""
```

若 BGE 当时真实不可用：

- UI/API 应 lexical fallback；
- 不能 500；
- 应能看到 degraded/fallback signal（若产品设计有）；
- 报告区分“外部 embedding unavailable”与 WebUI bug。

## 10.3 Legacy read hotfix

必须选一条 v0.2.3 时代旧 Markdown（frontmatter 本身没有新 identity），从 UI result 打开 read。

API 必须：

```text
source_key non-empty
canonical_conversation_id non-empty
source_system = codex
conversation_id non-empty
```

这是 v0.2.5 release blocker 的回归项。

## 10.4 Recall

验证：

- recall 返回 context items；
- item source/canonical identity 非空；
- 默认 canonical collapse 语义不制造重复；
- token budget 变化有合理响应；
- recall 页面/面板无 HTML 注入。

## 10.5 Long conversation

打开至少一条大 Markdown（>100k chars，如可从现有数据找到）：

- 页面不崩；
- browser 不冻结；
- content area 可滚动；
- Markdown/code block/Unicode 基本可读；
- 不一次性把全页面布局撑坏。

如果 UI 对 500k char note 明显卡顿，记录性能 issue，不要为了验收改 production 数据。

---

# 11. WU7 — Config 页面（只读）

验证 effective config 展示：

```text
backend sqlite
conversation_archive enabled
staging dir NAS-local
index path conversation-index-v024.ready.sqlite
retrieval hybrid
embedding bge-m3
rerank disabled
```

关键安全要求：

- password/token/API key 只显示 masked/present，不回显明文；
- page source/API response 不包含 secret value；
- 浏览器 devtools network response 不泄露 secret；
- config 中不得出现 Windows `D:/` 或 `G:/` runtime dependency。

**不要点击 Save/Patch/Delete Secret。**

`Test Connection` 如果是无持久化且只探测当前 provider，可测试一次；必须确认它不写 config。

---

# 12. WU8 — Import / Export 页面

Production 只读/视觉验收。

Import：

- 页面加载；
- validation UI 有明确格式/错误状态；
- 不选择真实文件时不会自动触发 import；
- 不把用户本地路径上传到日志；
- execute 必须显式动作。

不要在 production 真执行 JSON/Conversation import。

Export：

- 页面加载；
- options 文案正确；
- 不点击实际生成敏感 export，除非 export API 明确是无副作用且输出保存到安全临时位置；
- 默认本任务只做页面与 API contract 检查。

---

# 13. WU9 — Audit 页面

验证：

- audit list 能加载；
- 时间/operation/actor/result 可读；
- pagination/filter；
- 不展示 secret；
- 不把完整 embedding payload/token/password 写进 event detail；
- v0.2.5 production cutover 后最近事件如果存在，应合理，不要求一定有某条固定 event。

---

# 14. WU10 — Security 页面

只读验证：

- 当前 auth 状态；
- password change 表单不会显示现密码；
- token/secret 不明文；
- logout 可用；
- destructive security action 要明确确认；
- 浏览器 autocomplete 配置合理；
- API error 不返回 stack trace/secrets。

不要在 production 修改 password/token。

---

# 15. WU11 — Network idle / 空耗门禁

用户明确要求软件不能空消耗资源。

登录 Dashboard 和 Conversations 页面后，各观察至少 60 秒 network/activity。

记录：

```text
request count during idle
repeating endpoint
interval
response size
CPU if easily available
```

Release acceptance：

- 不得每秒轮询；
- 不得 idle 时重复做 vector search；
- 不得 idle 时调用 embedding/rerank；
- 不得持续下载大 stats payload；
- 合理的低频 session/status ping 若存在要记录，但不能造成明显 CPU/network。

如果发现持续高频 polling：P1。

---

# 16. WU12 — Error / degraded behavior（不得人为破坏 production）

只观察自然出现的 degraded state。

如果 BGE-M3 临时不可用：

```text
search -> lexical fallback
WebUI -> 仍可用
no 500
no infinite spinner
fallback/degraded state visible if supported
```

如果当前 BGE healthy，不为了测试故意停服务；在本地 unit/integration test 用 mock 验证 degraded UI/API。

同理不要人为断 SQLite/NAS mount。

---

# 17. WU13 — 视觉 QA

每个主要页面至少一张 desktop screenshot；关键 Conversations 建议：

```text
status/default page
search results
conversation read
recall
zero-result
```

检查：

- 文本裁切；
- 卡片重叠；
- table 横向溢出；
- source/canonical 长 ID 是否撑破布局；
- CJK 字体；
- code/Markdown；
- dark/light（如果支持）；
- sidebar collapse；
- mobile smoke。

截图默认保存在 `.local/webui-acceptance/screenshots/`，不 commit。

---

# 18. WU14 — Accessibility / keyboard smoke

至少：

- Tab 可到主要导航与表单；
- focus visible；
- button 可 keyboard activate；
- input 有 label/aria-label；
- modal/dialog 可关闭；
- color 不是唯一状态信号；
- loading/error 有文本；
- table/list 有可理解结构。

不要求本任务完成 WCAG 全审计，但明显 blocker 必须记录。

---

# 19. WU15 — 前端/后端静态质量门禁

本地运行：

```text
python -m pytest -q
frontend npm run build
frontend npm run lint
git diff --check
```

如果发现 WebUI bug 并修复：

- 在独立 worktree 修改；
- 补 regression test；
- build/lint/test 全绿；
- 创建本地 commit；
- 不 push/deploy，等待用户 review。

如果没有代码 bug，只生成验收报告，不需要制造 commit。

---

# 20. 缺陷严重度

## P0 — 立即停止验收并报告

- 未登录可访问敏感 data；
- secret 明文泄漏；
- 只读浏览触发数据修改/删除；
- WebUI 操作损坏 manifest/index；
- production container crash/restart loop；
- auth bypass。

## P1 — 发布级 blocker

- Conversations 仍只能看到 35 pilot；
- search/read/recall identity 丢失；
- legacy read v0.2.5 hotfix 失效；
- 页面核心功能 500/白屏；
- idle 高频 expensive polling；
- Config 页面 secret 回显；
- navigation 深链接不可用。

## P2 — 应修复

- 某 filter 不工作；
- 大 conversation 明显卡顿；
- mobile/layout 问题；
- error/degraded 提示差；
- i18n 文案错误。

## P3 — polish

- spacing；
- label 文案；
- 非关键 tooltip；
- 次要视觉一致性。

---

# 21. Production acceptance 最终 gate

必须核对：

```text
Production image/revision correct                         PASS
WebUI auth protection                                    PASS
Login/logout                                             PASS
SPA/static assets                                        PASS
Dashboard                                                PASS
Memories read-only                                       PASS
Proposals read-only                                      PASS
Conversations count/status 322                           PASS
Conversation search                                      PASS
Legacy conversation read identity                        PASS
Conversation recall identity                             PASS
Hybrid query or correct lexical degradation              PASS
Config secrets masked                                    PASS
No Windows runtime paths                                 PASS
Import/Export page no accidental execution               PASS
Audit no secret leak                                     PASS
Security page no secret leak                             PASS
Idle expensive polling                                   ZERO
Console P0/P1 errors                                     ZERO
Network unexpected 500                                   ZERO
Production data mutation during acceptance               ZERO
Markdown aggregate SHA unchanged                         PASS
raw ZIP SHA unchanged                                    PASS
rollback v0.2.3 preserved                                PASS
```

如果没有浏览器能力：

```text
VISUAL ACCEPTANCE NOT RUN
```

最终只能判：

```text
API/SECURITY ACCEPTANCE PASS; VISUAL PENDING
```

不能判 FULL PASS。

---

# 22. 报告输出

创建：

```text
docs/CONVERSATION_MEMORY_WEBUI_PRODUCTION_ACCEPTANCE_REPORT_V025.md
```

报告必须包含：

```text
timestamp
production image/revision
browser/version if used
viewport(s)
auth checks
page matrix
conversation checks
API checks
idle network checks
console errors
visual issues
accessibility smoke
performance observations
P0/P1/P2/P3 findings
immutability hashes
rollback state
code fixes if any
final verdict
```

禁止报告：

- password；
- bearer token；
- provider API key；
-完整 private memory text；
-完整 private conversation transcript。

允许记录 conversation id/source_key 的短前缀用于定位，但不要无必要复制完整敏感内容。

---

# 23. Git / 停机规则

若纯验收、无代码修复：

- 可以只生成 report；
- 不需要 push；
- 不改 production。

若有本地代码修复：

1. 只 stage 目标 WebUI 源码/测试/report；
2. `git diff --cached --check`；
3. 本地 commit；
4. 停止并汇报；
5. 禁止 push/tag/GHCR/NAS deployment，等待用户授权。

---

# 24. Agent 最终汇报模板

```text
PRODUCTION:
  image:
  revision:
  WebUI health:
  MCP health:

AUTH:
  unauth protected:
  login:
  logout:
  secret exposure:

PAGE MATRIX:
  Dashboard:
  Memories:
  Proposals:
  Conversations:
  Config:
  Import:
  Export:
  Audit:
  Security:

CONVERSATIONS:
  status count:
  search:
  legacy read identity:
  recall identity:
  hybrid/fallback:

IDLE RESOURCE:
  repeated requests:
  expensive polling:

VISUAL:
  browser used:
  screenshots:
  desktop:
  mobile:

FINDINGS:
  P0:
  P1:
  P2:
  P3:

IMMUTABILITY:
  Markdown SHA unchanged:
  raw ZIP SHA unchanged:
  production mutation count: 0

ROLLBACK:
  v0.2.3 preserved:

CODE FIXES:
  none / local commit SHA
  pushed?: NO

VERDICT:
  FULL PASS
  or
  PASS WITH P2/P3
  or
  BLOCKED
```

