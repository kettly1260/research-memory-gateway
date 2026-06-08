---
name: research-memory-gateway
description: Guides agents to use the Research Memory Gateway MCP for evidence-first scientific memory across Codex, Cherry Studio, KiloCode, and related tools. Use when doing literature review, paper notes, synthesis route planning, experiment planning, mechanism hypotheses, material-system tracking, presentation outlines, or research decisions that may need long-term recall.
---

# Research Memory Gateway

Use this skill when the user is doing reusable scientific research work and the `research-memory-gateway` MCP tools are available.

## Core Rule

Long-term memory is for reusable research assets, not ordinary chat history.

Never save directly unless the user has explicitly confirmed. First call `propose_save`; call `save_research_memory` only after confirmation with `user_confirmed=true`.

## When To Propose Saving

Call `propose_save` when the session produces one of these assets:

- A literature review or technical-route comparison.
- A reusable paper note.
- A synthesis route, precursor choice, reaction condition plan, or risk analysis.
- An experiment plan, control design, characterization workflow, or expected-result criterion.
- A mechanism hypothesis, falsification condition, or evidence evaluation.
- A material-system summary, performance table, or structure-property relation.
- A PPT, report, group-meeting, or thesis-defense outline.
- A research decision that changes future work.

Do not propose saving for small clarifications, one-off translations, casual discussion, or incomplete speculation unless the user asks to remember it.

## Tool Workflow

1. If current work may duplicate earlier work, call `check_overlap` first.
2. Build a strong-evidence memory object.
3. Call `propose_save` with the reason and suggested memory.
4. Show the user a concise save proposal: type, title, claims, evidence count, source_refs, overlap candidates.
5. Wait for explicit confirmation.
6. If confirmed, call `save_research_memory` with `user_confirmed=true` and the `proposal_id`.
7. If the user asks to verify or trace a claim, call `open_source_ref` before relying on the memory.
8. If the user asks for memory hygiene, call `audit_unverified`.

## Strong Evidence Memory Shape

Use exactly one of these `memory_type` values:

- `literature_review`
- `paper_note`
- `synthesis_route`
- `experiment_plan`
- `mechanism_hypothesis`
- `material_system`
- `presentation_outline`
- `research_decision`

Every proposed memory should include:

- `project`: research project or topic namespace.
- `topic`: specific scientific topic.
- `memory_type`: one of the allowed types.
- `title`: short searchable title.
- `summary`: retrieval entry point only, not the source of truth.
- `claims`: checkable conclusions with confidence and verification status.
- `evidence`: exact quote, DOI, URL, paper title, file path, or source excerpt when available.
- `source_refs`: original session, file, DOI, or URL anchors.
- `entities`: materials, analytes, papers, methods, precursors, solvents, metrics, hypotheses.
- `relations`: lightweight graph edges between entities.
- `tags`: stable searchable keywords.
- `next_actions`: follow-up experiments, searches, or verification work.

## Anti-Hallucination Rules

Treat `summary` as an index, not evidence.

Use `evidence_backed` only when a claim references valid `evidence_ids`.

Use `unverified` when evidence is missing.

Use `inferred` when the claim is reasoned from evidence but not directly stated.

Use `conflicting`, `superseded`, or `retracted` when older memories should not be used as-is.

Do not present an `unverified` or `inferred` claim as an established scientific conclusion.

## Save Proposal Format To User

After `propose_save`, present this concise summary:

```text
建议保存为长期科研记忆：
类型：<memory_type>
标题：<title>
项目：<project>
可核查结论：<n> 条
证据：<n> 条
来源锚点：<n> 个
潜在重复/冲突：<n> 条
状态：等待用户确认后才写入
```

Do not end with an open-ended offer. If confirmation is needed, ask one direct confirmation question.

## Retrieval Rules

When answering from memory:

- Prefer `search_research_memory` before relying on vague recollection.
- Report whether claims are `evidence_backed`, `inferred`, or `unverified`.
- Use `open_source_ref` when the user asks for provenance or when a high-stakes experimental decision depends on the claim.
- Surface conflicts instead of hiding them.
