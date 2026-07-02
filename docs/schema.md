# Strong Evidence Schema

Every saved memory is a structured research asset. The `summary` field is only a retrieval entry point. Scientific conclusions must be represented as `claims` and linked to `evidence` whenever possible.

## Required Core Fields

- `project`
- `topic`
- `memory_type`
- `title`
- `summary`
- `claims`
- `evidence`
- `source_refs`

## Memory Lifecycle Status

Each `ResearchMemory` has a memory-level lifecycle status independent from claim verification status:

- `active`: default state. Normal search, audit, AI retrieval, and export include active memories.
- `archived`: retained for reference but hidden from normal search/export unless explicitly included.
- `deleted`: soft-deleted tombstone state. Hidden from normal search/export and recoverable until hard delete.

Lifecycle metadata fields:

- `memory_status`
- `status_changed_at`
- `status_change_reason`

Default semantics:

- `search_research_memory` returns only `active` unless archived/deleted are explicitly included.
- `check_overlap` searches `active`, `archived`, and `deleted` by default so duplicate/deleted history can be detected.
- `audit_unverified` audits only `active` by default.
- `export_memories` exports only `active` by default.
- Hard delete physically removes rows from `memories`, `memories_fts`, and `memory_embeddings`; it is only available after soft delete.

## Memory Types / 记忆类型

The canonical taxonomy is exposed by `get_memory_taxonomy` and WebUI `GET /admin/api/taxonomy`. Each category has stable English keys plus Chinese and English labels.

- `literature_review / 文献综述`
- `paper_note / 论文笔记`
- `synthesis_route / 合成路线`
- `experiment_plan / 实验规划`
- `mechanism_hypothesis / 机制假设`
- `material_system / 材料体系`
- `presentation_outline / 汇报提纲`
- `research_decision / 研究决策`
- `workflow_plan / 工作流规划`

`experiment_plan / 实验规划` and `workflow_plan / 工作流规划` require `metadata.plan_status / 规划状态` at write time. Legacy reads do not fail only because this metadata is missing.

## Plan Status / 规划状态

`metadata.plan_status` describes whether a plan-type memory can be used as an action basis. It is distinct from `proposal_status`.

- `draft / 草案`: context only; not actionable by default.
- `accepted / 已确认`: confirmed by the user; actionable by default.
- `active / 执行中`: currently in force; actionable by default.
- `superseded / 已被取代`: historical record only.

Optional `metadata.plan_type / 规划类型` values:

- `agent_memory_policy / Agent 记忆策略`
- `mcp_setup / MCP 配置`
- `research_workflow / 科研工作流`
- `writing_workflow / 写作工作流`
- `deployment_workflow / 部署工作流`
- `project_governance / 项目治理`

## Memory Proposals / 记忆提案

Agents should not silently write memories merely because the MCP or skill is installed. The normal workflow is:

1. Agent drafts a reusable memory candidate.
2. If the user has not confirmed saving, the candidate is stored as a proposal.
3. If the user confirms in chat, the agent calls `save_research_memory` with `user_confirmed=true` and a `confirmation` payload, so WebUI does not ask for a second confirmation.

`proposal_status / 提案状态` values:

- `pending / 待审`
- `approved / 已批准`
- `rejected / 已驳回`
- `needs_edit / 需修改`
- `saved / 已保存`
- `expired / 已过期`

Proposal versions are append-only in `memory_proposal_versions`. Old versions are retained for audit/history; sensitive redaction should use an explicit admin workflow instead of editing history in place.

Confirmation payloads are sanitized before storage and should include:

```json
{
  "source": "chat",
  "text": "可以，保存",
  "confirmed_by": "user"
}
```

## Verification Status

- `evidence_backed`: claim links to evidence IDs.
- `inferred`: plausible inference from evidence but not directly demonstrated.
- `unverified`: no supporting evidence yet.
- `conflicting`: conflicts with another saved memory or source.
- `superseded`: replaced by newer evidence or decision.
- `retracted`: should no longer be used.

## Light Graph Fields

Use `entities` and `relations` to support material- and mechanism-level lookup without forcing a full graph database in v1.

Examples of entity types:

- `paper`
- `material`
- `precursor`
- `solvent`
- `reaction_condition`
- `characterization_method`
- `performance_metric`
- `analyte`
- `mechanism`
- `hypothesis`

Examples of relation names:

- `detects`
- `synthesized_by`
- `uses_precursor`
- `requires_condition`
- `measured_by`
- `supports_hypothesis`
- `conflicts_with`
- `supersedes`
