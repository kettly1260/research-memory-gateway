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

## Optional Embedding And Rerank

Use this when embedding/rerank services are already deployed elsewhere:

```yaml
retrieval:
  mode: hybrid
  embedding:
    enabled: true
  rerank:
    enabled: true
```

Then set environment variables:

```yaml
environment:
  EMBEDDING_BASE_URL: "http://<embedding-host>:<port>/v1"
  EMBEDDING_MODEL: "<embedding-model>"
  RERANK_BASE_URL: "http://<rerank-host>:<port>/v1"
  RERANK_MODEL: "<rerank-model>"
```

Embedding uses OpenAI-compatible `/embeddings` by default. Rerank uses `/rerank` with `query`, `documents`, `top_n`, and optional `model` fields. If either service fails, the gateway falls back to SQLite keyword search.

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
