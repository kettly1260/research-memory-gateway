# V2 Agent Memory Refactor — Baseline and Acceptance Record

## Current release status

- V2 Alpha architecture commit: `bfff3275e594277746ed12af8e6a420488683014`.
- P0 hardening is being validated on local branch `v2/p0-hardening`.
- **Do not push the Alpha commit to `origin/main` as the production mainline yet.**
- `origin/main` remains on the frozen V1 baseline until P0 hardening and real-client invocation acceptance are reviewed.

The Alpha proved that the Agent Surface and MCP transport architecture work, but real-data review exposed release-blocking semantics/security issues. The hardening branch fixes those issues without changing the SQLite table schema.

## Frozen V1 baseline

- Baseline commit: `7dcdf2c9dceb66e0c415cd3eafcd248d6b3f1539`
- Local legacy branch: `legacy/research-memory-gateway-v1`
- Local tag: `v1-pre-agent-memory-refactor`
- V2 Alpha development branch: local `main`
- P0 hardening branch: local `v2/p0-hardening`
- SQLite schema version: `5`
- V1 MCP surface: 19 management-oriented tools.
- V1 regression before/alongside the refactor:
  - `tests/test_models.py`: 45 passed.
  - `tests/test_webui.py`: 21 passed before the new V2 batch test was added.

The refactor deliberately leaves the existing SQLite/FTS/embedding/rerank/WebUI backend intact and adds a thin Agent Surface above it.

## V2 Agent Surface

Default `server.surface=agent` exposes only:

1. `recall_memory`
2. `capture_memory`
3. `verify_memory`
4. `get_project_state`

`admin` preserves the 19 legacy management tools. `full` exposes both surfaces for migration/debugging.

## Capture policy

- Ambient Memory: project paths, configuration, workflow, stable preferences, project state. Default behavior is deduplicated auto-save.
- Trusted Research Memory: quantitative research facts, experiment conditions/results, solution preparation, spectra/peaks, LOD, mechanisms, literature conclusions. Default behavior is Proposal Queue review.
- `user_confirmed=true` is reserved for an explicit user request to save/remember the specific Trusted item.
- Capture-generated scientific claims remain `unverified` unless separately validated; a conversation assertion is provenance, not automatic scientific proof.

### P0 hardening of Capture

The Alpha classifier was overfit to the initial Fe/PL benchmark vocabulary. The hardening branch removes compound-specific routing terms from the classifier and classifies by reusable semantic structure instead:

- scientific value + unit
- dimensionless material/property assignment
- measurement/result/observation
- characterization method + observation/assignment
- experiment condition/plan
- synthesis/research decision/mechanism/literature conclusion
- software/project state for Ambient context

Regression examples now include Tg, dielectric constant, thermal conductivity, tensile strength, XRD, TGA, ceramic density, battery capacity/efficiency, Raman, XPS, SEM/TEM, BET, DSC/DMA, and FTIR.

The direct Capture dataset contains **63** cases across research domains, software state, sensitive configuration, research hypotheses, explicit stable preferences, and negative chat/speculation/task samples. All 63 currently match the expected capture/ignore and Ambient/Trusted decision.

Classification is fail-safe: only positively recognized low-risk operational state (path/config/tool/workflow/Git/etc.) is eligible for Ambient auto-save. Durable content that remains semantically uncertain is routed to Trusted review rather than silently auto-saved as Ambient. This preserves safety even before a future model-backed semantic classifier is added.

### P0 hardening of secret handling

All service-backed writes and Ambient auto-save now pass a recursive Secret Scanner **before SQLite/FTS persistence**. It scans/redacts string content and nested memory fields including title/summary, claims, evidence, source-ref excerpts, entities, metadata, confirmations, audit metadata, dictionary values, and dictionary keys. Common password/token/API-key/Bearer/JWT/GitHub/AWS/private-key/cookie/session/connection-string credential forms are replaced with `[REDACTED]`; secret-shaped dictionary keys are replaced with `[REDACTED_KEY]`.

Tests explicitly verify that a captured `sk-...` value is absent from the stored JSON and cannot be found through FTS.

### P0 hardening of Ambient state

State-like Ambient records use high-confidence semantic slots derived as `project + subject/entity + property`. Examples include `multi.frontend.repository_path`, `multi.backend.repository_path`, and `origin-mcp.repository_path`. A new active value only supersedes an older record when both can be assigned to the same slot. Temporal modifiers such as `old`, `new`, and `original` are not treated as subjects. Legacy Alpha semantic keys are not blindly trusted; their content is re-inferred before supersession so an old coarse key cannot archive a distinct valid state.

### P0.1 hypothesis and explicit-save semantics

