# Deployment Guide

## Recommended NAS Deployment

1. Copy this project to the NAS.
2. Copy `config.example.yaml` to `config.yaml`.
3. Set `RESEARCH_MEMORY_TOKEN` in the environment.
4. Start with SQLite backend for smoke testing.
5. Put the service behind Tailscale, ZeroTier, or WireGuard.
6. Configure clients to use `http://<nas-ip>:8787/sse`.

## Docker Compose

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

## Nocturne Backend

The first implementation includes a SQLite backend for immediate validation and a Nocturne adapter boundary. Keep the gateway as the public MCP endpoint. Once your Nocturne deployment contract is fixed, map the adapter methods to Nocturne's `create_memory`, `search_memory`, and `read_memory` tools or HTTP endpoints.

Until then, use:

```yaml
backend:
  type: sqlite
```

This still preserves the final data shape and can export JSON for migration into Nocturne.

## Backup

Recommended schedule:

- Daily: copy `data/research_memory.db` to a timestamped backup.
- Weekly: call `export_memories` with `export_format="both"`.
- Monthly: restore a backup into a test directory and verify search works.
