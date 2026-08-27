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

V2 adds two backward-compatible fields without changing the SQLite table schema:

- `memory_tier`: `ambient` or `trusted`. Legacy memories without the field deserialize as `trusted`.
- `claims[].claim_id`: stable claim-level identifier generated on parse/write when absent, used by `verify_memory` for targeted provenance expansion.

## V2 Memory Tiers

`memory_tier` controls the default capture workflow, not scientific truth status:

- `ambient`: low-risk reusable project context such as paths, tool configuration, workflow rules, project state, and stable preferences. `capture_memory` may save these automatically when `memory.ambient_auto_save=true`.
- `trusted`: high-value research content such as experimental conditions/results, quantitative values, solution recipes, spectra/peak positions, LOD, mechanisms, and literature conclusions. `capture_memory` creates a proposal by default when `memory.trusted_capture_requires_review=true`.

Tier is independent from `verification_status`. A Trusted memory can still be `unverified`; the tier says it deserves stricter governance, not that the claim has already been proven.

Capture classification is intentionally based on **semantic role/structure**, not a whitelist of the user's current compounds. Generic scientific measurements (value + scientific unit, dimensionless material/property assignments), characterization observations, experimental conditions/results, literature conclusions, and research decisions are treated as Trusted. This is why values such as Tg, dielectric constant, thermal conductivity, tensile strength, XRD peaks, and TGA temperatures are governed the same way as PL/Fe chemistry data.

The default is fail-safe: only content that is positively recognized as low-risk Ambient project/config/workflow state may auto-save as Ambient. Durable content that cannot be confidently classified as Ambient is routed to Trusted review rather than silently auto-saved. A future model-backed semantic classifier may refine uncertain cases, but its absence must not weaken this safety boundary.

## Ambient state keys and supersession

State-like Ambient memories may carry `metadata.semantic_key`, for example:

```text
origin-mcp.repository_path
multi.frontend.repository_path
multi.backend.repository_path
memory-gateway.rerank.selected_model
```

Semantic slots are inferred as `project + subject/entity + property`. The parser accepts both `subject + property` (`Frontend repository path ...`) and `property + for/of + subject` (`Repository path for frontend ...`), and normalizes Chinese possessives such as `前端仓库路径` / `前端的仓库路径` to the same subject. Auto-supersession is intentionally conservative: the gateway only archives an older value when both records can be assigned to the same high-confidence slot. Distinct subjects such as `frontend.repository_path` and `backend.repository_path` remain active simultaneously. Temporal adjectives such as `old`, `new`, and `original` are treated as modifiers, not subjects, so `Original repository path ...` does not accidentally become an `origin` slot. If an explicit qualifier such as `for/of` is present but cannot be parsed confidently, slot inference returns `None` rather than falling back to a coarse project-level slot.

When `capture_memory` receives a newer active Ambient value with the same high-confidence semantic slot, the previous value is archived and its claims are marked `superseded`. Normal recall only searches active memories, so stale paths/branches/models/configuration do not outrank the current value. Legacy V2 Alpha path/state records are re-inferred from their content before supersession; an ambiguous legacy record is preserved rather than auto-archived.

Research hypotheses are not treated as worthless speculation. Hypothesis detection combines speculative modality (`可能`, `may`, `might`, `could`), a causal/association relation (`导致`, `有关`, `相关`, `归因于`, `due to`, `related to`, `associated with`, `attributed to`), and research context or explicit validation intent. Statements such as `AIE 可能与分子内运动受限有关，需进一步验证` and `The fluorescence enhancement may be related to restricted intramolecular motion and needs verification` are routed to `trusted / mechanism_hypothesis` with `unverified` or later `inferred` verification. Unsupported transient guesses without a reusable research hypothesis may still be ignored.

Explicit reply/answer preferences such as `以后回复简洁一点`, `记住以后回答尽量简洁`, and `Keep replies concise` are classified as low-risk Ambient preference context rather than Trusted research facts.

`user_confirmed=true` represents an explicit user request to remember the supplied content. It overrides normal durability/speculation/one-off ignore heuristics, but it never bypasses secret redaction or write validation.

## Secret redaction before persistence

All service-backed writes and all `capture_memory` paths run through a recursive credential scanner before persistence. It scans string content and nested fields including summary, claims, evidence, source-ref excerpts, entities, metadata, confirmation payloads, audit metadata, **dictionary keys**, and dictionary values. Detected credential values are replaced with `[REDACTED]`; secret-shaped dictionary keys are replaced with `[REDACTED_KEY]` before SQLite/FTS indexing.

The scanner covers labeled passwords/tokens/API keys, Bearer/Authorization values, common `sk-` and GitHub token shapes, JWTs, AWS access keys, private-key blocks, URL passwords, cookies/sessions, and connection-string credentials. This is defense in depth: agents should still avoid sending credentials to `capture_memory` intentionally.

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

For the V2 Agent Surface, agents normally call `capture_memory` instead of constructing proposals directly. The gateway classifies Ambient vs Trusted, builds the strong schema, checks overlap, and applies the appropriate write policy.

The underlying Admin workflow remains:

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

## Conversation source anchors

When a client can provide stable provenance identifiers, `capture_memory` accepts:

- `source_client`
- `conversation_id`
- `message_id`
- `session_id`
- `source_timestamp`

These are stored on conversation `SourceRef` / evidence metadata. If only the default `source_context="current conversation"` is available, the source is an assertion anchor rather than a reopenable transcript. `open_source_ref` reports `kind=conversation_anchor` and `resolvable=false` until the client or a future Memory Bridge provides an archived/exported conversation source.

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
