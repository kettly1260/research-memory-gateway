# Research Memory Bridge

A thin Rust client that captures Codex / Claude Code lifecycle events
automatically and uploads them to `research-memory-gateway`.

```text
Agent
  |
  | lifecycle hook / transcript adapter
  v
research-memory-bridge
  |  normalize
  |  redact
  |  durable local spool (SQLite, WAL)
  |  retry (exponential backoff + jitter)
  |  batching
  v
HTTP ingest API
  |
  v
research-memory-gateway
  +-- complete conversation archive
  +-- FTS
  +-- embedding
  +-- retrieval
  +-- MCP HTTP recall
```

* **Write:** `Agent -> bridge -> HTTP ingest -> gateway`
* **Read:** `Agent -> Streamable HTTP MCP -> gateway`

The MCP transport is **not** changed to stdio, and capture never depends on the
model calling a tool.

## Why automatic capture costs no context

MCP is the *read* path: the model calls `conversation_search`,
`conversation_recall`, `conversation_read`, `recall_memory` or `verify_memory`
when it decides it needs history. That is a deliberate, context-consuming
action.

Capture is the *write* path, and it never goes through the model. The agent runs
a configured command at a lifecycle boundary; that command writes one SQLite
transaction and exits. Nothing about the round trip — not the payload, not the
ACK, not an error message — enters the conversation. That is why there is no
"remember to call the memory tool every turn" skill: automatic capture must not
depend on the model's cooperation.

## Scope

The bridge is a client, **not** a second memory server. It deliberately contains
no MCP server, WebUI, embedding, reranker, vector database, LLM, wiki, Obsidian
integration, Research-AI-Hub integration or PostgreSQL.

It owns exactly six jobs: hooks/adapters, normalization, secret redaction, a
durable spool, retry, and batched HTTP upload (plus optional transcript
reconciliation).

It also does **not** participate in retrieval. FTS ranking, vector ranking,
rerank and prompt injection stay in the gateway; no gateway retrieval code is
copied into Rust.

## Install

### 1. Get a binary

Download the archive for your platform from the GitHub release, or build it:

```bash
cd bridge
cargo build --release --locked
```

| Platform | Asset |
|---|---|
| Windows x86_64 | `research-memory-bridge-windows-x86_64.zip` |
| Linux x86_64 | `research-memory-bridge-linux-x86_64.tar.gz` |
| Linux aarch64 | `research-memory-bridge-linux-aarch64.tar.gz` |

### 2. Configure

```bash
research-memory-bridge config init --server-url http://<gateway>:8787
```

The token is **never** written to the config file — only the *name* of the
environment variable that holds it:

```bash
export RESEARCH_MEMORY_TOKEN=...            # Linux / macOS / WSL
setx RESEARCH_MEMORY_TOKEN "..."            # Windows (new shell required)
```

### 3. Install hooks

```bash
research-memory-bridge install-hooks --agent codex
research-memory-bridge install-hooks --agent claude-code
```

### 4. Verify

```bash
research-memory-bridge doctor
```

`doctor` checks the config, the spool, the token, the installed hooks, the
gateway's reachability, whether the token is actually accepted, and whether
ingest is enabled on the gateway.

## Layout

```text
<platform user data dir>/research-memory-bridge/     (%APPDATA% on Windows)
├── config.toml
├── spool.sqlite        (+ -wal / -shm)
├── logs/bridge.log     (rotates at 5 MiB, keeps 2 generations)
└── backups/            (pre-modification copies of agent configs)
```

Override with `--home <dir>` or `RESEARCH_MEMORY_BRIDGE_HOME`. Every path is
resolved through platform APIs; nothing assumes `~`, `/tmp`, `bash` or `chmod`.

## Codex setup

Codex runs the command in `notify` (`~/.codex/config.toml`) when it emits
`agent-turn-complete`, passing the payload **as the last command-line argument**
(not on stdin):

