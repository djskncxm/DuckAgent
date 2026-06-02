import re
from pathlib import Path

import structlog

from duckagent.config import settings
from duckagent.mcp import McpClientManager
from .base import ToolAgent

logger = structlog.get_logger()


class TraceAgent(ToolAgent):
    """Agent specialized in analyzing ARM64 execution traces.

    When trace files are configured, connects to MCP servers named in
    ``DUCKAGENT_TRACE_AGENT_MCP_SERVERS`` (default: ``"trace"``).
    """

    def __init__(self, bus, model: str, prompts_dir: Path) -> None:
        prompt_file = prompts_dir / "trace_agent.md"
        base_prompt = prompt_file.read_text() if prompt_file.exists() else "你是 Trace 分析 Agent。"

        super().__init__(
            agent_id="trace_agent",
            system_prompt=base_prompt,
            bus=bus,
            model=model,
        )

        trace_files = settings.trace_files
        existing = {k: v for k, v in trace_files.items() if v.exists()}
        self._mcp_manager: McpClientManager | None = None
        if existing:
            configs = settings.resolve_mcp_configs("trace_agent")
            if configs:
                self._mcp_manager = McpClientManager(configs)

    async def stop(self) -> None:
        if self._mcp_manager:
            await self._mcp_manager.close()
        await super().stop()

    @property
    def _default_evidence(self) -> str:
        return "analysis based on provided trace"

    @staticmethod
    def _extract_evidence(text: str) -> list[str]:
        return re.findall(r"line \d+[^.;\n]*", text, re.IGNORECASE)
