# Research Memory Gateway v0.2.5 WebUI Production Acceptance Report

- **Taskbook**: `docs/CONVERSATION_MEMORY_WEBUI_PRODUCTION_ACCEPTANCE_TASKBOOK_V025.md`
- **Execution Date**: 2026-09-14
- **Production Target**: `ghcr.io/kettly1260/research-memory-gateway:v0.2.5` (commit `a97f80b655ee5024abe2d609fb39fa0c54896313`)
- **Execution Mode**: Strict Read-Only Production Verification
- **Verification Worktree**: `G:\LLM\worktrees\rmg-webui-acceptance` on branch `qa/webui-v025`
- **Verdict**: **PASS WITH P2** (Visual and API production acceptance 100% complete; 1 frontend P2 defect identified and fixed in worktree, pending production deploy)

---

## 1. Executive Summary

Research Memory Gateway v0.2.5 WebUI 生产环境验收已按照 `CONVERSATION_MEMORY_WEBUI_PRODUCTION_ACCEPTANCE_TASKBOOK_V025.md` 规范严格执行。本次验收通过 SSH 隧道直连真实 Unraid NAS 生产实例（`http://127.0.0.1:18788`），覆盖了 Authentication / Session、9 大主要页面矩阵、Conversations 控制台核心能力（322 会话状态、Fe3+ 混合检索、溯源抽屉、旧版 Markdown 溯源身份回填、召回预览、长文档渲染）、配置脱敏、60 秒空载资源监控（Dashboard 与 Conversations）、桌面（1440/1280）与移动端（390px）视觉 QA、键盘无障碍及数据不可变性校验。

**验收结果全景**：
- **生产数据零篡改**：未在生产环境执行任何创建、更新、删除、归档、回填、配置变更或导入导出操作。
- **不可变性强校验**：Raw Codex ZIP SHA-256 与 322 份 Markdown 文件聚合 SHA-256 与上线基线 100% 吻合。
- **回滚保障完好**：v0.2.3 容器（`research-memory-gateway-pre-v024`，image: `ghcr.io/kettly1260/research-memory-gateway:v0.2.3`，状态 `Exited (0)`）与真实 SQLite 备份文件（`manifest.pre-v024.sqlite`、`manifest.pre-v025-cutover.sqlite`、`manifest.v025.postcutover.sqlite`、`conversation-index-v023-full.pre-v024.sqlite`、`conversation-index-v024.ready.sqlite`）均完好保留在 NAS 生产目录。
- **资源零空耗**：Dashboard 与 Conversations 页面在 60 秒 idle 期间录得 **0** 次 API 请求，无任何轮询与向量/Embedding 额外开销。
- **缺陷发现与修复**：发现 1 处前端 P2 缺陷（顶部栏全局搜索 / `Ctrl+K` 打开命令面板时 `CommandDialog` 缺少 `<Command>` 上下文包装引发客户端 TypeError）。该缺陷已在独立 worktree `rmg-webui-acceptance` 中完成修复并通过前端静态编译与检查，尚未部署生产环境。

---

## 2. Production Environment & Infrastructure

| 项目 | 生产状态 / 验证值 | 状态 |
|---|---|---|
| **Container Name** | `research-memory-gateway` | PASS |
| **Image** | `ghcr.io/kettly1260/research-memory-gateway:v0.2.5` | PASS |
| **Revision** | `a97f80b655ee5024abe2d609fb39fa0c54896313` | PASS |
| **Host** | Unraid NAS (`192.168.22.102`) | PASS |
| **Port Mapping** | `18788:8788/tcp` (WebUI: host 18788 -> container 8788), `18787:8787/tcp` (MCP: host 18787 -> container 8787) | PASS |
| **Rollback Container** | `research-memory-gateway-pre-v024` (image: `ghcr.io/kettly1260/research-memory-gateway:v0.2.3`, status: `Exited (0)`) | PASS |
| **Rollback / Backup Databases** | 真实备份保留：<br>• `manifest.pre-v024.sqlite`<br>• `manifest.pre-v025-cutover.sqlite`<br>• `manifest.v025.postcutover.sqlite`<br>• `conversation-index-v023-full.pre-v024.sqlite`<br>• `conversation-index-v024.ready.sqlite` | PASS |
| **Browser Engine** | Microsoft Edge (Chromium, Headless Automation via Playwright) | PASS |
| **Viewports Tested** | Desktop Large (1440x900), Desktop Small (1280x720), Mobile (390x844) | PASS |

---

## 3. Immutability Verification (生产数据未改动校验)

按照任务书第 4 节与第 21 节要求，验收前后对 NAS 挂载路径下的原始归档与文档进行了 SHA-256 校验：

