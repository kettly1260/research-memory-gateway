# Conversation Ingest Protocol (schema_version = 1)

The non-MCP HTTP API that `research-memory-bridge` uses to write captured
conversations into the gateway.

```text
WRITE   Agent -> research-memory-bridge -> HTTP ingest -> gateway
READ    Agent -> Streamable HTTP MCP    -> gateway
```

The two directions are deliberately different transports:

| | Write path | Read path |
|---|---|---|
| Transport | plain HTTP/JSON over the ingest API | MCP Streamable HTTP (`/mcp`) |
| Who triggers it | the agent's lifecycle hook, automatically | the model, when it decides to recall |
| Consumes model context | **no** | yes (the tool call and its result) |
| Auth | `Authorization: Bearer <token>` | same |
| Idempotency | `event_id` primary key | n/a (read-only) |

Keeping capture on HTTP is what makes automatic capture free: nothing about a
hook round-trip enters the model's context window.

## Source of truth

The contract is defined once and mirrored in three places that must agree:

| Artifact | Role |
|---|---|
| [`schemas/conversation-ingest-v1.json`](../schemas/conversation-ingest-v1.json) | language-neutral JSON Schema |
| [`src/research_memory_gateway/ingest/schema.py`](../src/research_memory_gateway/ingest/schema.py) | server-side validation (pydantic) |
| [`bridge/src/event.rs`](../bridge/src/event.rs) | client-side structs (serde) |
| [`schemas/event-id-contract-v1.json`](../schemas/event-id-contract-v1.json) | cross-language id derivation fixture |

`tests/test_conversation_ingest_contract.py` and
`bridge/tests/event_id.rs` both assert the same fixture, so the Python and Rust
implementations cannot drift.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/conversations/events` | one event |
| `POST` | `/api/conversations/events/batch` | up to `max_batch_events` events |
| `POST` | `/api/conversations/session-end` | declare a finished session |
| `POST` | `/api/conversations/snapshot` | reconcile a whole conversation |
| `GET` | `/api/conversations/ingest/stats` | ingest counters |

All of them sit behind the gateway's existing `BearerAuthMiddleware`, so the
master token *and* any `api_keys` row work exactly as they do for MCP. There is
no second authentication system.

When `conversation_ingest.enabled` or `conversation_archive.enabled` is false,
the write endpoints answer `503` with the code `ingest_disabled` (not `404`), so
a client can tell "not configured" apart from "wrong URL".

## Event

```json
{
  "event_id": "rmb1_<64 hex chars>",
  "schema_version": 1,
  "source_system": "codex",
  "source_account_namespace": "<sha256 of an account label>",
  "session_id": "019e3ad1-05d6-7382-972f-0d377e6092c6",
  "conversation_id": "019e3ad1-05d6-7382-972f-0d377e6092c6",
  "thread_id": "019e3ad1-05d6-7382-972f-0d377e6092c6",
  "branch_id": "",
  "message_id": "msg_user_1",
  "turn_id": "turn-1",
  "event_type": "user_prompt",
  "role": "user",
  "content": "之前 Fe 的硝酸溶液怎么配的？",
  "timestamp": "2026-09-30T10:00:00+00:00",
  "metadata": {"adapter": "codex-rollout", "id_source": "provider"}
}
```

### `event_type`

`session_start`, `user_prompt`, `assistant_message`, `system_message`,
`tool_call`, `tool_result`, `turn_end`, `session_end`.

Only `user_prompt`, `assistant_message`, `system_message` and the two tool types
become sections in the archived note. `session_start`, `turn_end` and
`session_end` are stored as ledger telemetry so they cannot dilute conversation
recall.

### `role`

`user`, `assistant`, `system`, `tool`, or `""` for lifecycle events.

### `metadata.id_source`

`provider`, `synthesized` or `content`. It records whether the block's
`message_id` came from the agent or had to be derived.

This matters for convergence. Codex's `notify` hook carries no message ids, so
the realtime path anchors on a synthesized id while the transcript path anchors
on the provider id — the same message, two different `event_id`s. The gateway
therefore drops a **non-provider-identified** block when a
**provider-identified** block with the same role and content fingerprint exists,
so the archive keeps the real provenance without duplication.

## Batch request

```json
{"schema_version": 1, "client_id": "machine-id", "events": [ ... ]}
```

## Response envelope

Every write endpoint answers with the same envelope, whatever the status code:

```json
{
  "schema_version": 1,
  "accepted": ["rmb1_..."],
  "duplicates": ["rmb1_..."],
  "rejected": [
    {"event_id": "rmb1_...", "code": "content_too_large", "message": "..."}
  ],
  "session": {"materialized": true, "note_path": "...", "event_count": 4}
}
```

* `accepted` — newly written to the ledger and the archive.
* `duplicates` — already present with the same content fingerprint. **Not an
  error**: a client whose ACK was lost can simply re-send.
* `rejected` — never an ACK. Each entry carries a stable machine-readable
  `code`; clients must switch on the code, never on the message text.

### Rejection codes

| Code | Meaning | Retry? |
|---|---|---|
| `unsupported_schema_version` | the gateway does not speak this version | no |
| `invalid_payload` | schema validation failed, or a batch exceeded the limit | no |
| `missing_required_field` | e.g. no conversation identifier on a message event | no |
| `invalid_event_type` | not in the canonical vocabulary | no |
| `invalid_role` | not in the canonical vocabulary | no |
| `content_too_large` | exceeds `conversation_ingest.max_content_chars` | no |
| `metadata_too_large` | exceeds 64 KiB | no |
| `event_id_conflict` | same `event_id`, **different** content | no |
| `session_id_mismatch` | reserved for future use | no |
| `ingest_disabled` | the surface is switched off | yes |
| `archive_write_failed` | the note could not be written | yes |
| `internal_error` | unexpected server fault | yes |

### Status codes

| Status | When |
|---|---|
| `200` | accepted and/or duplicates, or a partial success with per-event rejections |
| `400` | the whole request was refused (bad version, oversized batch, empty snapshot, malformed JSON) |
| `401` | missing or invalid bearer token |
| `413` | request body over 32 MiB |
| `503` | ingest disabled |

## Session end

```json
{
  "schema_version": 1,
  "source_system": "codex",
  "session_id": "019e3ad1-...",
  "conversation_id": "019e3ad1-...",
  "observed_message_count": 5,
  "last_message_id": "msg_9",
  "ended_at": "2026-09-30T10:30:00+00:00"
}
```

Response:

```json
{
  "schema_version": 1,
  "stored_message_count": 3,
  "observed_message_count": 5,
  "missing_event_count": 2,
  "reconciliation_required": true,
  "last_message_id": "msg_9",
  "note_path": "..."
}
```

`missing_event_count > 0` is the signal for the client to run a transcript
reconciliation pass. The gateway never invents the missing events itself.

`thread_id` / `branch_id` may be omitted when exactly one session exists for the
conversation; the gateway adopts that one rather than guessing among several.

## Snapshot

```json
{
  "schema_version": 1,
  "source_system": "claude-code",
  "session_id": "claude-1",
  "conversation_id": "claude-1",
  "title": "Fe stock preparation",
  "messages": [
    {"role": "user", "content": "...", "message_id": "m1"},
    {"role": "assistant", "content": "...", "message_id": "m2"}
  ],
  "ended": true
}
```

Snapshot messages go through the *same* idempotency path as realtime events. If
a message omits `event_id`, the server derives it with the shared algorithm, so a
snapshot and the hook that captured the same message collapse into one archived
message. **Clients should send the same `message_id` the realtime adapter uses**
(Claude Code: `promptId` / record `uuid`; Codex: the rollout `payload.id`) —
otherwise the two copies cannot be recognised as the same message.

## Event identity

```
event_id = "rmb1_" + SHA-256(
    "rmg-bridge-event-v1" \0
    derivation_version \0
    schema_version \0
    source_system \0
    source_account_namespace \0
    conversation_id \0
    thread_id \0
    branch_id \0
    (message_id or turn_id) \0
    event_type \0
    content_identity
)