```json
{"type":"agent-turn-complete",
 "thread-id":"...",
 "last-assistant-message":"...",
 "input-messages":["..."]}
```

`install-hooks --agent codex` writes:

```toml
notify = ["C:\\path\\to\\research-memory-bridge.exe", "capture", "--agent", "codex", "--bridge-managed"]
```

Details that matter:

* **Codex supports exactly one `notify` command.** If one is already configured
  (for example a computer-use helper), the installer refuses and tells you to
  re-run with `--force`. With `--force` the original value is backed up to
  `backups/` and restored by `uninstall-hooks`.
* The edit is **surgical**: comments, key order and unrelated tables in
  `config.toml` are preserved byte-for-byte. The bridge never round-trips your
  config through a TOML serializer.
* `--bridge-managed` is an internal marker so detection survives a moved or
  renamed binary and can never claim someone else's hook.

### What Codex does *not* provide

Codex's `notify` currently fires **only** for `agent-turn-complete`. There is no
`SessionStart`, no `SessionEnd`, and no tool lifecycle. Therefore:

* a turn completion is mapped to that turn's messages and is explicitly **not**
  treated as a session end;
* `last-assistant-message` is a *summary*, not necessarily the full reply — the
  transcript reconciliation path supplies the exact text;
* sessions are closed with the documented fallback:

```bash
research-memory-bridge finalize-session \
  --agent codex \
  --session-id <thread-id> \
  --observed-message-count <n>
```

## Claude Code setup

Claude Code runs configured commands on lifecycle events and passes the event
JSON **on stdin**. `install-hooks --agent claude-code` appends to
`~/.claude/settings.json`:

| Event | Mapping |
|---|---|
| `SessionStart` | `session_start` |
| `UserPromptSubmit` | `user_prompt` (anchored on `prompt_id`) |
| `Stop` | `turn_end` — a turn boundary, **not** a session end |
| `SessionEnd` | `session_end` |
| `PreToolUse` / `PostToolUse` | tool telemetry, only with `--with-tools` |

Claude Code hooks *compose*, so installation appends a matcher group and never
replaces anything. Existing hooks, permissions and every other setting are
preserved; `uninstall-hooks` removes only the bridge's own groups.

Two hook-contract details drive the implementation:

* **exit code 2 blocks the agent**, and plain stdout of `UserPromptSubmit` /
  `SessionStart` is injected into the model's context. So `capture` always exits
  `0` and never writes to stdout.
* `Stop` means "Claude finished responding". Treating it as `SessionEnd` would
  wrongly declare a live session finished.

## Transcript reconciliation

Realtime hooks are the low-latency path; transcripts are the
eventual-consistency path.

```bash
research-memory-bridge watch --agent codex            # loop
research-memory-bridge watch --agent claude-code --once
```

* **Codex** — `~/.codex/sessions/<Y>/<M>/<D>/rollout-*.jsonl`, the record shape
  already parsed by this repository's `CodexExportReader`.
* **Claude Code** — `~/.claude/projects/<slug>/<session-id>.jsonl`.

Properties:

* a byte cursor per file, advanced only past the last complete newline, so a
  half-written trailing line is re-read rather than lost;
* the session identity is re-read from the head of the file on every pass, so an
  incremental pass derives exactly the same event ids as a cold pass;
* both paths feed the same spool, so a message captured by both is rejected by
  the primary key rather than archived twice.

Adding another agent means implementing one trait
(`watcher::TranscriptSource`); the watcher itself does not change.

## Security

**Redaction runs on both sides.** The client sanitizer is a defence, not a
guarantee, so the gateway runs a second independent pass before anything reaches
the archive:

```text
client sanitizer -> network -> server sanitizer -> archive
```

Covered: `Authorization` headers, bearer tokens, API keys, common secret env
assignments, password-like assignments, GitHub tokens, OpenAI/Anthropic-style
keys, AWS access keys, JWTs, private key blocks, cookies/session tokens,
connection strings, and URL userinfo.

