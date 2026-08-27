# Research Memory Gateway V2 实施审计

本审计按根目录 `RESEARCH_MEMORY_GATEWAY_V2_AGENT_MEMORY_REFACTOR_PLAN.md` 逐项核对当前
`v2/p0-hardening` 工作树。结论是：V2.0/P0 的仓库内实现已经基本完成；剩余阻塞主要是
真实客户端自主调用验收、会话归档桥，以及发布分支切换，而不是 Agent Surface 缺少实现。
截至 2026-08-28，Codex 已有一条真实 Recall pilot 成功记录，但尚不足以形成完整通过率。

## 审计结论

| 任务书阶段 | 状态 | 证据或剩余工作 |
|---|---|---|
| Phase 0：V1 冻结与基线 | 部分完成 | 本地 legacy 分支与固定 tag 已存在，schema/tool/config/WebUI 基线已记录，回归为 121 passed。`origin/main` 仍停在 V1，发布切换尚未执行。 |
| Phase 1：`recall_memory` | 完成 | 默认 compact 返回、查询重写、生命周期过滤、混合检索复用、claim-aware 结果均已实现。 |
| Recall context budget | 完成 | compact 默认预算 1000 tokens，standard 默认预算 1500 tokens；超限时裁剪低排名结果以及 query/project header，并返回预算/截断元数据。 |
| Phase 2：`verify_memory` | 完成 | 支持 memory/claim 级验证，返回 claim、evidence、source ref、冲突、superseded 与生命周期信息。 |
| Phase 3：`capture_memory` | 完成 | 低成本输入、项目解析、分类、claim/evidence/source ref/entity 生成、查重、冲突提示与 secret redaction 已实现。 |
| Phase 4：双层记忆 | 完成 | Ambient 自动保存；Trusted 默认进入 Proposal Queue；显式用户确认可立即保存。 |
| Phase 5：Proposal Workflow | 完成 | 后台队列和 WebUI 单条/批量 approve、reject、needs edit、save 已实现。 |
| Phase 6：Agent/Admin Surface | 完成 | 默认 Agent Surface 仅 4 个工具；admin/full 可配置。 |
| Phase 7：短 Skill/Prompt | 完成 | Agent Skill/System Prompt 已改为主动 recall/capture/verify 规则，不暴露内部 taxonomy/schema。 |
| Phase 8：Benchmark 资产 | 完成 | 30 条 Recall、71 条 Capture；scorer 检查漏测、重复/未知 case、阈值、延迟和 token 记录，并支持 `--require-pass`；另有 8 条可幂等写入隔离数据库的固定 Recall corpus。 |
| Phase 9：ChatGPT 接入验收 | 外部阻塞 | Workspace 中只有一个空白未发布 Agent，且可用 app 中没有 Research Memory Gateway。必须先注册 Streamable HTTP custom MCP app、挂载到 Agent，并经用户确认发布后才能记录真实结果。 |
| Phase 10：跨客户端验收 | 进行中 | Codex R01 真实 pilot 已自主 recall 并命中正确记忆；30/71 全量结果仍未产生。优化后的 R02 因 Codex 模型服务网络超时未形成结果。Cherry Studio、KiloCode 尚未执行。 |
| Conversation archive bridge | 未完成 | 当前可保存 conversation anchor，但没有可重新打开原始会话的客户端/session archive bridge。 |
| V2.3/V3 | 按计划暂缓 | Automatic ingestion、EverOS/Reflection 不属于当前 V2.0 P0。 |

## 发布阻塞

当前分支关系为：

- `legacy/research-memory-gateway-v1` 与 `v1-pre-agent-memory-refactor` 指向 V1 基线。
- 本地 `main` 包含 V2 Alpha。
- 当前开发分支是 `v2/p0-hardening`。
- `origin/main` 仍指向 V1 基线。

在真实 ChatGPT/Codex invocation benchmark 至少完成一轮并人工复核前，不应把
`v2/p0-hardening` 宣称为已经通过客户端验收，也不应仅凭单元测试报告主动调用率。

## 本次补足

1. 安装并实际运行开发测试依赖；当前完整回归为 `121 passed`。
2. 为 recall 增加可配置的上下文预算和超限裁剪，补齐任务书第 15 节。
3. 强化 benchmark scorer：结果完整性、case ID 合法性、target gate、延迟/token 汇总、CI 非零退出。
4. 将超长 project 纳入 hard budget，并为 `get_project_state` 增加独立 standard budget。
5. 增加对应回归测试并更新示例配置与验收记录。
6. 增加隔离 Recall benchmark 数据库的固定 8 条 seed corpus，并验证重复 seed 稳定。
7. 修复 Agent 把 cwd/repository path 错当 project 时的零命中：project 过滤无结果后回退全局检索，并返回 `project_filter_fallback`。
8. 保存真实 Codex R01 pilot JSONL；该结果证明单次自主调用，不替代 30 条 Recall 验收。
9. 重新核验 ChatGPT Workspace Agent/app 状态，并记录 custom MCP 尚未注册的外部阻塞。

## 下一验收动作

1. 恢复 Codex 模型服务连通性后，用精简工具上下文跑完 30 Recall / 71 Capture。
2. 注册 ChatGPT custom MCP app，挂载并发布 Workspace Agent 后执行相同数据集。
3. 保存每个客户端的完整 Recall/Capture JSONL，使用 `--require-pass` 评分。
4. 复核 false positive、错误 tier 与错误 memory，而不只看总分。
5. 达标后再决定把 hardening 提交合入并发布到 `origin/main`。
