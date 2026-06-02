"""MCP Client Manager — unified layer for managing multiple MCP server connections.

Agents are **pure MCP clients** — they connect to MCP servers that are
managed externally (by the user or by the system).  The manager does NOT
spawn, stop, or manage server processes.

Connections are **lazy**: the first ``list_tools()`` or ``call_tool()``
triggers connection.  Startup is instant; unavailable servers are
skipped gracefully.

Supports both **stdio** (subprocess) and **http** (SSE) transports.
Configuration is compatible with Claude Code's ``.mcp.json`` format.

Usage::

    manager = McpClientManager([
        McpServerConfig.http("ida", "http://127.0.0.1:13337/mcp"),
    ])
    # No connection yet — fast startup
    tools = manager.list_tools()   # lazy connect here
    result = await manager.call_tool("get_function", {"addr": "0x1000"})
    await manager.close()
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import structlog

from duckagent.mcp.schema_converter import mcp_tools_to_openai_format

logger = structlog.get_logger()

# ── Config ────────────────────────────────────────────────────────────


@dataclass
class McpServerConfig:
    """Configuration for one MCP server.

    Two transport modes:

    - **stdio**: ``command`` + ``args`` (subprocess, spawned on connect)
    - **http**: ``url`` (SSE / Streamable HTTP, server already running)
    """

    name: str
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    transport: Literal["stdio", "http"] = "http"
    enabled: bool = True

    @classmethod
    def stdio(cls, name: str, command: str, args: list[str] | None = None,
              *, env: dict[str, str] | None = None) -> McpServerConfig:
        return cls(name=name, transport="stdio", command=command,
                    args=args or [], env=dict(env or {}))

    @classmethod
    def http(cls, name: str, url: str, *,
             headers: dict[str, str] | None = None) -> McpServerConfig:
        return cls(name=name, transport="http", url=url,
                    headers=dict(headers or {}))

    @classmethod
    def trace(cls, *, env: dict[str, str] | None = None) -> McpServerConfig:
        """Built-in trace MCP server (stdio, self-contained)."""
        return cls.stdio(
            name="trace", command=sys.executable,
            args=["-m", "duckagent.mcp.servers.trace_server"], env=env,
        )

    @classmethod
    def jadx(cls, *, env: dict[str, str] | None = None) -> McpServerConfig:
        """Built-in JADX MCP server wrapper (stdio, fallback)."""
        return cls.stdio(
            name="jadx", command=sys.executable,
            args=["-m", "duckagent.mcp.servers.jadx_server"], env=env,
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, name: str = "") -> McpServerConfig:
        """Create from a dict (Claude Code ``.mcp.json`` format or DuckAgent JSON).

        Claude Code format::

            {"command": "...", "args": [...], "env": {...}}           # stdio
            {"type": "http", "url": "http://...", "headers": {...}}   # http
        """
        server_name = name or str(data.get("name", ""))
        transport_type = str(data.get("type", data.get("transport", "stdio")))

        if transport_type in ("http", "sse"):
            return cls.http(
                name=server_name,
                url=str(data.get("url", "")),
                headers={str(k): str(v) for k, v in data.get("headers", {}).items()},
            )
        return cls.stdio(
            name=server_name,
            command=str(data.get("command", sys.executable)),
            args=[str(a) for a in data.get("args", [])],
            env={str(k): str(v) for k, v in data.get("env", {}).items()},
        )


# ── .mcp.json loader ──────────────────────────────────────────────────


def load_mcp_json(*paths: str | Path) -> dict[str, McpServerConfig]:
    """Load MCP server configs from ``.mcp.json`` files (Claude Code format).

    Later files override earlier ones for the same server name.
    """
    merged: dict[str, McpServerConfig] = {}
    for p in paths:
        path = Path(p).expanduser()
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text())
            servers = data.get("mcpServers", {})
            for srv_name, srv_data in servers.items():
                merged[srv_name] = McpServerConfig.from_dict(srv_data, name=srv_name)
            logger.debug("mcp_json_loaded", path=str(path), servers=list(servers))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("mcp_json_load_failed", path=str(path), error=str(e))
    return merged


# ── Connection ────────────────────────────────────────────────────────


class McpServerConnection:
    """Lazy connection to one MCP server.

    Connection is established on first use, not at construction time.
    Uses ``contextlib.AsyncExitStack`` to manage nested async context
    managers (transport → session) safely across Python versions.
    """

    def __init__(self, config: McpServerConfig) -> None:
        self.config = config
        self._exit_stack: Any = None
        self._stderr_fh: Any = None
        self._session: Any = None
        self._tools: list[dict[str, Any]] = []
        self._tool_names: set[str] = set()
        self._connected: bool = False
        self._connect_error: str | None = None

    @property
    def tools(self) -> list[dict[str, Any]]:
        return self._tools

    @property
    def tool_names(self) -> set[str]:
        return self._tool_names

    @property
    def is_connected(self) -> bool:
        return self._connected and self._session is not None

    async def ensure_connected(self) -> None:
        """Connect if not already connected.  Idempotent."""
        if self._connected and self._session is not None:
            return
        try:
            await self._connect()
        except Exception as e:
            self._connect_error = str(e)
            logger.warning("mcp_connect_failed", server=self.config.name,
                            transport=self.config.transport, error=str(e))

    async def _connect(self) -> None:
        import contextlib
        from mcp import ClientSession

        self._exit_stack = contextlib.AsyncExitStack()

        if self.config.transport == "http":
            from mcp.client.streamable_http import streamablehttp_client
            read, write, _sid = await self._exit_stack.enter_async_context(
                streamablehttp_client(
                    url=self.config.url,
                    headers=self.config.headers or None,
                ))
        else:
            from mcp.client.stdio import stdio_client, StdioServerParameters
            params = StdioServerParameters(
                command=self.config.command,
                args=self.config.args,
                env={**os.environ, **self.config.env} if self.config.env else None,
            )
            # Suppress MCP server stderr — FastMCP banners/health-check
            # logs go to stderr and would spew into the terminal.
            self._stderr_fh = open(os.devnull, "w")
            read, write = await self._exit_stack.enter_async_context(
                stdio_client(params, errlog=self._stderr_fh))

        self._session = await self._exit_stack.enter_async_context(
            ClientSession(read, write))
        await self._session.initialize()
        self._connected = True
        logger.debug("mcp_connected", server=self.config.name,
                     transport=self.config.transport)

        await self._refresh_tools()

    async def disconnect(self) -> None:
        """Close session and transport.  Does NOT kill the server process."""
        if self._exit_stack is not None:
            try:
                await self._exit_stack.aclose()
            except BaseException:
                pass
            self._exit_stack = None
            self._session = None
        if self._stderr_fh is not None:
            try:
                self._stderr_fh.close()
            except BaseException:
                pass
            self._stderr_fh = None
        self._connected = False
        logger.debug("mcp_disconnected", server=self.config.name)

    async def _refresh_tools(self) -> None:
        if self._session is None:
            return
        result = await self._session.list_tools()
        raw_tools: list[Any] = result.tools if hasattr(result, "tools") else []
        self._tools = mcp_tools_to_openai_format(raw_tools, server=self.config.name)
        self._tool_names = {t["function"]["name"] for t in self._tools}
        logger.debug("mcp_tools_refreshed", server=self.config.name,
                      count=len(self._tools))

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        if not self.is_connected:
            err = self._connect_error or "unknown"
            return f'{{"status": "error", "error": "MCP server not connected: {err}"}}'
        try:
            result = await self._session.call_tool(name, arguments)
        except Exception as exc:
            logger.warning("mcp_tool_call_failed", server=self.config.name,
                            tool=name, error=str(exc))
            return f'{{"status": "error", "error": "{exc}"}}'
        return self._extract_text(result)

    @staticmethod
    def _extract_text(result: Any) -> str:
        if result is None:
            return ""
        if isinstance(result, str):
            return result
        if isinstance(result, bytes):
            return result.decode("utf-8", errors="replace")
        contents: list[Any] = getattr(result, "content", []) or []
        if not contents:
            return str(result)
        texts: list[str] = []
        for block in contents:
            if hasattr(block, "text"):
                texts.append(block.text)
            elif isinstance(block, dict):
                texts.append(str(block.get("text", block.get("type", ""))))
            else:
                texts.append(str(block))
        return "\n".join(texts) if len(texts) != 1 else texts[0]


# ── Manager ───────────────────────────────────────────────────────────


class McpClientManager:
    """Manages MCP server connections as a pure client.

    - Does NOT start/stop server processes.
    - Connections are lazy (first ``list_tools()`` or ``call_tool()``).
    - Unavailable servers are skipped gracefully.
    """

    def __init__(self, configs: list[McpServerConfig]) -> None:
        self._connections: dict[str, McpServerConnection] = {}
        for cfg in configs:
            if cfg.enabled:
                self._connections[cfg.name] = McpServerConnection(cfg)
        self._tool_to_server: dict[str, str] = {}

    # ── Lifecycle ─────────────────────────────────────────────────

    async def connect_all(self) -> None:
        """Pre-connect all servers (optional; called automatically on first use)."""
        if not self._connections:
            return
        await asyncio.gather(
            *(conn.ensure_connected() for conn in self._connections.values()),
            return_exceptions=True,
        )
        self._rebuild_routing_table()

    async def close(self) -> None:
        """Disconnect all sessions."""
        await asyncio.gather(
            *(conn.disconnect() for conn in self._connections.values()),
            return_exceptions=True,
        )
        self._connections.clear()
        self._tool_to_server.clear()

    # ── Tool access ──────────────────────────────────────────────

    def list_tools(self) -> list[dict[str, Any]]:
        """Return cached tools.  Call ``connect_all()`` first for fresh data."""
        all_tools: list[dict[str, Any]] = []
        for conn in self._connections.values():
            all_tools.extend(conn.tools)
        return all_tools

    async def ensure_tools(self) -> list[dict[str, Any]]:
        """Connect all servers (if needed) and return tools."""
        await self.connect_all()
        return self.list_tools()

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """Execute a tool, routing to the correct server."""
        server_name = self._tool_to_server.get(name)
        if server_name is None:
            # Lazy connect + rebuild routing table
            await self.connect_all()
            self._rebuild_routing_table()
            server_name = self._tool_to_server.get(name)

        if server_name is None:
            return f'{{"status": "error", "error": "Unknown tool: {name}"}}'

        conn = self._connections.get(server_name)
        if conn is None:
            return f'{{"status": "error", "error": "MCP server not available: {server_name}"}}'

        return await conn.call_tool(name, arguments)

    # ── Helpers ──────────────────────────────────────────────────

    def get_unavailable_servers(self) -> list[tuple[str, str]]:
        """Return (name, error) pairs for servers that failed to connect."""
        result: list[tuple[str, str]] = []
        for conn in self._connections.values():
            if not conn.is_connected and conn._connect_error:
                result.append((conn.config.name, conn._connect_error))
        return result

    def _rebuild_routing_table(self) -> None:
        self._tool_to_server.clear()
        for conn in self._connections.values():
            for name in conn.tool_names:
                self._tool_to_server[name] = conn.config.name


# ── Built-in registry ─────────────────────────────────────────────────

BUILTIN_MCP_SERVERS: dict[str, McpServerConfig] = {
    "trace": McpServerConfig.trace(),
}
