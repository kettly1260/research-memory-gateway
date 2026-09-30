# Third-Party Notices

## No vendored third-party source

`research-memory-bridge` does **not** vendor, fork or copy source code from any
other agent-memory project. Every adapter is written against the provider's own
documented hook contract and validated against real payloads captured on the
development machine:

| Adapter | Contract source |
|---|---|
| Codex `notify` | Codex `config.toml` `notify` semantics (`agent-turn-complete`, payload appended as the final CLI argument) |
| Codex transcript | `~/.codex/sessions/**/rollout-*.jsonl`, the record shape already parsed by this repository's own `CodexExportReader` |
| Claude Code hooks | The published Claude Code hooks reference (`hook_event_name`, stdin JSON, exit-code contract) |
| Claude Code transcript | `~/.claude/projects/<slug>/<session-id>.jsonl`, verified against real files on disk |

## Design references (no code taken)

The following projects were studied for *design and fixture ideas only*. No code,
schema or fixture was copied from them:

- `akitaonrails/ai-memory` — lifecycle hooks, local spool, hook-drain, retry,
  idempotency, fail-open capture.
- `rohitg00/agentmemory` — multi-agent adapters, connect/install-hooks.
- `carloshpdoc/agent-memory-hub` — transcript adapters.
- `thedotmack/claude-mem` — hook → worker pattern.

Because no source was copied, no third-party license text is reproduced here. If
code is ever taken from one of these projects, its license must be verified as
compatible and its copyright notice reproduced in this file before the change
lands.

## Rust dependencies

The bridge's direct dependencies are all permissively licensed (MIT / Apache-2.0
/ dual). The exact resolved set is pinned in `bridge/Cargo.lock`; run
`cargo license` or `cargo deny check licenses` there for the full inventory.

Notable transitive components:

| Crate | License | Note |
|---|---|---|
| `rusqlite` (feature `bundled`) | MIT | compiles the SQLite amalgamation, which is public domain |
| `reqwest` with `rustls-tls` | MIT / Apache-2.0 | rustls avoids any OpenSSL linkage |
| `unicode-normalization` | MIT / Apache-2.0 | provides the NFC normalization the id derivation depends on |

## Python dependencies

Unchanged from the existing gateway; see `pyproject.toml` and `uv.lock`.
