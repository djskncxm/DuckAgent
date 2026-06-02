"""Tests for MCP ↔ OpenAI schema conversion."""

from duckagent.mcp.schema_converter import mcp_tool_to_openai, mcp_tools_to_openai_format


class TestMcpToolToOpenAI:
    """Tests for converting a single MCP Tool to OpenAI format."""

    def test_converts_basic_tool(self) -> None:
        tool = {
            "name": "test_tool",
            "description": "A test tool.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query."},
                },
                "required": ["query"],
            },
        }
        result = mcp_tool_to_openai(tool)
        assert result["type"] == "function"
        assert result["function"]["name"] == "test_tool"
        assert result["function"]["description"] == "A test tool."
        assert result["function"]["parameters"] == tool["inputSchema"]

    def test_converts_tool_with_empty_input_schema(self) -> None:
        tool = {
            "name": "no_param_tool",
            "description": "No parameters.",
            "inputSchema": {"type": "object", "properties": {}},
        }
        result = mcp_tool_to_openai(tool)
        assert result["function"]["name"] == "no_param_tool"
        assert result["function"]["parameters"]["properties"] == {}

    def test_converts_tool_without_description(self) -> None:
        tool = {
            "name": "minimal",
            "description": "",
            "inputSchema": {"type": "object", "properties": {}},
        }
        result = mcp_tool_to_openai(tool)
        assert result["function"]["name"] == "minimal"
        assert result["function"]["description"] == ""

    def test_converts_trace_search_schema(self) -> None:
        """Verify that a realistic trace_search tool schema converts correctly."""
        tool = {
            "name": "trace_search",
            "description": "Search a trace file.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Exact substring."},
                    "file": {
                        "type": "string",
                        "enum": ["code", "rw", "bl"],
                        "description": "Which trace file.",
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                },
                "required": ["query", "limit"],
            },
        }
        result = mcp_tool_to_openai(tool)
        params = result["function"]["parameters"]
        assert params["properties"]["file"]["enum"] == ["code", "rw", "bl"]
        assert params["properties"]["limit"]["maximum"] == 100

    def test_converts_jadx_tool_schema(self) -> None:
        """Verify a JADX tool schema with enum converts correctly."""
        tool = {
            "name": "jadx_search_classes_by_keyword",
            "description": "Search classes.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "search_term": {"type": "string"},
                    "search_in": {
                        "type": "string",
                        "enum": ["class", "method", "field", "code", "comment"],
                    },
                },
                "required": ["search_term"],
            },
        }
        result = mcp_tool_to_openai(tool)
        params = result["function"]["parameters"]
        assert "class" in params["properties"]["search_in"]["enum"]


class TestMcpToolsToOpenAI:
    """Tests for batch conversion."""

    def test_empty_list(self) -> None:
        assert mcp_tools_to_openai_format([]) == []

    def test_single_tool(self) -> None:
        tools = [
            {
                "name": "only_tool",
                "description": "Only one.",
                "inputSchema": {"type": "object", "properties": {}},
            }
        ]
        result = mcp_tools_to_openai_format(tools)
        assert len(result) == 1
        assert result[0]["function"]["name"] == "only_tool"

    def test_multiple_tools_preserve_order(self) -> None:
        tools = [
            {"name": "first", "description": "", "inputSchema": {"type": "object", "properties": {}}},
            {"name": "second", "description": "", "inputSchema": {"type": "object", "properties": {}}},
            {"name": "third", "description": "", "inputSchema": {"type": "object", "properties": {}}},
        ]
        result = mcp_tools_to_openai_format(tools)
        assert [r["function"]["name"] for r in result] == ["first", "second", "third"]
