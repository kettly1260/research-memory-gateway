# research-memory-bridge

A thin Rust client that captures Codex / Claude Code lifecycle events and uploads
them to `research-memory-gateway`.

```text
Agent
  | lifecycle hook / transcript adapter
  v
research-memory-bridge   normalize -> redact -> durable spool -> retry -> batch
  | HTTP ingest API
  v
research-memory-gateway  archive + FTS + embedding + retrieval
  ^
  | Streamable HTTP MCP (read path)
Agent
```

The bridge is **not** a second memory server. It has no MCP server, no WebUI, no
embedding, no reranker, no vector database, no LLM. Retrieval stays in the
gateway; the bridge only writes.

Full documentation: [`../docs/RESEARCH_MEMORY_BRIDGE.md`](../docs/RESEARCH_MEMORY_BRIDGE.md).

## Quick start

```bash
research-memory-bridge config init --server-url http://<gateway>:8787
research-memory-bridge install-hooks --agent codex
research-memory-bridge install-hooks --agent claude-code
research-memory-bridge doctor
```

Set the token in the environment, never on the command line or in the config file:

```bash
export RESEARCH_MEMORY_TOKEN=...          # Linux / macOS / WSL
setx RESEARCH_MEMORY_TOKEN ...            # Windows
```

## Commands

| Command | Purpose |
|---|---|
| `capture --agent <codex\|claude-code\|generic> [PAYLOAD]` | hot path: spool one hook payload, exit 0 |
| `drain [--once\|--daemon] [--max-batches N]` | upload spooled events |
| `watch --agent <...> [--once]` | reconcile agent transcripts |
| `status [--json]` | local state, no network |
| `doctor [--json]` | config + server + auth + ingest checks |
| `install-hooks --agent <...> [--force] [--with-tools] [--dry-run]` | idempotent hook installation |
| `uninstall-hooks --agent <...>` | remove only what `install-hooks` added |
| `config [--json]` / `config init --server-url <url>` | configuration |
| `finalize-session --agent codex --session-id <id>` | SessionEnd fallback |
| `version` | print version |

## Layout

```text
~/.local/share/research-memory-bridge/     (Linux; %APPDATA% on Windows)
├── config.toml
├── spool.sqlite
├── logs/bridge.log        (+ .1, .2 rotation at 5 MiB)
└── backups/               (pre-modification copies of agent configs)
```

Override with `--home <dir>` or `RESEARCH_MEMORY_BRIDGE_HOME`.

## Development

```bash
cargo fmt --check
cargo clippy --all-targets --all-features -- -D warnings
cargo test
```

`schemas/event-id-contract-v1.json` is shared with the Python gateway: it is
generated from `src/research_memory_gateway/ingest/identity.py` and asserted by
both test suites, so the two id derivations can never drift.