| 校验项 | 基线预期 Hash | 生产实测 Hash | 结果 |
|---|---|---|---|
| **Raw Codex ZIP** | `e1a853e494856163a0cc7493de4ec0c73480487a7a1efb9c2c902e83cb97018e` | `e1a853e494856163a0cc7493de4ec0c73480487a7a1efb9c2c902e83cb97018e` | **MATCH** |
| **322 Markdown Aggregate** | `508b7038f05b01fdea73f44b5d3f728f81b2f4f81ae35728548e1e0971153e1a` | `508b7038f05b01fdea73f44b5d3f728f81b2f4f81ae35728548e1e0971153e1a` | **MATCH** |
| **Production Mutations** | 0 | 0 writes / 0 deletes / 0 config saves | **ZERO** |

---

## 4. Acceptance Matrix by Work Unit (WU0 – WU15)

### WU0 — Workspace & Local Baseline
- 独立 worktree 创建在 `G:\LLM\worktrees\rmg-webui-acceptance`，分支 `qa/webui-v025`，基于 commit `49c3969`。
- 本地回归测试：`python -m pytest -q` 运行 **250 passed, 0 failed** (42.88s)。
- `git diff --check`：无空白符或语法异常。

### WU1 — Authentication & Session Protection
- **未登录拦截**：
  - `GET /admin` 返回 `303 See Other -> /admin/login`。
  - `GET /admin/conversations` 返回 `303 See Other -> /admin/login`。
  - `GET /admin/api/stats` 返回 `401 {"error": "unauthorized"}`。
  - `GET /admin/api/conversations/*` 全部返回 `401 Unauthorized`。
- **登录凭据鉴权**：
  - 错误密码提交：返回 `401 {"error": "invalid_password"}`，前端给出清晰红字错误提示。
  - 正确密码登录：成功返回 200，颁发 `webui_session` Cookie。
- **Cookie 规范**：`HttpOnly=True`, `SameSite=Lax`, `Path=/`, `Max-Age=1800` (与配置 30 分钟完全吻合)。
- **登出与防回退**：点击顶部登出按钮后成功重定向至 `/admin/login`；浏览器点击后退按钮触发 401 拦截并立即跳回 `/admin/login`，绝不展示未授权敏感数据缓存。

### WU2 — Global SPA Navigation & Assets
- 9 大路由全部加载无 500、无白屏：
  `/admin` (Dashboard), `/admin/memories`, `/admin/conversations`, `/admin/proposals`, `/admin/config`, `/admin/security`, `/admin/import`, `/admin/exports`, `/admin/audit`。
- 静态资源 bundle (`.js`, `.css`) 全部 HTTP 200，无旧 hash 404。
- 语言切换（中 / EN）即时生效，无缺失 key。

### WU3 — Dashboard / Stats
- **记忆与提案数据**：活跃记忆 4 条，已归档 0 条，待审提案 0 条。
- **标签语义**：对话归档数正确显示 **322**（非旧 pilot 35），且与底层文档数 322 一一对应。
- 检索与向量覆盖状态展示清晰，图表渲染正常。

### WU4 — Memories & Memory Detail (Read-Only)
- 记忆列表加载正常，表格分页与搜索输入可用。
- 查看详情弹窗/页面可正常打开，Markdown 渲染与元数据展示完整。
- 生产环境未触发任何 Save / Archive / Delete 操作。

### WU5 — Proposals Review (Read-Only)
- 提案页面加载正常，当前为 0 条待审提案，Empty State 文案友好清晰。
- 批量选择与操作控件不会在加载时误触自动请求。

### WU6 — Conversations (重点核心验收)
- **Status 卡片**：
  - 归档文档数：`322`
  - 内容分块数 (Sections)：`26,269`
  - 向量覆盖率：`6.7%` (1,750 / 26,269)
  - 嵌入模型：`bge-m3` (1024d)
  - 来源平台分布：`CODEX: 322`
  - 线程角色分布：`Guardian Review: 24`, `子 Agent: 211`, `主会话: 87`
- **Fe3+ 混合检索**：
  - 搜索关键词 `Fe3+` 命中 10 条真实科研会话分块。
  - `fallback_to_lexical: False`，得分类型包含 `VECTOR | 0.602`。
  - 每条结果均附带 `source_key`（`srcv1_fabd4a40...`）与 `canonical_conversation_id`（`d03b83ff...`）。
  - 会话标题（如 `生成Cover Letter和可编辑TOC`）与小节路径展示准确。
- **阅读全文抽屉 (Reader Drawer)**：
  - 点击“阅读全文”成功拉出侧边抽屉，文件路径显示为 `/app/exports/conversations-v022-canary/Conversations/2026/...`。
  - 溯源锚点显示明确：`ordinal: 1363 | msg_0777ad18...`。
  - 提供“仅看匹配小节”与“查看完整会话”无缝切换。