Scientific speculation is distinguished from disposable guessing. A causal/mechanistic statement with hypothesis/verification context (for example `AIE may cause the enhancement, to be verified`) is routed to Trusted `mechanism_hypothesis` rather than dropped by the speculation filter. Unsupported transient guesses such as an unchecked instrument-failure guess remain ignorable.

`user_confirmed=true` represents explicit user intent to remember the supplied content and therefore bypasses normal durability/speculation/one-off ignore heuristics. Secret scanning and write validation still run first and cannot be bypassed by explicit confirmation.

### MCP ToolAnnotations

The Agent Surface now publishes standard MCP `ToolAnnotations`: `recall_memory`, `verify_memory`, and `get_project_state` set `readOnlyHint=true`; `capture_memory` sets `readOnlyHint=false` and `destructiveHint=false`. The real Streamable HTTP integration test verifies these annotations through `mcp.ClientSession.list_tools()`.

## Recall semantics

`recall_memory` now rewrites history-oriented natural language before keyword retrieval. Conversational framing such as `之前`, `上次`, `怎么配的`, `last time`, and similar phrases is removed or normalized; useful preparation aliases are added where appropriate. The default keyword configuration now retrieves the Fe/HNO3 preparation record for the plan's natural prompt `之前 Fe 的硝酸溶液怎么配的？`.

Recall output is claim-aware. When a query matches one or more claims, the compact result returns `matched_claims` and uses the best matched claim as `content`. Each matched claim carries its **own** `verification` and `confidence`; the result no longer substitutes the worst verification status from an unrelated claim in the same memory.

`summary` remains an index/overview field and is returned separately.

## Conversation provenance

`capture_memory` accepts optional `source_client`, `conversation_id`, `message_id`, `session_id`, and `source_timestamp`. These fields produce an auditable conversation anchor. A generic `source_context="current conversation"` is not described as a reopenable transcript. `open_source_ref` reports `conversation_anchor` with `resolvable=false` until a client/Memory Bridge provides a resolvable archived session/export.

## Proposal Queue

The WebUI API now supports batch actions through `POST /admin/api/proposals/batch`:

- `approve`
- `needs_edit`
- `reject`
- `save`

The frontend proposal page supports multi-select and the same batch actions.

## Invocation benchmark

`benchmarks/` contains:

- 30 Recall natural-language cases.
- 63 Capture natural-language cases, including a cross-domain/adversarial generalization set.
- a client-neutral scoring script.

The invocation benchmark must be run in real ChatGPT/Codex/Cherry/Kilo clients. Unit tests cannot substitute for model/tool-choice measurements; therefore client recall/capture rates must not be reported as passing until those actual runs are recorded.

The Capture dataset is also executed directly against the default gateway classifier in pytest. All 63 cases currently match the expected capture/ignore decision and Ambient/Trusted tier. This proves classifier behavior **after the tool is called**; it does not prove autonomous agent tool choice.

Target gates:

- explicit historical recall rate >= 90%
- top-5 correct memory rate >= 90%
- recall false-positive rate < 10%
- durable capture rate >= 80%
- capture false-positive rate < 10%

## Local verification status

- Full pytest suite after P0.1 hardening: `107 passed`.
- V2 Agent Surface + benchmark code: Ruff check passed.
- Real Streamable HTTP MCP integration test passed using `uvicorn`, `mcp.ClientSession`, `initialize`, `list_tools`, natural-language `recall_memory`, `capture_memory`, secret redaction, and Ambient supersession.
- Frontend files were not changed by P0 hardening; ESLint and TypeScript `tsc -b` were re-run on the hardening branch and passed.
- `git diff --check`: passed (Windows line-ending conversion warnings only).
- Full-repository Ruff is **not** yet a clean gate: the V1 baseline contains 51 pre-existing style findings (for example legacy `datetime(timezone.utc)`, broad exception handling, and older test typing patterns). These were intentionally not mass-refactored as part of the Agent Memory change.

## Still not accepted

The following remain P1/client-acceptance work and must not be reported as complete:

- real ChatGPT autonomous recall/capture invocation rates
- real Codex autonomous recall/capture invocation rates
- Cherry Studio/Kilo cross-client invocation matrix
- recorded client result JSONL and target-rate acceptance
- reopenable conversation history without a client/session archive bridge

Unit tests and the direct 60-case classifier benchmark cannot substitute for those model/tool-choice measurements.

## Rollback

If V2 behavior regresses, compare against or restore from `legacy/research-memory-gateway-v1` / `v1-pre-agent-memory-refactor`. Database format remains backward compatible because V2 fields are stored in the existing JSON memory payload with defaults for legacy records.
