"""
Bailiff — a client-side MCP server that exposes one `ask` tool.

The calling agent (Copilot CLI, Claude Desktop, Cursor, etc.) invokes `ask`
with a natural-language question. Bailiff forwards it to an upstream that
speaks the OpenAI `/v1/responses` API, attaching an MCP tool block that
points at a remote knowledge MCP server (e.g. Scribe over Qdrant). The
upstream model calls the knowledge tool autonomously during inference and
returns a synthesised answer; Bailiff unwraps it and hands it back to the
agent.

The upstream can be any /v1/responses-compatible host that honours MCP tool
blocks — typically a LiteLLM gateway in front of LM Studio, or LM Studio
direct.

Flow:
    Agent  -- MCP: ask(q) -->  Bailiff
    Bailiff -- POST /v1/responses (+ knowledge tool block) -->  Upstream
    Upstream model -- mcp_call: search -->  Knowledge server -->  Vector DB
    Upstream -- synthesised text -->  Bailiff -->  Agent
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


def _env(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return value if value is not None else default


UPSTREAM_URL = _env("UPSTREAM_URL", "http://localhost:4000").rstrip("/")
UPSTREAM_API_KEY = _env("UPSTREAM_API_KEY")
UPSTREAM_MODEL = _env("UPSTREAM_MODEL", "local")

KNOWLEDGE_URL = _env("KNOWLEDGE_URL", "http://localhost:8000/mcp")
KNOWLEDGE_LABEL = _env("KNOWLEDGE_LABEL", "knowledge")

# Comma-separated list of Scribe tools the upstream model may call.
# Default exposes the full Scribe v1 surface (search + memory + ingest).
# Back-compat: if KNOWLEDGE_TOOL is set, it overrides as a single tool.
_DEFAULT_ALLOWED_TOOLS = (
    "search_archives,list_collections,"
    "recall,remember,forget,list_memories,"
    "ingest_url,ingest_path,forget_collection"
)
_legacy_single = (_env("KNOWLEDGE_TOOL") or "").strip()
_list_env = (_env("KNOWLEDGE_ALLOWED_TOOLS") or "").strip()
_tools_str = _legacy_single or _list_env or _DEFAULT_ALLOWED_TOOLS
KNOWLEDGE_ALLOWED_TOOLS = [t.strip() for t in _tools_str.split(",") if t.strip()]

REQUEST_TIMEOUT = float(os.getenv("BAILIFF_TIMEOUT", "180"))

SYSTEM_PROMPT_FILE = _env("BAILIFF_SYSTEM_PROMPT_FILE", "/app/system_prompt.md")
_DEFAULT_INLINE_INSTRUCTIONS = (
    "You are a knowledge engine. Use the attached Scribe tools to retrieve "
    "context from the indexed archives, recall stored memories when relevant, "
    "and synthesise a single citation-bearing answer. Do not return raw tool "
    "output verbatim."
)


def _load_system_prompt() -> str:
    """Load the upstream system prompt from file, with env-var fallback.

    Priority:
      1. `BAILIFF_INSTRUCTIONS` env var, if set (inline override).
      2. File at `BAILIFF_SYSTEM_PROMPT_FILE`, if it exists.
      3. Built-in minimal default.
    """
    inline = os.getenv("BAILIFF_INSTRUCTIONS")
    if inline:
        LOG.info("System prompt: loaded from BAILIFF_INSTRUCTIONS env var (%d chars)", len(inline))
        return inline
    try:
        text = open(SYSTEM_PROMPT_FILE, encoding="utf-8").read().strip()
        if text:
            LOG.info("System prompt: loaded from %s (%d chars)", SYSTEM_PROMPT_FILE, len(text))
            return text
        LOG.warning("System prompt file %s is empty; using built-in default", SYSTEM_PROMPT_FILE)
    except FileNotFoundError:
        LOG.warning("System prompt file %s not found; using built-in default", SYSTEM_PROMPT_FILE)
    except Exception as exc:
        LOG.warning("Failed to read %s: %s; using built-in default", SYSTEM_PROMPT_FILE, exc)
    return _DEFAULT_INLINE_INSTRUCTIONS


SYSTEM_INSTRUCTIONS = _load_system_prompt()

mcp = FastMCP(
    name="bailiff",
    instructions=(
        "Call `ask` with a natural-language question to receive a synthesised, "
        "citation-bearing answer drawn from the indexed archives."
    ),
)


def _extract_text(response_json: dict[str, Any]) -> str:
    """Pull the final assistant text out of a /v1/responses payload."""
    if response_json.get("output_text"):
        return response_json["output_text"]
    chunks: list[str] = []
    for item in response_json.get("output", []):
        if item.get("type") != "message":
            continue
        content = item.get("content")
        if isinstance(content, str):
            chunks.append(content)
        elif isinstance(content, list):
            for piece in content:
                if isinstance(piece, dict) and piece.get("text"):
                    chunks.append(piece["text"])
    return "\n\n".join(chunks).strip() if chunks else json.dumps(response_json, indent=2)


@mcp.tool
async def ask(query: str) -> str:
    """Ask the knowledge engine a natural-language question and receive a
    synthesised, citation-bearing answer (not raw vector chunks).

    The question is sent to the configured upstream, which delegates retrieval
    to the knowledge MCP server during inference and returns a final answer.
    """
    payload = {
        "model": UPSTREAM_MODEL,
        "input": query,
        "instructions": SYSTEM_INSTRUCTIONS,
        "tools": [
            {
                "type": "mcp",
                "server_label": KNOWLEDGE_LABEL,
                "server_url": KNOWLEDGE_URL,
                "allowed_tools": KNOWLEDGE_ALLOWED_TOOLS,
            }
        ],
    }
    headers = {"Content-Type": "application/json"}
    if UPSTREAM_API_KEY:
        headers["Authorization"] = f"Bearer {UPSTREAM_API_KEY}"

    LOG.info("Forwarding query (%d chars) to %s", len(query), UPSTREAM_URL)
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        try:
            resp = await client.post(f"{UPSTREAM_URL}/v1/responses", headers=headers, json=payload)
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            body = exc.response.text[:500]
            LOG.error("Upstream %s: %s", exc.response.status_code, body)
            return f"Knowledge engine error ({exc.response.status_code}): {body}"
        except Exception as exc:
            LOG.error("Upstream unreachable: %s", exc)
            return f"Knowledge engine unreachable: {exc}"

    answer = _extract_text(resp.json())
    LOG.info("Upstream returned %d chars", len(answer))
    return answer


def main() -> None:
    transport = os.getenv("BAILIFF_TRANSPORT", "stdio")
    if transport == "stdio":
        LOG.info("Starting Bailiff (stdio)")
        mcp.run()
        return
    host = os.getenv("BAILIFF_HOST", "0.0.0.0")
    port = int(os.getenv("BAILIFF_PORT", "8100"))
    path = os.getenv("BAILIFF_PATH", "/mcp" if transport == "http" else "/sse")
    LOG.info("Starting Bailiff (%s) on %s:%s%s", transport, host, port, path)
    mcp.run(transport=transport, host=host, port=port, path=path)


if __name__ == "__main__":
    main()
