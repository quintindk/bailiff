# Bailiff

Bailiff is a tiny client-side MCP server that exposes a single `ask` tool to coding agents (Copilot CLI, Claude Desktop, Cursor, etc.). The agent asks a natural-language question; Bailiff delegates retrieval and synthesis to an upstream model that itself has an MCP knowledge tool attached. The agent receives a finished, citation-bearing answer — never raw vector chunks — saving its own context budget.

## How it works

```
Agent (Copilot CLI / Claude Desktop / Cursor)
   │  MCP: ask(query)
   ▼
Bailiff
   │  POST /v1/responses + tools=[{type:mcp, server_url:<knowledge>}]
   ▼
Upstream (LiteLLM gateway, LM Studio, or anything /v1/responses-compatible)
   │  model autonomously calls the knowledge MCP server during inference
   ▼
Knowledge MCP server  ─►  Vector DB
   │
   └── synthesised answer ──► back up the chain
```

Bailiff itself is ~140 lines of Python. It does three things:

1. Receives the `ask(query)` MCP call.
2. POSTs `query` to `${UPSTREAM_URL}/v1/responses` with a `tools` array containing one `{type:"mcp"}` block pointing at `${KNOWLEDGE_URL}`.
3. Unwraps the `output_text` and returns it.

> The OpenAI-compatible `/v1/chat/completions` endpoint silently drops MCP tool blocks. Use `/v1/responses`.

## Tool

| Tool | Description |
| --- | --- |
| `ask(query)` | Ask a natural-language question. Returns a synthesised markdown answer with file path citations. |

## Quick start

### Container (HTTP transport)

```bash
cp .env.example .env
# edit .env for your upstream + knowledge server, then:
docker compose up -d
docker compose logs -f bailiff
```

Bailiff listens on `http://localhost:8100/mcp`.

### Local stdio (for direct agent integration)

```bash
cp .env.example .env
# edit .env: BAILIFF_TRANSPORT=stdio
pip install -r requirements.txt
python bailiff.py
```

### Agent wiring

**Stdio client (Copilot CLI, generic MCP):**

```json
{
  "mcpServers": {
    "bailiff": {
      "command": "python",
      "args": ["/path/to/bailiff/bailiff.py"]
    }
  }
}
```

**HTTP client (Claude Desktop streamable-http, etc.):**

```json
{
  "mcpServers": {
    "bailiff": {
      "url": "http://localhost:8100/mcp"
    }
  }
}
```

## Configuration

All settings come from environment variables.

| Var | Default | Notes |
| --- | --- | --- |
| `BAILIFF_TRANSPORT` | `stdio` | `stdio` for agent integration, `http` for shared deployments. |
| `BAILIFF_HOST` / `BAILIFF_PORT` / `BAILIFF_PATH` | `0.0.0.0` / `8100` / `/mcp` | HTTP bind. |
| `UPSTREAM_URL` | `http://localhost:4000` | Any host exposing OpenAI `/v1/responses` with MCP tool support. |
| `UPSTREAM_API_KEY` | _empty_ | Bearer token for the upstream, if required. |
| `UPSTREAM_MODEL` | `local` | Model identifier the upstream expects. |
| `KNOWLEDGE_URL` | `http://localhost:8000/mcp` | URL of the knowledge MCP server, sent in the tool block. Must be reachable *from the upstream*, not from Bailiff. |
| `KNOWLEDGE_LABEL` | `knowledge` | Server label inside the tool block. |
| `KNOWLEDGE_TOOL` | `search_archives` | Name of the retrieval tool the model is allowed to call. |
| `BAILIFF_TIMEOUT` | `180` | Seconds to wait for the upstream. |
| `BAILIFF_INSTRUCTIONS` | _see code_ | System instructions handed to the upstream model. |
| `LOG_LEVEL` | `INFO` | Standard Python log level. |

> `KNOWLEDGE_URL` is fetched by the upstream, not by Bailiff. If the upstream runs on a different host (e.g. LM Studio on Windows, Bailiff on WSL), this URL must resolve from the upstream's network namespace.

## Smoke test

```python
import asyncio
from fastmcp import Client

async def main():
    async with Client("http://localhost:8100/mcp") as c:
        r = await c.call_tool("ask", {"query": "What's in the indexed archives?"})
        print(r.data)

asyncio.run(main())
```

## Requirements

- Python 3.10+
- `fastmcp >= 2.0`
- `httpx >= 0.27`
- An upstream that speaks OpenAI `/v1/responses` and honours `{type:"mcp"}` tool blocks (LM Studio recent builds, LiteLLM proxies, etc.).
- An MCP server exposing a retrieval tool over the upstream's network.
