# Deployment Guide

## Deployment Modes

This project is self-contained by default. Docker does not deploy Nocturne Memory. If you already have embedding and rerank models, keep SQLite as the storage backend and enable hybrid retrieval.

Default mode:

```text
AI client -> research-memory-gateway MCP -> SQLite
```

Recommended enhanced mode:

```text
AI client -> research-memory-gateway MCP -> SQLite
                                     |-> embedding model
                                     |-> rerank model
```

Use SQLite mode first. Add embedding/rerank only for better retrieval quality.

## Recommended NAS Deployment

1. Copy this project to the NAS.
2. Copy `config.example.yaml` to `config.yaml`.
3. Set `RESEARCH_MEMORY_TOKEN` in the environment.
4. Use SQLite backend for normal first deployment.
5. Optional: set `retrieval.mode=hybrid` and provide embedding/rerank service URLs.
6. Put the service behind Tailscale, ZeroTier, or WireGuard.
7. Configure clients to use `http://<nas-ip>:8787/sse`.

## Docker Compose

Published GHCR image for NAS:

```powershell
Copy-Item config.example.yaml config.yaml
docker compose -f docker-compose.nas.yml pull
docker compose -f docker-compose.nas.yml up -d
```

Local build:

```powershell
Copy-Item config.example.yaml config.yaml
docker compose up -d --build
```

The compose file mounts:

- `./data` for SQLite runtime data.
- `./exports` for Markdown and JSON exports.
- `./config.yaml` as read-only config.

WebUI is not exposed by default. If you enable `webui.enabled: true`, explicitly publish port `8788` only on trusted interfaces or behind VPN/authenticated reverse proxy. Persist `./data` because it contains `webui-auth.json`, `web_config.yaml`, and `webui-secrets.json.enc`.

## Optional WebUI Admin Console

WebUI runs in the same container on a separate port, default `8788`, and is disabled by default. It is a single-admin private console for memory CRUD, lifecycle state changes, runtime retrieval config, API key/token storage, JSON import/export, audit inspection, and controlled embedding backfill.

Enable it only after setting an initial password:

```yaml
webui:
  enabled: true
  host: 0.0.0.0
  port: 8788
  initial_password: "replace-this-before-first-start"
```

On first start, the password is hashed into `./data/webui-auth.json`. After confirming login, remove `initial_password` from `config.yaml`; the plaintext value is not needed again. Do not set a default password in shared compose files.

Set `WEBUI_SECRET_KEY` before saving provider secrets from the WebUI:

```yaml
environment:
  WEBUI_SECRET_KEY: "<long-random-secret>"
```

Non-secret runtime settings are written atomically to `./data/web_config.yaml`. API keys and Nocturne tokens are encrypted into `./data/webui-secrets.json.enc`; secrets are masked in responses and are not exported. Environment variables remain the highest-precedence override, so the WebUI shows when a saved value is shadowed by env.

The WebUI performs CSRF checks on write operations, uses fixed-expiry HttpOnly sessions, and sends a basic CSP that only allows local resources. All frontend assets are vendored locally; no CDN is required.

Embedding backfill should be run with dry-run first. Keep concurrency low when using paid or rate-limited model APIs. Only one WebUI backfill job can run at a time.

Nocturne v1 support is reserved/test-only: save connection config, encrypted token, and perform status/capabilities probes. It does not sync, import, write, dual-write, or make Nocturne the default backend.

## Optional Embedding And Rerank

Use this when embedding/rerank services are already deployed elsewhere:

```yaml
retrieval:
  mode: hybrid
  embedding:
    enabled: true
    timeout_seconds: 30
    max_retries: 1
  rerank:
    enabled: true
    timeout_seconds: 30
    max_retries: 1
```

Then set environment variables:

```yaml
environment:
  EMBEDDING_BASE_URL: "http://<embedding-host>:<port>/v1"
  EMBEDDING_MODEL: "<embedding-model>"
  RERANK_BASE_URL: "http://<rerank-host>:<port>/v1"
  RERANK_MODEL: "<rerank-model>"
```

Embedding uses OpenAI-compatible responses by default: `{"data": [{"embedding": [...]}]}` and direct `{"embedding": [...]}` are both accepted. `EMBEDDING_BASE_URL` can be either a server root or a `/v1` URL; if `/embeddings` returns 404, the gateway retries `/v1/embeddings`.

Rerank uses `/rerank` with `query`, `documents`, `top_n`, and optional `model` fields. Supported response shapes include `results[].index/relevance_score`, `results[].document.index/score`, and `data[].index/score`.

Failure behavior is intentionally conservative. If embedding is unavailable during search, the gateway falls back to SQLite FTS. If embedding is unavailable during save, the memory is still saved and an existing vector is preserved. If rerank is unavailable, the gateway returns the un-reranked hybrid merge. Use the `retrieval_health` MCP tool to inspect recent embedding/rerank errors, HTTP status codes, vector counts, and stored vector dimensions.

## VPS Reverse Proxy Pattern

Recommended flow:

```text
Client -> HTTPS -> Nginx on VPS -> Tailscale/WireGuard -> NAS gateway
```

Nginx should enforce HTTPS and authentication. Do not expose the NAS service directly to the public internet without auth.

## Optional Nocturne Backend Boundary

The first implementation includes a complete SQLite backend and a Nocturne adapter boundary. Nocturne is not deployed and is not required for AI clients to call the gateway.

Nocturne is useful only if you want its Dashboard, PostgreSQL deployment, or broader long-term agent-memory features. If you choose to use it, deploy Nocturne separately, then map the adapter methods to Nocturne's `create_memory`, `search_memory`, and `read_memory` tools or HTTP endpoints.

For your current deployment, use:

```yaml
backend:
  type: sqlite
```

This preserves the final data shape and can export JSON for migration later if you decide to deploy another memory backend.

## Backup

Recommended schedule:

- Daily: copy `data/research_memory.db` to a timestamped backup.
- Weekly: call `export_memories` with `export_format="both"`.
- Monthly: restore a backup into a test directory and verify search works.

## Admin CLI

Use `research-memory-admin` for maintenance:

```powershell
research-memory-admin inspect-db --config config.yaml
research-memory-admin backfill-embeddings --config config.yaml --dry-run
research-memory-admin audit-integrity --config config.yaml --repair-fts --repair-orphan-embeddings
```

In Docker:

```bash
docker compose -f docker-compose.nas.yml exec research-memory-gateway research-memory-admin inspect-db --config /app/config.yaml
```

See `docs/operations.md` for client matrix, model-service templates, and unpublished research safety guidance.