- **Legacy Read Identity 回填 (v0.2.5 Hotfix 验证)**：
  - 对旧版 frontmatter 未显式记录 identity 的 Markdown 文档，通过 `GET /admin/api/conversations/read` 接口即时从 SQLite index 回填 `source_key` 与 `canonical_conversation_id`，杜绝空值。
- **Recall 召回效果预览**：
  - 输入 `Fe3+` 并设定 Token 预算 1500（3000 字符），成功在上限内召回 5 个小节。
  - 匹配类型为 `Hybrid Vector + Lexical`。
  - 注入 Agent 上下文预览（`<pre>` 区域）排版整齐，一键复制功能正常。
- **长会话渲染**：
  - 渲染大型会话（`生成Cover Letter和可编辑TOC` 完整文档），滚动条流畅，未发生页面卡顿或布局崩坏。
- **空结果状态**：
  - 检索不存在的字符串 `xyznonexistentkeyword999` 返回 0 结果，UI 展示优雅空状态。

### WU7 — Config & Secret Masking
- **脱敏检查**：API 密钥全部显示为 `••••••••`，底层响应及 Devtools 抓包均无明文密码或 Token。
- **路径检查**：配置展示中全部为 Linux 容器路径（如 `/app/exports/...`），绝对无 Windows `D:/` 或 `G:/` 痕迹。
- **只读保证**：未点击保存或修改配置。

### WU8 — Import / Export Visual QA
- **Import**：上传区域支持拖拽与格式校验，未选择文件时导入按钮处于安全禁用状态。
- **Export**：导出格式（JSON / Markdown）选项完整，未在生产触发副作用导出。

### WU9 — Audit Log
- 审计时间线完整记录系统安全事件（如 `retrieval.backfill_completed`, `security.password_changed`）。
- 详情 JSON 中无密码或敏感 Token 泄露。

### WU10 — Security
- 密码修改表单当前密码输入框不回显明文，API Keys 与连接状态一目了然。

### WU11 — Idle Resource Monitoring (空耗门禁)
- **Dashboard 60s Idle**：API 请求数 = **0**。
- **Conversations 60s Idle**：API 请求数 = **0**。
- **结论**：系统在页面打开静止时完全无任何高频 polling，无背景重复 vector search，无 Embedding/Rerank API 滥用消耗。

### WU12 — Degraded Behavior Boundaries
- BGE-M3 在线运行正常，因此生产流量走 Hybrid 向量路径。
- 本地单元测试验证了当 BGE-M3 不可用时自动降级到 FTS5 词法检索，不会发生 500 崩溃。

### WU13 — Visual QA (多分辨率覆盖)
- **Desktop 1440x900**：所有卡片、表格、Drawer、Modal 比例适中，无内容截断。
- **Desktop 1280x720**：导航栏自动适配，表格横向无溢出。
- **Mobile 390x844**：移动端汉堡菜单生效，卡片纵向流动排布，字体可读性高。

### WU14 — Accessibility & Keyboard Smoke
- 核心功能支持键盘 `Tab` 焦点导航与 `Enter` 激活。
- Drawer 与弹窗均支持 `Escape` 键快速关闭。

### WU15 — Code Quality & Static Verification
- `python -m pytest -q`：**250 passed, 0 failed**。
- `frontend npm run build`：**Build succeeded in 12.97s** (0 errors)。
- `frontend npm run lint`：**0 errors, clean**。
- `git diff --check`：**Clean**。

---

## 5. Defects & Findings

| 编号 | 严重级 | 问题描述 | 影响范围 | 状态 / 处置建议 |
|---|---|---|---|---|
| **F-01** | **P2** | 顶部栏全局搜索按钮点击或按下 `Ctrl+K` 打开命令面板时，`CommandDialog` 抛出 `TypeError: Cannot read properties of undefined (reading 'subscribe')` 错误。根本原因是 `CommandDialog` 的 `DialogContent` 内部未包裹 `<Command>` 原语组件，导致子组件（`CommandInput`, `CommandList`）缺少 `cmdk` 的 Context Store。 | 点击顶部搜索框或快捷键呼出命令面板的用户。 | **已修复**。在独立 worktree `rmg-webui-acceptance` 的 `src/research_memory_gateway/webui/frontend/src/components/ui/command.tsx` 中将 `{children}` 包装入 `<Command>` 组件，通过前端 build/lint 验证，等待后续发版集成。 |

**P0 / P1 缺陷数**：**0**。

---

## 6. Screenshots & Evidence Artifacts Index

