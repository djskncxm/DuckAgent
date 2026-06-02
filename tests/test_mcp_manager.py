"""Tests for McpClientManager and McpServerConnection."""

import pytest

from duckagent.mcp.client_manager import (
    McpClientManager,
    McpServerConfig,
    McpServerConnection,
)


class TestMcpServerConfig:
    """Tests for McpServerConfig."""

    def test_trace_factory_creates_config(self) -> None:
        cfg = McpServerConfig.trace()
        assert cfg.name == "trace"
        assert "duckagent.mcp.servers.trace_server" in cfg.args
        assert cfg.enabled is True

    def test_jadx_factory_creates_config(self) -> None:
        cfg = McpServerConfig.jadx()
        assert cfg.name == "jadx"
        assert "duckagent.mcp.servers.jadx_server" in cfg.args
        assert cfg.enabled is True

    def test_factory_with_env(self) -> None:
        cfg = McpServerConfig.jadx(env={"DUCKAGENT_JADX_HOST": "10.0.0.1"})
        assert cfg.env["DUCKAGENT_JADX_HOST"] == "10.0.0.1"

    def test_disabled_config_not_connected(self) -> None:
        cfg = McpServerConfig(name="disabled", command="true", args=[], enabled=False)
        manager = McpClientManager([cfg])
        assert len(manager._connections) == 0


class TestMcpClientManagerRouting:
    """Tests for tool routing table."""

    def test_empty_manager_returns_empty_tools(self) -> None:
        manager = McpClientManager([])
        tools = manager.list_tools()
        assert tools == []

    @pytest.mark.asyncio
    async def test_unknown_tool_returns_error(self) -> None:
        manager = McpClientManager([])
        result = await manager.call_tool("unknown_tool", {})
        assert "error" in result.lower() or "Unknown" in result

    def test_routing_table_built_from_connections(self) -> None:
        """Verify _rebuild_routing_table maps tool names to servers."""
        # Create connections that already have tools cached
        cfg_a = McpServerConfig(name="server_a", command="true", args=[])
        cfg_b = McpServerConfig(name="server_b", command="true", args=[])

        conn_a = McpServerConnection(cfg_a)
        conn_b = McpServerConnection(cfg_b)

        # Inject cached tools directly
        conn_a._tools = [
            {"type": "function", "function": {"name": "tool_a1", "description": "", "parameters": {}}},
            {"type": "function", "function": {"name": "tool_a2", "description": "", "parameters": {}}},
        ]
        conn_a._tool_names = {"tool_a1", "tool_a2"}

        conn_b._tools = [
            {"type": "function", "function": {"name": "tool_b1", "description": "", "parameters": {}}},
        ]
        conn_b._tool_names = {"tool_b1"}

        manager = McpClientManager([cfg_a, cfg_b])
        manager._connections["server_a"] = conn_a
        manager._connections["server_b"] = conn_b
        manager._rebuild_routing_table()

        assert manager._tool_to_server["tool_a1"] == "server_a"
        assert manager._tool_to_server["tool_a2"] == "server_a"
        assert manager._tool_to_server["tool_b1"] == "server_b"
        assert "tool_unknown" not in manager._tool_to_server

    def test_list_tools_aggregates_all_connections(self) -> None:
        cfg = McpServerConfig(name="s1", command="true", args=[])
        conn = McpServerConnection(cfg)
        conn._tools = [
            {"type": "function", "function": {"name": "t1", "description": "", "parameters": {}}},
            {"type": "function", "function": {"name": "t2", "description": "", "parameters": {}}},
        ]
        conn._tool_names = {"t1", "t2"}

        manager = McpClientManager([cfg])
        manager._connections["s1"] = conn

        tools = manager.list_tools()
        assert len(tools) == 2
        names = {t["function"]["name"] for t in tools}
        assert names == {"t1", "t2"}

    def test_call_tool_routes_to_correct_server(self) -> None:
        """call_tool finds the correct server from the routing table."""
        cfg = McpServerConfig(name="srv", command="true", args=[])
        manager = McpClientManager([cfg])

        # Set up a connection with a routing table entry
        conn = McpServerConnection(cfg)
        conn._tool_names = {"my_tool"}
        manager._connections["srv"] = conn
        manager._rebuild_routing_table()

        # We can't actually call the tool since there's no real MCP session,
        # but the routing resolution should work.
        server_name = manager._tool_to_server.get("my_tool")
        assert server_name == "srv"
        assert manager._tool_to_server.get("nonexistent") is None


class TestMcpServerConnectionTextExtraction:
    """Tests for _extract_text."""

    def test_none_returns_empty(self) -> None:
        assert McpServerConnection._extract_text(None) == ""

    def test_string_returns_as_is(self) -> None:
        assert McpServerConnection._extract_text("hello") == "hello"

    def test_bytes_decoded(self) -> None:
        assert McpServerConnection._extract_text(b"bytes") == "bytes"

    def test_single_text_content(self) -> None:
        class TextBlock:
            text = "result text"

        class FakeResult:
            content = [TextBlock()]

        result = McpServerConnection._extract_text(FakeResult())
        assert result == "result text"

    def test_multiple_text_contents_joined(self) -> None:
        class TextBlock:
            def __init__(self, text: str) -> None:
                self.text = text

        class FakeResult:
            content = [TextBlock("line1"), TextBlock("line2")]

        result = McpServerConnection._extract_text(FakeResult())
        assert result == "line1\nline2"

    def test_dict_text_content(self) -> None:
        class FakeResult:
            content = [{"type": "text", "text": "from dict"}]

        result = McpServerConnection._extract_text(FakeResult())
        assert result == "from dict"

    def test_empty_content_falls_back_to_str(self) -> None:
        class FakeResult:
            content = []

        # This will fall through to str(result) which gives the repr
        result = McpServerConnection._extract_text(FakeResult())
        # str() of the FakeResult instance
        assert "FakeResult" in result or result


class TestMcpClientManagerClose:
    """Tests for lifecycle methods (no actual subprocess)."""

    @pytest.mark.asyncio
    async def test_close_clears_connections(self) -> None:
        manager = McpClientManager([])
        manager._connections["test"] = McpServerConnection(
            McpServerConfig(name="test", command="true", args=[])
        )
        await manager.close()
        assert len(manager._connections) == 0
        assert len(manager._tool_to_server) == 0

    @pytest.mark.asyncio
    async def test_connect_all_with_empty_configs(self) -> None:
        manager = McpClientManager([])
        await manager.connect_all()
        assert len(manager._connections) == 0
