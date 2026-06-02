"""Convert between MCP Tool schemas and OpenAI function-calling format.

MCP ``tools/list`` returns ``mcp.types.Tool`` objects with ``inputSchema``
(JSON Schema).  OpenAI / litellm expect ``{"type": "function", "function":
{"name": ..., "description": ..., "parameters": ...}}``.  This module
provides pure conversion functions between the two.
"""

from __future__ import annotations

from typing import Any


def mcp_tool_to_openai(tool: Any, *, server: str = "") -> dict[str, Any]:
    """Convert a single MCP Tool to OpenAI function-calling dict.

    Args:
        tool: An ``mcp.types.Tool`` instance or a dict with ``name``,
            ``description``, and ``inputSchema`` keys.
        server: Optional MCP server name — prepended to the description
            so the LLM knows which backend provides this tool.

    Returns:
        OpenAI-format tool dict.
    """
    if hasattr(tool, "model_dump"):
        d = tool.model_dump()
    elif hasattr(tool, "dict"):
        d = tool.dict()
    else:
        d = dict(tool)

    name: str = d.get("name", "")
    description: str = d.get("description", "")
    input_schema: dict[str, Any] = d.get("inputSchema", {})

    if server:
        description = f"[{server}] {description}"

    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": input_schema,
        },
    }


def mcp_tools_to_openai_format(tools: list[Any], *, server: str = "") -> list[dict[str, Any]]:
    """Convert a list of MCP Tools to OpenAI function-calling format.

    Args:
        tools: List of ``mcp.types.Tool`` instances or dicts.
        server: Optional MCP server name to annotate descriptions.

    Returns:
        List of OpenAI-format tool dicts ready for litellm.
    """
    return [mcp_tool_to_openai(t, server=server) for t in tools]
