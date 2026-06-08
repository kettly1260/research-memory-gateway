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
