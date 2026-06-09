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
