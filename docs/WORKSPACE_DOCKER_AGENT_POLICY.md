# Workspace Docker Host Resource Hygiene Global Policy (P0)

此文档为 Workspace 顶层 `G:\LLM\AGENTS.md` 中 Docker Host 约束的受版本控制权威来源（Canonical Source）。
任何 Agent 只要在 `unraid` 或 `gau-unraid` 创建 Docker 资源，都必须严格遵守以下 10 项约束：

1. **Preflight inventory**：任务开始创建资源前，必须执行前置清单采集（`task-start`）。
2. **Explicit ownership**：所有新创建资源必须记录 `task_id` 与 ownership，使用统一标签：
   - `io.agent.managed=true`
   - `io.agent.task=<task-id>`
   - `io.agent.project=<project>`
   - `io.agent.created-by=<agent>`
   - `io.agent.lifecycle=temporary|candidate|production|rollback`
   无法打标签的资源（如构建层、匿名卷）必须由自动化工具计入 Task Manifest。
3. **Fail-closed cleanup**：无论任务成功、失败或异常中断，都必须无条件执行 cleanup phase。
4. **Postflight inventory**：任务结束时必须执行后置清单采集并由系统自动比对 Preflight 与 Postflight 增量（Delta）。
5. **Transient cleanup**：任务结束前必须清理本任务创建的所有临时/测试资源（temporary containers, superseded images, temporary networks, temporary volumes, disposable builders）。
6. **No global prune**：严格禁止执行粗暴的全局 prune（如 `docker system prune -a`、`docker image prune -a` 等）。
7. **Preserve unknown**：对 ownership 无法证明归属的资源，严禁自动删除。任务前已存在的未知资源归入 `preexisting_unknown` 并受保护；任务期间新增的未认领资源归入 `new_unknown` 并阻断门禁。
8. **Strict retention**：仅允许有意保留明确的 `production`、`rollback`、`active candidate` 或 `approved shared cache`。旧 Candidate 被替代后必须标记为 `superseded` 并清理。
9. **Mandatory residual reporting**：任务报告必须基于真实 Delta 明确列出 `residual task-owned garbage`。
10. **Hard completion gate**：若 `residual task-owned garbage` 非零或存在 `new_unknown` 资源，严禁宣称任务完成（Task Complete）或 Release-Ready。
