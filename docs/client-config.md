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

## SSE Mode

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

Prefer remote SSE if your client version supports it. If not, run the gateway with `--transport stdio` locally, or create a small local stdio-to-SSE proxy that forwards to the NAS service.

## Cherry Studio Notes

If Cherry Studio supports remote MCP/SSE, connect directly to the NAS URL. If it only supports local command MCP, use stdio mode or a local proxy.

## Codex Notes

Use the same system prompt from `prompts/research-memory-system-prompt.md` so save suggestions are consistent across tools.

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
http://<nas-tailscale-ip>:8787/sse
```

Open the WebUI only from trusted networks or through your authenticated admin proxy:

```text
http://<nas-tailscale-ip>:8788/admin
```

Runtime retrieval changes made in WebUI take effect server-side for subsequent MCP searches and saves. If an environment variable such as `EMBEDDING_BASE_URL` is set, it overrides the corresponding WebUI-saved value.
