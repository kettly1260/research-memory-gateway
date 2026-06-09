# ADR 001: Nocturne Backend Boundary

## Status

Proposed. No production Nocturne adapter is implemented by this ADR.

## Context

`NocturneMemoryBackend` is currently an explicit placeholder. The gateway already has a complete SQLite backend, strong research-memory schema, source references, exports, retrieval diagnostics, and lifecycle tools. Nocturne may be useful for a dashboard, PostgreSQL-backed agent memory, or broader long-term-memory workflows, but the deployed Nocturne contract is not yet known.

Required user-provided information before implementation:

- Nocturne URL and transport: stdio, SSE, Streamable HTTP, REST, or reverse-proxied MCP.
- Tool or endpoint names for create, search, read, update, and delete.
- Authentication mechanism and header format.
- Namespace/project mapping and whether source refs are first-class fields.
- Stable URI scheme for saved memories.
- Error response shape, pagination, and search result scoring semantics.

## Options

### Option 1: Direct MCP Tool Proxy

Map `save`, `search`, `list_all`, and `get` to Nocturne MCP tools. Store each `ResearchMemory` as a JSON document in Nocturne metadata or body.

Advantages: closest to Nocturne's native interface and can preserve tool-level semantics.

Risks: requires a stable remote MCP client inside the gateway and exact tool names. Pagination, update, and delete behavior may differ across Nocturne deployments.

### Option 2: HTTP Adapter To A Nocturne REST Facade

Ask the user to expose or build a REST facade with endpoints such as `POST /memories`, `POST /search`, `GET /memories/{id}`, and `PATCH /memories/{id}`.

Advantages: easiest to test, monitor, secure, and retry from this gateway.

Risks: requires a facade that may not exist yet. Data mapping can diverge from Nocturne-native memory structure.

### Option 3: SQLite Primary With Nocturne Export/Import Sync

Keep SQLite as the gateway backend and periodically export JSON to Nocturne or import selected Nocturne memories into SQLite.

Advantages: preserves the current reliable default and avoids coupling live MCP calls to Nocturne availability.

Risks: not real-time. Conflict resolution and tombstone handling need a sync policy.

## Recommendation

Prefer Option 2 if the user can provide a stable HTTP contract. Otherwise use Option 3 as a migration/sync path. Do not implement Option 1 until the Nocturne MCP transport and tool names are confirmed.

## Data Mapping

`ResearchMemory` should be stored losslessly as JSON. Nocturne search text should include `project`, `topic`, `memory_type`, `title`, `summary`, tags, entity names, relation labels, and claim text. `source_refs`, `evidence`, and claim verification status must be preserved exactly.

Recommended Nocturne metadata keys:

- `gateway_memory_id`
- `project`
- `memory_type`
- `topic`
- `verification_statuses`
- `source_ref_count`
- `schema_version`

## Fallback Policy

Do not silently write to SQLite as a Nocturne fallback unless the user explicitly opts into a cache/sync mode. Silent dual-write can create divergent sources of truth. If Nocturne is selected and unavailable, fail writes with a diagnostic error; reads may use an explicitly documented SQLite cache only if configured.

## Decision Constraints

SQLite remains the default backend. Nocturne must not become the default path. No destructive Nocturne operation should be exposed without `user_confirmed=true` and an explicit contract for how Nocturne represents tombstones, superseded memories, and retractions.
