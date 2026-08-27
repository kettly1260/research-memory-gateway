# V2 Agent Memory Refactor — Baseline and Acceptance Record

## Frozen V1 baseline

- Baseline commit: `7dcdf2c9dceb66e0c415cd3eafcd248d6b3f1539`
- Local legacy branch: `legacy/research-memory-gateway-v1`
- Local tag: `v1-pre-agent-memory-refactor`
- V2 development branch: `main`
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
- 30 Capture natural-language cases.
- a client-neutral scoring script.

The invocation benchmark must be run in real ChatGPT/Codex/Cherry/Kilo clients. Unit tests cannot substitute for model/tool-choice measurements; therefore client recall/capture rates must not be reported as passing until those actual runs are recorded.

The Capture dataset is also executed directly against the default gateway classifier in pytest. All 30 cases currently match the expected capture/ignore decision and Ambient/Trusted tier.

Target gates:

- explicit historical recall rate >= 90%
- top-5 correct memory rate >= 90%
- recall false-positive rate < 10%
- durable capture rate >= 80%
- capture false-positive rate < 10%

## Local verification status

- Full pytest suite after V2 changes: `83 passed`.
- V2 Agent Surface + benchmark code: Ruff check passed.
- Changed frontend files: ESLint passed.
- Frontend TypeScript: `tsc --noEmit` passed.
- `git diff --check`: passed (Windows line-ending conversion warnings only).
- Full-repository Ruff is **not** yet a clean gate: the V1 baseline contains 51 pre-existing style findings (for example legacy `datetime(timezone.utc)`, broad exception handling, and older test typing patterns). These were intentionally not mass-refactored as part of the Agent Memory change.

## Rollback

If V2 behavior regresses, compare against or restore from `legacy/research-memory-gateway-v1` / `v1-pre-agent-memory-refactor`. Database format remains backward compatible because V2 fields are stored in the existing JSON memory payload with defaults for legacy records.
