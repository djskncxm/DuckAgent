"""MCP (Model Context Protocol) integration for DuckAgent tools.

Provides a unified layer for managing tool connections via MCP:

- :class:`McpClientManager` — manages multiple MCP server connections
  (stdio + HTTP), provides ``list_tools()`` and ``call_tool()``
- :class:`McpServerConfig` — server config with ``.stdio()``, ``.http()``,
  ``.from_dict()`` factories; compatible with Claude Code ``.mcp.json``
- :func:`load_mcp_json` — load server configs from ``.mcp.json`` files
- :func:`mcp_tools_to_openai_format` — MCP Tool schemas → litellm format
"""

from duckagent.mcp.client_manager import (
    BUILTIN_MCP_SERVERS,
    McpClientManager,
    McpServerConfig,
    McpServerConnection,
    load_mcp_json,
)
from duckagent.mcp.schema_converter import mcp_tool_to_openai, mcp_tools_to_openai_format

__all__ = [
    "BUILTIN_MCP_SERVERS",
    "load_mcp_json",
    "McpClientManager",
    "McpServerConfig",
    "McpServerConnection",
    "mcp_tool_to_openai",
    "mcp_tools_to_openai_format",
]
