# Bailiff — The Delegate

Bailiff is the client-side MCP server pillar of [Chamberlain](../README.md). It is the only component the coding agent talks to. It exposes a single tool — `query_knowledge_base` — that fans the question out through the full Chamberlain stack and returns a synthesised, citation-bearing answer.

## Architecture

```
Agent (Copilot CLI / Claude Desktop / Cursor)
   │  MCP tool: query_knowledge_base(query)
   ▼
Bailiff
   │  POST /v1/responses  +  tools=[{type:mcp, server:scribe}]
   ▼
Catchpole  ─►  LM Studio  ─MCP─►  Scribe  ─►  Qdrant
                  ▲                 │
                  └── synthesised ──┘
```

LM Studio's `/v1/responses` endpoint accepts a `tools` array with `{type: "mcp", server_label, server_url}` blocks. During inference the model autonomously calls Scribe, retrieves chunks, and writes a final answer. Bailiff just unwraps the response text and returns it to the agent.

> The OpenAI-compatible `/v1/chat/completions` endpoint silently drops MCP tools. Use `/v1/responses`.

## Tool

| Tool | Description |
| --- | --- |
| `query_knowledge_base(query)` | Ask anything about the Chamberlain estate. Returns a synthesised markdown answer with file path citations. |

## Quick start

### Container (HTTP transport)

```bash
cp .env.example .env
# defaults are sane for a local stack on the same host
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

**Copilot CLI / generic stdio client:**

```json
{
  "mcpServers": {
    "bailiff": {
      "command": "python",
      "args": ["/path/to/chamberlain/bailiff/bailiff.py"]
    }
  }
}
```

**HTTP-based client (Claude Desktop streamable-http, etc.):**

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

| Var | Default | Notes |
| --- | --- | --- |
| `BAILIFF_TRANSPORT` | `stdio` | `stdio` for agent integration, `http` for shared deployments. |
| `BAILIFF_HOST` / `BAILIFF_PORT` / `BAILIFF_PATH` | `0.0.0.0` / `8100` / `/mcp` | HTTP bind. |
| `CATCHPOLE_URL` | `http://localhost:4000` | LiteLLM proxy. |
| `CATCHPOLE_API_KEY` | _empty_ | Master key for Catchpole. |
| `CATCHPOLE_MODEL` | `lm_studio/local` | Model alias to invoke. |
| `SCRIBE_URL` | `http://localhost:8000/mcp` | Embedded in the MCP tool block sent to LM Studio. |
| `SCRIBE_LABEL` | `scribe` | Label passed through to LM Studio. |
| `BAILIFF_TIMEOUT` | `180` | Seconds to wait for Catchpole. |
| `BAILIFF_INSTRUCTIONS` | _see code_ | System instructions for the model. |
| `LOG_LEVEL` | `INFO` | Standard Python log level. |

## Smoke test

```python
import asyncio
from fastmcp import Client

async def main():
    async with Client("http://localhost:8100/mcp") as c:
        r = await c.call_tool("query_knowledge_base", {"query": "What does the _decide function in Catchpole do?"})
        print(r.data)

asyncio.run(main())
```

You should get a synthesised answer that cites `catchpole.py`. Watch the Scribe logs in parallel — you'll see a `CallToolRequest` with the right query.

## See also

- The [Chamberlain Architecture specification](../specification.md).
- [Scribe](../scribe/) — the knowledge MCP server Bailiff references.
- [Catchpole](../catchpole/) — the gateway Bailiff posts to.
- [Miller](../miller/) — the ingester that populates the archive.
