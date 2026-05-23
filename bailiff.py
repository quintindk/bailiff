"""
Bailiff — The Delegate.

A client-side FastMCP server exposing a single `query_knowledge_base` tool.
The calling agent (Copilot CLI, Claude Desktop, Cursor, etc.) sees this tool
and invokes it when it needs project knowledge. Bailiff forwards the query
to Catchpole's /v1/responses endpoint with an MCP tool block pointing at
Scribe, so LM Studio retrieves and synthesises an answer during inference.

Flow:
    Agent  -- MCP tool: query_knowledge_base(q) -->  Bailiff
    Bailiff -- POST /v1/responses (+ Scribe tool block) -->  Catchpole
    Catchpole -- /v1/responses (with MCP tools) -->  LM Studio
    LM Studio -- mcp_call: search_archives -->  Scribe -->  Qdrant
    LM Studio -- synthesised text -->  Catchpole -->  Bailiff -->  Agent
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx
from fastmcp import FastMCP

LOG = logging.getLogger("bailiff")
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

CATCHPOLE_URL = os.getenv("CATCHPOLE_URL", "http://localhost:4000").rstrip("/")
CATCHPOLE_API_KEY = os.getenv("CATCHPOLE_API_KEY", "")
CATCHPOLE_MODEL = os.getenv("CATCHPOLE_MODEL", "lm_studio/local")

SCRIBE_URL = os.getenv("SCRIBE_URL", "http://localhost:8000/mcp")
SCRIBE_LABEL = os.getenv("SCRIBE_LABEL", "scribe")

REQUEST_TIMEOUT = float(os.getenv("BAILIFF_TIMEOUT", "180"))
SYSTEM_INSTRUCTIONS = os.getenv(
    "BAILIFF_INSTRUCTIONS",
    (
        "You are the Chamberlain knowledge engine. Use the `search_archives` "
        "tool whenever the question requires repository context, code, or "
        "documentation from the Chamberlain project. Always call the tool "
        "before answering questions about the code. Cite file paths in your "
        "synthesised answer."
    ),
)

mcp = FastMCP(
    name="bailiff",
    instructions=(
        "Bailiff is the delegate to the Chamberlain knowledge engine. Call "
        "`query_knowledge_base` with a natural-language question to receive "
        "a synthesised answer backed by the project's code and docs."
    ),
)


def _extract_text(response_json: dict[str, Any]) -> str:
    """Pull the final assistant text out of a /v1/responses payload."""
    if "output_text" in response_json and response_json["output_text"]:
        return response_json["output_text"]
    chunks: list[str] = []
    for item in response_json.get("output", []):
        if item.get("type") == "message":
            content = item.get("content")
            if isinstance(content, str):
                chunks.append(content)
            elif isinstance(content, list):
                for piece in content:
                    text = piece.get("text") if isinstance(piece, dict) else None
                    if text:
                        chunks.append(text)
    if chunks:
        return "\n\n".join(chunks).strip()
    return json.dumps(response_json, indent=2)


@mcp.tool
async def query_knowledge_base(query: str) -> str:
    """Query the unified Chamberlain knowledge base.

    Sends `query` to the Catchpole gateway, which routes to a local model
    that uses the Scribe MCP server to retrieve and synthesise relevant
    chunks from the indexed repositories.

    Args:
        query: Natural-language question about the Chamberlain estate.

    Returns:
        A synthesised markdown answer.
    """
    payload = {
        "model": CATCHPOLE_MODEL,
        "input": query,
        "instructions": SYSTEM_INSTRUCTIONS,
        "tools": [
            {
                "type": "mcp",
                "server_label": SCRIBE_LABEL,
                "server_url": SCRIBE_URL,
                "allowed_tools": ["search_archives"],
            }
        ],
    }
    headers = {"Content-Type": "application/json"}
    if CATCHPOLE_API_KEY:
        headers["Authorization"] = f"Bearer {CATCHPOLE_API_KEY}"

    LOG.info("Forwarding query (%d chars) to Catchpole at %s", len(query), CATCHPOLE_URL)
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        try:
            resp = await client.post(
                f"{CATCHPOLE_URL}/v1/responses",
                headers=headers,
                json=payload,
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            LOG.error("Catchpole error %s: %s", exc.response.status_code, exc.response.text[:500])
            return f"Knowledge base error ({exc.response.status_code}): {exc.response.text[:500]}"
        except Exception as exc:
            LOG.error("Knowledge base call failed: %s", exc)
            return f"Knowledge base unreachable: {exc}"

    answer = _extract_text(resp.json())
    LOG.info("Catchpole returned %d chars", len(answer))
    return answer


if __name__ == "__main__":
    transport = os.getenv("BAILIFF_TRANSPORT", "stdio")
    if transport == "stdio":
        LOG.info("Starting Bailiff (stdio)")
        mcp.run()
    else:
        host = os.getenv("BAILIFF_HOST", "0.0.0.0")
        port = int(os.getenv("BAILIFF_PORT", "8100"))
        path = os.getenv("BAILIFF_PATH", "/mcp" if transport == "http" else "/sse")
        LOG.info("Starting Bailiff (%s) on %s:%s%s", transport, host, port, path)
        mcp.run(transport=transport, host=host, port=port, path=path)