所有视觉验收截图与网络/控制台日志均保存在项目主工作区绝对位置：
`G:\LLM\memory\.local\webui-acceptance\`

该目录已确认真实存在并包含以下 4 类凭证：
1. **25 张完整分辨率截图** (`screenshots/`，覆盖 1440 桌面、1280 桌面、390 移动端、搜索、阅读全文、召回、长文档、配置脱敏、登出保护及 P2 命令面板错误状态)
2. **完整网络抓包日志** (`network/network_requests.json`)
3. **浏览器控制台日志** (`console/browser_console.json`)
4. **自动化指标与空耗测试汇总** (`timings/acceptance_summary.json`)

```text
G:\LLM\memory\.local\webui-acceptance\
├── screenshots/
│   ├── 01-login-page-1440.png                (登录界面)
│   ├── 02-login-error-1440.png               (密码错误拦截提示)
│   ├── 03-dashboard-1440.png                 (控制台仪表盘)
│   ├── 04-memories-1440.png                  (记忆管理只读表格)
│   ├── 05-proposals-1440.png                 (待审记忆页面)
│   ├── 06-conversations-status-1440.png      (对话记忆 322 状态与分布)
│   ├── 07-conversations-search-fe3.png       (Fe3+ 混合检索结果列表)
│   ├── 08-conversations-reader-drawer.png    (对话全文阅读抽屉与溯源锚点)
│   ├── 09-conversations-search-hub.png       (Research-AI-Hub 检索)
│   ├── 10-conversations-search-empty.png     (空结果状态)
│   ├── 11-conversations-recall-results.png   (Fe3+ 召回效果预览与上下文注入)
│   ├── 12-conversations-long-render.png      (长文本 Markdown 完整渲染)
│   ├── 13-config-desktop-1440.png            (参数配置脱敏展示)
│   ├── 13b-config-embedding-1440.png         (Embedding 配置密钥掩码)
│   ├── 14-import-desktop-1440.png            (数据导入页面)
│   ├── 15-export-desktop-1440.png            (数据导出页面)
│   ├── 16-audit-desktop-1440.png             (审计日志时间线)
│   ├── 17-security-desktop-1440.png          (安全与授权页面)
│   ├── 18-dashboard-1280.png                 (1280 紧凑桌面控制台)
│   ├── 19-conversations-1280.png             (1280 紧凑桌面会话控制台)
│   ├── 20-dashboard-mobile-390.png           (390px 移动端控制台)
│   ├── 21-conversations-mobile-390.png       (390px 移动端会话列表)
│   ├── 22-config-mobile-390.png              (390px 移动端参数配置)
│   ├── 23-after-logout.png                   (退出登录后重定向保护)
│   └── 24-command-palette-opened.png         (命令面板唤起状态)
├── network/
│   └── network_requests.json                 (完整网络抓包日志)
├── console/
│   └── browser_console.json                  (浏览器控制台输出日志)
└── timings/
    └── acceptance_summary.json               (各工作单元自动化指标汇总)
```

---

## 7. Code Changes in Acceptance Worktree

在 `G:\LLM\worktrees\rmg-webui-acceptance`（分支 `qa/webui-v025`）上针对 F-01 缺陷的修复代码如下：

```diff
diff --git a/src/research_memory_gateway/webui/frontend/src/components/ui/command.tsx b/src/research_memory_gateway/webui/frontend/src/components/ui/command.tsx
index de85ba8..bdf8385 100644
--- a/src/research_memory_gateway/webui/frontend/src/components/ui/command.tsx
+++ b/src/research_memory_gateway/webui/frontend/src/components/ui/command.tsx
@@ -58,7 +58,9 @@ function CommandDialog({
         )}
         showCloseButton={showCloseButton}
       >
-        {children}
+        <Command className="[&_[cmdk-group-heading]]:px-2 [&_[cmdk-group-heading]]:font-medium [&_[cmdk-group-heading]]:text-muted-foreground [&_[cmdk-group]:not([hidden])_~[cmdk-group]]:pt-0 [&_[cmdk-group]]:px-2 [&_[cmdk-input-wrapper]_svg]:h-5 [&_[cmdk-input-wrapper]_svg]:w-5 [&_[cmdk-input]]:h-12 [&_[cmdk-item]]:px-2 [&_[cmdk-item]]:py-3 [&_[cmdk-item]_svg]:h-5 [&_[cmdk-item]_svg]:w-5">
+          {children}
+        </Command>
       </DialogContent>
     </Dialog>
   )
```

---

## 8. Final Verdict

```text
============================================================
FINAL VERDICT: PASS WITH P2
============================================================
- 生产环境运行稳定，核心 WebUI 功能 100% 验收通过。
- 对话归档文档数 322、分块 26,269、向量覆盖 6.7%、CODEX 100% 来源、Fe3+ 混合检索与溯源锚点全部吻合。
- 零生产数据篡改，哈希校验完全一致。
- 发现 1 个 P2 缺陷（命令面板包装缺失），已在独立 worktree 完成修复并通过验证，但尚未 production deploy。
============================================================
```
