# Operations And Client Matrix

## Quick Start

1. Copy `config.example.yaml` to `config.yaml`.
2. Keep the default `backend.type: sqlite` and `retrieval.mode: keyword` for first launch.
3. Start locally with `research-memory-gateway --config config.yaml --transport stdio` or on NAS with `--transport streamable-http --host 0.0.0.0 --port 8787`.
4. Add `prompts/research-memory-system-prompt.md` or install `skills/research-memory-gateway/SKILL.md` in each AI client/agent so save behavior is consistent.
5. Treat this prompt/skill as the cross-agent policy: client-local memory features do not automatically write to the gateway, so each agent must proactively call `propose_save` and ask for confirmation when durable knowledge is produced.

## Client Matrix

| Client | Preferred transport | Example config location | Notes |
|---|---|---|---|
| Kilo | Streamable HTTP `/mcp` when available, otherwise SSE or stdio | `.kilo/` or global Kilo MCP config | Use the bundled skill in `skills/research-memory-gateway/SKILL.md`. |
| Cherry Studio | Remote MCP `/mcp` or legacy SSE `/sse` if supported, otherwise local command | Cherry Studio MCP settings | Embedding/rerank variables belong on the server, not in Cherry. |
| Codex | Remote Streamable HTTP `/mcp`, otherwise local stdio | Codex MCP settings | Reuse the same system prompt or inject the bundled skill text; Codex local memory is not a gateway backend. |
| Generic local MCP | stdio | client-specific JSON | Command: `research-memory-gateway --config <path> --transport stdio`. |
| NAS remote MCP | Streamable HTTP | client-specific remote MCP URL | URL: `http://<nas-ip>:8787/mcp`; set `RESEARCH_MEMORY_TOKEN` for exposed deployments. |

Security rule: when `RESEARCH_MEMORY_TOKEN` is unset, HTTP/SSE loopback requests from `127.0.0.1` or `::1` are allowed for local development. Non-loopback requests must use a configured master token or an active WebUI-created API key.

## Updating From GitHub

The repository tracks the MCP source, `prompts/`, and `skills/`, so `git pull` retrieves new gateway code and client policy assets. Copied client assets do not update themselves; after pulling, sync them into client config directories:

```powershell
cd G:\LLM\memory
git pull
python -m pip install -e .
.\scripts\sync-client-assets.ps1
```

Use `-PromptTargetDir <dir>` for clients that store a copied system prompt outside the repository. Restart the gateway process and any AI client that had already loaded the old skill or prompt. For Docker deployments, rebuild or pull the latest image and restart the container.

## Retrieval Modes

Keyword mode is the default and uses SQLite FTS only. It is appropriate for small and medium personal research memory stores and requires no model services.

Hybrid mode keeps SQLite as the storage backend and adds external embedding/rerank calls. It does not add an external vector database and does not require Nocturne.

## Model Service Templates

OpenAI-compatible embedding services usually work with:

```yaml
EMBEDDING_BASE_URL: "http://<host>:<port>/v1"
EMBEDDING_MODEL: "<embedding-model>"
```

Root-path embedding services can use:

```yaml
EMBEDDING_BASE_URL: "http://<host>:<port>"
```

The gateway tries `/embeddings` first and then `/v1/embeddings` after a 404 when `endpoint_path` is `/embeddings`.

Rerank services should accept:

```json
{"query": "...", "documents": ["..."], "top_n": 10, "model": "..."}
```

Accepted response shapes are documented in `README.md` and `docs/deployment.md`.

Common templates:

```yaml
# OpenAI-compatible, vLLM, TEI, or Jina embedding endpoint with /v1/embeddings
EMBEDDING_BASE_URL: "http://<host>:<port>/v1"
EMBEDDING_MODEL: "<embedding-model>"

# Root-path embedding endpoint where /embeddings is directly available
EMBEDDING_BASE_URL: "http://<host>:<port>"
EMBEDDING_MODEL: "<embedding-model>"

# Ollama OpenAI-compatible embedding endpoint, if enabled by your deployment
EMBEDDING_BASE_URL: "http://<ollama-host>:11434/v1"
EMBEDDING_MODEL: "nomic-embed-text"

# BGE/Jina-style reranker facade accepting query/documents/top_n
RERANK_BASE_URL: "http://<rerank-host>:<port>/v1"
RERANK_MODEL: "<rerank-model>"
```

## Backfill Existing Memories

After enabling embedding, existing memories need a one-time backfill:

```powershell
research-memory-admin backfill-embeddings --config config.yaml --dry-run
research-memory-admin backfill-embeddings --config config.yaml
```

Useful filters:

```powershell
research-memory-admin backfill-embeddings --config config.yaml --project my-project --limit 100
research-memory-admin backfill-embeddings --config config.yaml --memory-type paper_note --force
```

Inside a NAS container:

```bash
docker compose -f docker-compose.nas.yml exec research-memory-gateway research-memory-admin inspect-db --config /app/config.yaml
docker compose -f docker-compose.nas.yml exec research-memory-gateway research-memory-admin backfill-embeddings --config /app/config.yaml --dry-run
```

## Maintenance

Inspect the database:

```powershell
research-memory-admin inspect-db --config config.yaml
```

Audit integrity:

```powershell
research-memory-admin audit-integrity --config config.yaml
research-memory-admin audit-integrity --config config.yaml --repair-fts --repair-orphan-embeddings
```

Use the MCP tools `health`, `retrieval_health`, and `audit_database_integrity` for client-side diagnostics.

## WebUI Operations

The WebUI is optional and disabled by default. When enabled, it runs on port `8788` by default and uses `./data/webui-auth.json` for the single admin password hash, `./data/web_config.yaml` for non-secret runtime settings, and `./data/webui-secrets.json.enc` for encrypted API keys/tokens.

Operational checklist:

- Set `webui.initial_password` only for first bootstrap, then remove it after `webui-auth.json` exists.
- Set `WEBUI_SECRET_KEY` before saving embedding/rerank API keys or Nocturne token.
- Keep `data/` persistent and backed up; it contains the WebUI auth/config/secret stores and SQLite database.
- Do not publish port `8788` directly to the public internet. Use local access, VPN, or an authenticated reverse proxy.
- Use dry-run before WebUI embedding backfill, then keep concurrency within provider limits.
- Treat hard delete as irreversible for the live SQLite/FTS/embedding rows. It does not remove historical backups, export files, or remote Nocturne data.

Security smoke checks after enabling WebUI:

- `GET /admin` redirects to `/admin/login` before login.
- Login sets an HttpOnly `webui_session` cookie.
- `POST`, `PATCH`, and `DELETE` under `/admin` fail without CSRF.
- Effective config masks secrets and omits plaintext secret values.
- HTML forms never place API keys/tokens in `value` attributes.

Visual smoke breakpoints and page checklist are in `docs/webui-visual-smoke.md`.

## When Not To Save Long-Term Memory

Do not save transient conversation phrasing, temporary debugging logs, private credentials, raw unpublished data without explicit consent, or claims that cannot be represented as `claims` plus evidence status. Save durable decisions, literature findings, experimental routes, mechanism hypotheses, reusable project context, and reusable agent/MCP/deployment configuration lessons.

## Unpublished Research Safety

Keep `config.yaml`, `data/`, and `exports/` out of public repositories. Treat unpublished experiments, negative results, compound structures, and manuscript plans as sensitive. Prefer private NAS or VPN access, set `RESEARCH_MEMORY_TOKEN`, and avoid exposing the service directly to the public internet.
