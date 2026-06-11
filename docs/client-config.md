# Client Configuration Examples

## Stdio Mode

Use stdio mode when the client does not support remote SSE or Streamable HTTP.

```json
{
  "mcpServers": {
    "research-memory-gateway": {
      "command": "research-memory-gateway",
      "args": ["--config", "G:/LLM/memory/config.yaml", "--transport", "stdio"]
    }
  }
}
```

## Streamable HTTP Mode

The Docker image defaults to Streamable HTTP. Start the gateway on the NAS:

```powershell
research-memory-gateway --config config.yaml --transport streamable-http --host 0.0.0.0 --port 8787
```

Then point clients that support remote Streamable HTTP MCP to:

```text
http://<nas-tailscale-ip>:8787/mcp
```

Set `RESEARCH_MEMORY_TOKEN` and configure the client to send:

```text
Authorization: Bearer <token>
```

When `RESEARCH_MEMORY_TOKEN` is unset, only loopback HTTP/SSE clients from `127.0.0.1` or `::1` are allowed without a token.

## Legacy SSE Mode

Start the gateway on the NAS:

```powershell
research-memory-gateway --config config.yaml --transport sse --host 0.0.0.0 --port 8787
```

Then point clients that support remote MCP to:

```text
http://<nas-tailscale-ip>:8787/sse
```

If exposing through a VPS reverse proxy, use HTTPS and Bearer auth at the proxy layer:

```text
https://memory.example.com/sse
```

## KiloCode Notes

Prefer remote Streamable HTTP if your client version supports it. Example remote endpoint:

```json
{
  "mcpServers": {
    "research-memory-gateway": {
      "type": "remote",
      "url": "http://<nas-tailscale-ip>:8787/mcp",
      "headers": {
        "Authorization": "Bearer <token>"
      }
    }
  }
}
```

If your Kilo version only supports stdio, use the stdio example above. If it supports legacy SSE but not Streamable HTTP, start the server with `--transport sse` or `--transport both` and use `/sse`.

## Cherry Studio Notes

If Cherry Studio supports remote MCP, configure the remote URL as `http://<nas-tailscale-ip>:8787/mcp` and add an Authorization Bearer header when `RESEARCH_MEMORY_TOKEN` is set. If it only supports SSE, run the gateway with `--transport sse` or `--transport both` and use `http://<nas-tailscale-ip>:8787/sse`. If it only supports local command MCP, use:

```json
{
  "command": "research-memory-gateway",
  "args": ["--config", "G:/LLM/memory/config.yaml", "--transport", "stdio"]
}
```

## Codex Notes

Use the same system prompt from `prompts/research-memory-system-prompt.md`, or inject the bundled skill text from `skills/research-memory-gateway/SKILL.md`, so save suggestions are consistent across tools.

Codex local memory options such as `memories`, `generate_memories`, or `use_memories` do not make this MCP a memory backend. They write Codex local memory only. To persist durable knowledge in this gateway, the agent must proactively call `propose_save`, ask for user confirmation, and only then call `save_research_memory` with `user_confirmed=true`.

For remote-capable Codex clients, prefer:

```toml
[mcp_servers.research-memory-gateway]
type = "remote"
url = "http://<nas-tailscale-ip>:8787/mcp"

[mcp_servers.research-memory-gateway.headers]
Authorization = "Bearer <token>"
```

For local stdio:

```toml
[mcp_servers.research-memory-gateway]
command = "research-memory-gateway"
args = ["--config", "G:/LLM/memory/config.yaml", "--transport", "stdio"]
```

## Retrieval Mode Notes

Client configuration is independent from retrieval mode. MCP clients call the same tools in both modes; `retrieval.mode` is a server-side setting in `config.yaml`.

Default keyword mode uses only SQLite FTS:

```yaml
backend:
  type: sqlite
retrieval:
  mode: keyword
```

Hybrid mode still keeps SQLite as the storage backend and adds optional external model calls:

```yaml
backend:
  type: sqlite
retrieval:
  mode: hybrid
  embedding:
    enabled: true
  rerank:
    enabled: true
```

Set `EMBEDDING_BASE_URL`, `EMBEDDING_MODEL`, `RERANK_BASE_URL`, and `RERANK_MODEL` in the server environment or Docker Compose file, not in the client MCP JSON. Use the `retrieval_health` tool from any connected client to verify whether hybrid retrieval is active, falling back, or missing model configuration.

## WebUI Notes

The WebUI is not an MCP endpoint and should not be configured in AI clients. It is an optional browser admin console on a separate port, default `8788`, for the human administrator.

Keep client MCP URLs pointed at port `8787`:

```text
http://<nas-tailscale-ip>:8787/mcp
```

Open the WebUI only from trusted networks or through your authenticated admin proxy:

```text
http://<nas-tailscale-ip>:8788/admin
```

Runtime retrieval changes made in WebUI take effect server-side for subsequent MCP searches and saves. If an environment variable such as `EMBEDDING_BASE_URL` is set, it overrides the corresponding WebUI-saved value.
