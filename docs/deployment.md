# Deployment Guide

## Deployment Modes

This project is self-contained by default. Docker does not deploy Nocturne Memory automatically.

Default mode:

```text
AI client -> research-memory-gateway MCP -> SQLite
```

Optional future mode:

```text
AI client -> research-memory-gateway MCP -> Nocturne Memory
```

Use SQLite mode first unless you have already deployed Nocturne and know its final MCP/HTTP contract.

## Recommended NAS Deployment

1. Copy this project to the NAS.
2. Copy `config.example.yaml` to `config.yaml`.
3. Set `RESEARCH_MEMORY_TOKEN` in the environment.
4. Use SQLite backend for normal first deployment.
5. Put the service behind Tailscale, ZeroTier, or WireGuard.
6. Configure clients to use `http://<nas-ip>:8787/sse`.

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

## VPS Reverse Proxy Pattern

Recommended flow:

```text
Client -> HTTPS -> Nginx on VPS -> Tailscale/WireGuard -> NAS gateway
```

Nginx should enforce HTTPS and authentication. Do not expose the NAS service directly to the public internet without auth.

## Optional Nocturne Backend

The first implementation includes a complete SQLite backend and a Nocturne adapter boundary. Nocturne is not required for AI clients to call the gateway.

Nocturne is useful only if you want its Dashboard, PostgreSQL deployment, or broader long-term agent-memory features. If you choose to use it, deploy Nocturne separately, then map the adapter methods to Nocturne's `create_memory`, `search_memory`, and `read_memory` tools or HTTP endpoints.

Until then, use:

```yaml
backend:
  type: sqlite
```

This preserves the final data shape and can export JSON for migration into Nocturne later.

## Backup

Recommended schedule:

- Daily: copy `data/research_memory.db` to a timestamped backup.
- Weekly: call `export_memories` with `export_format="both"`.
- Monthly: restore a backup into a test directory and verify search works.
