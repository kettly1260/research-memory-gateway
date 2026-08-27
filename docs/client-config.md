# Client Configuration Examples

## Stdio Mode

Use stdio mode when the client does not support remote SSE or Streamable HTTP.

Normal clients should use the V2 Agent Surface. The server then exposes only `recall_memory`, `capture_memory`, `verify_memory`, and `get_project_state`.

```json
{
  "mcpServers": {
    "research-memory-gateway": {
      "command": "research-memory-gateway",
      "args": ["--config", "G:/LLM/memory/config.yaml", "--transport", "stdio", "--surface", "agent"]
    }
  }
}
```

## Streamable HTTP Mode

The Docker image defaults to Streamable HTTP. Start the gateway on the NAS:

```powershell
research-memory-gateway --config config.yaml --transport streamable-http --surface agent --host 0.0.0.0 --port 8787
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

## ChatGPT Custom App / Workspace Agent Notes

Do **not** point ChatGPT Cloud directly at a NAS Tailscale/private-network address. OpenAI's
current MCP documentation states that ChatGPT connects to remote MCP servers and cannot
directly connect to a local/private/on-premises MCP server. For a gateway that stays on a
NAS, developer machine, or private network, use **Secure MCP Tunnel**.

Recommended topology:

```text
G:/LLM/memory or NAS
        |
        | Streamable HTTP :8787/mcp
        v
Secure MCP Tunnel
        |
        v
ChatGPT custom MCP app
        |
        v
Workspace Agent
```

Start the gateway with `--surface agent` (or keep `server.surface: agent`) so the connected
MCP exposes only the four low-friction memory tools. Keep the gateway private; the tunnel is
the supported bridge rather than exposing the NAS directly to the public Internet.

For ordinary ChatGPT chats, manually selecting a custom app applies to the **single message**
where it is selected, not automatically to the whole conversation. Therefore a normal chat +
manually selected app is not a reliable implementation of "always consider recall on every
turn". Use a **Workspace Agent** with the custom MCP added in its Tools configuration when
you need a repeatable agent whose tool policy is attached to the agent itself.

Do not confuse Workspace Agents with ChatGPT **agent mode**: current OpenAI documentation
states that agent mode does not use custom apps.

Pair the MCP connection with `skills/research-memory-gateway/SKILL.md` or the concise prompt in `prompts/research-memory-system-prompt.md`. The MCP endpoint supplies capabilities; the skill/prompt supplies the proactive recall/capture policy.

For invocation validation, use the natural prompts in `benchmarks/recall_cases.jsonl` and `benchmarks/capture_cases.jsonl`. Do not tell the model to call a specific tool; the benchmark is intended to measure autonomous tool choice.

Current OpenAI references (verify again before deployment because these features evolve):

- https://help.openai.com/en/articles/12584461-developer-mode-apps-and-full-mcp-connectors-in-chatgpt-beta
- https://help.openai.com/en/articles/20001143

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
  "args": ["--config", "G:/LLM/memory/config.yaml", "--transport", "stdio", "--surface", "agent"]
}
```

## Codex Notes

Use the concise V2 system prompt from `prompts/research-memory-system-prompt.md`, or install/inject the bundled skill from `skills/research-memory-gateway/SKILL.md`, so recall and capture triggers are consistent across tools.

Codex local memory options such as `memories`, `generate_memories`, or `use_memories` do not make this MCP a memory backend. They write Codex local memory only. V2 therefore relies on explicit Agent Surface tools: proactive `recall_memory` for prior context and `capture_memory` for durable new information. Trusted research captures are queued by the gateway instead of requiring the Agent to construct `propose_save` payloads itself.

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
args = ["--config", "G:/LLM/memory/config.yaml", "--transport", "stdio", "--surface", "agent"]
```

## Surface Selection

`config.yaml` defaults to:

```yaml
server:
  surface: agent
```

Override per process when needed:

```powershell
# Normal agent: only four low-friction memory tools
research-memory-gateway --config config.yaml --transport stdio --surface agent

# Human/admin automation: legacy management tools only
research-memory-gateway --config config.yaml --transport stdio --surface admin

# Migration/debugging: all tools
research-memory-gateway --config config.yaml --transport stdio --surface full
```

Do not use `full` as the default Agent integration: a large management tool surface recreates the tool-choice problem V2 is intended to solve.

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
