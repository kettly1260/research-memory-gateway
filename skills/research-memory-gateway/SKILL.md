---
name: research-memory-gateway
description: Use the Research Memory Gateway MCP for proactive long-term recall, capture, and provenance verification across research and software projects. Trigger whenever prior user/project context may affect the answer (previous, last time, earlier, continue, resume, prior experiment/config/path/decision, 之前、上次、以前、继续、还记得、原来、我们做过、怎么配的), when durable reusable information is produced, or when the user questions the source/reliability of a remembered scientific fact.
---

# Research Memory Gateway

Use the V2 Agent Surface. Keep memory work subordinate to the user's main task.

## Recall

- Call `recall_memory` before answering when previous user-specific or project-specific context could materially change the answer.
- Strongly trigger on previous/last time/earlier/before/continue/resume and 之前/上次/以前/继续/还记得/原来/我们做过/怎么配的.
- Also recall for established experiment conditions, project state, prior decisions, workflows, paths, tools, settings, or stable preferences.
- Do not guess historical user-specific facts when `recall_memory` can retrieve them.
- Keep `context_mode="compact"` unless the compact result is insufficient.

## Capture

- Call `capture_memory` when the interaction produces durable reusable information.
- Typical captures: project paths/state, stable configurations, workflows, settled decisions, experiment conditions/results, solution preparation details, and durable preferences.
- Pass the durable statement in `content`; add `project` when known.
- Do not construct taxonomy, claims, evidence, overlap checks, or proposal objects yourself. The gateway owns that schema.
- Do not capture casual chat, transient errors, pure questions, or unsupported speculation unless the user explicitly asks to remember it.
- Set `user_confirmed=true` only when the user explicitly asks to save/remember that specific information.
- Ambient memory may save automatically. Trusted Research Memory is queued for review unless explicitly confirmed.

## Verify

- Use `verify_memory` after recall when provenance or scientific reliability matters.
- Trigger on questions such as “Are you sure?”, “Where did that number come from?”, “Which paper/source supports this?”, or 你确定吗/这个数字哪里来的/原始来源是什么.
- Surface `unverified`, `inferred`, `conflicting`, `superseded`, or `retracted` status instead of presenting it as established fact.

## Project checkpoint

- Use `get_project_state` when resuming a known project and a recent-state checkpoint is more useful than a free-text recall query.

Admin tools are intentionally hidden from normal agents. Use the Admin Surface only for proposal review, merge/delete/update, audit, export, backfill, or debugging.