Precision matters as much as coverage: a value is only redacted when a
secret-looking label or a high-entropy token shape is present, so
`Fe(NO3)3`, `0.1 M HNO3`, `IC50 = 12.5 µM`, `ΔG° = -23.4 kJ/mol` and
`NaCl 0.9%` pass through untouched.

**Tokens.** The token lives only in an environment variable. It is never written
to `config.toml`, never printed by `status`/`doctor`/`config --json`, never
logged, and never included in an error message.

**Fail-closed on safety, fail-open on work.** An invalid server URL, an
unverifiable payload, or an authentication failure is never reported as a
successful ACK — the events stay in the spool and are retried. But a dead
gateway never blocks the agent: `capture` writes locally and exits `0` whatever
happens.

**Boundaries.** There is no DOM scraping, no browser injection, no cookie
extraction. ChatGPT Web has no official local lifecycle interface, so it is out
of scope for the bridge and is handled separately through Data Export import or
a supported connector.

## Operations

```bash
research-memory-bridge status            # local state, no network
research-memory-bridge status --json
research-memory-bridge doctor --json
research-memory-bridge drain --once      # one bounded pass
research-memory-bridge drain --daemon    # resident worker
```

`status` reports the server URL, whether a token is configured, pending /
inflight / acked / dead-letter counts, the age of the oldest pending event, the
last successful upload, the last error, and per-adapter installation state — and
never the token.

`drain --daemon` sleeps until the earliest scheduled retry (bounded by
`drain_interval_seconds`) when there is nothing to do; it never busy-loops.

### Growth bounds

| Mechanism | Effect |
|---|---|
| `spool_retention_days` | ACKed rows are deleted |
| `max_pending_events` | the oldest rows beyond the cap are parked in `dead_letter`, loudly logged, never silently dropped |
| log rotation | `bridge.log` rotates at 5 MiB, two generations |
| `dead_letter` | terminal rows stay inspectable and are never retried |

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `doctor` says `auth: INVALID` | the token in `$RESEARCH_MEMORY_TOKEN` is not accepted. Check the gateway's `server.auth_token_env` or add an `api_keys` row. |
| `doctor` says `gateway_ingest_enabled: !!` | set `conversation_archive.enabled` **and** `conversation_ingest.enabled` to `true` in the gateway config and restart it. |
| Events stay `pending` | the gateway is unreachable or rejecting. `status --json` shows `last_error`; `doctor` shows reachability. Nothing is lost. |
| `dead_letter` grows | look at `status --json` → `dead_letter_sample`. `content_too_large` means a payload exceeded `max_content_chars`; `event_id_conflict` means a client reused an id for different content. |
| Codex hooks not firing | `install-hooks --agent codex` refuses to replace a foreign `notify`. Re-run with `--force` (the original is backed up) — but remember Codex only supports one `notify` command. |
| Claude Code hooks not firing | check `~/.claude/settings.json` contains the bridge's matcher groups; `doctor` reports per-adapter installation state. |
| Duplicate-looking archive entries | the hook's `last-assistant-message` is a *summary*; the transcript supplies the full text, so both legitimately appear. Byte-identical copies collapse automatically. |
| Nothing in `conversation_search` | run `watch --agent <agent> --once` to reconcile the transcript, then search again. Also note that FTS5's `unicode61` tokenizer does not segment CJK — search a token that appears verbatim (`HNO3`), not a Chinese phrase. |

## Development

```bash
cd bridge
cargo fmt --check
cargo clippy --all-targets --all-features -- -D warnings
cargo test
cargo build --release --locked
```

`Cargo.lock` is committed: the CI release build uses `--locked` so the published
binaries are reproducible from the tag.

`schemas/event-id-contract-v1.json` is generated from the Python gateway and
asserted by both suites, which is the only thing keeping the two id derivations
from drifting.