content_identity = SHA-256("rmg-bridge-content-v1" \0 normalize_text(content))
```

`normalize_text` is deliberately conservative: Unicode NFC, CRLF/CR → LF,
per-line trailing whitespace removal, whole-text edge trim. No case folding, no
whitespace collapsing, no punctuation stripping — scientific numbers, formulas
and molecule names must never become collapse-equal.

`message_id` is preferred as the anchor; `turn_id` is the documented fallback.
With both empty the content alone anchors the event, which is stable across
replays but collapses two byte-identical consecutive messages in one turn — so
adapters pass a provider id whenever one exists.

## Limits and retention

| Setting | Default | Effect |
|---|---|---|
| `conversation_ingest.max_batch_events` | 500 | larger batches are refused |
| `conversation_ingest.max_content_chars` | 400000 | oversized events are refused |
| `conversation_ingest.retention_days` | 30 | ACKed events of **ended** sessions are pruned |
| `conversation_ingest.reconcile_on_session_end` | true | report missing events |

Retention prunes the *ledger*, never the archive. The materialized Markdown note
is the durable artifact, and materialization is idempotent (each block carries an
`rmb:ev <event_id>` marker), so a pruned ledger can only cause an append, never a
truncation.

## Redaction

Two independent sanitizers run on every payload:

```text
client sanitizer -> network -> server sanitizer -> archive
```

Neither trusts the other. Both are precision-first: a value is only redacted when
a secret-looking label or a high-entropy token shape is present, so
`Fe(NO3)3`, `0.1 M HNO3`, `IC50 = 12.5 µM` and `NaCl 0.9%` survive untouched.

## Storage layout

| Artifact | Path |
|---|---|
| ingest ledger | `conversation_ingest.state_path` (default `./data/conversation-ingest.sqlite`) |
| archive notes | `<conversation_archive.staging_dir>/bridge/<source_system>/<conversation>.md` |
| FTS index | `conversation_archive.index_path` |

The notes are ordinary conversation-archive Markdown with the same frontmatter
(`source_key`, `canonical_conversation_id`, `source_conversation_id`,
`source_system`, ...), so the existing MCP tools —
`conversation_search`, `conversation_recall`, `conversation_read` — find ingested
content with no special cases.
