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
