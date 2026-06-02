import re
from pathlib import Path

import structlog

from duckagent.config import settings
from duckagent.mcp import McpClientManager
from .base import ToolAgent

logger = structlog.get_logger()


class IdaJadxAgent(ToolAgent):
    """Agent for both IDA Pro and JADX static analysis.

    Connects to MCP servers named in ``DUCKAGENT_JADX_AGENT_MCP_SERVERS``
    (default: ``"ida-pro-mcp,jadx-mcp"``).
    """

    def __init__(self, bus, model: str, prompts_dir: Path) -> None:
        prompt_file = prompts_dir / "ida_jadx_agent.md"
        base_prompt = (
            prompt_file.read_text()
            if prompt_file.exists()
            else "你是 JADX 静态分析 Agent。"
        )

        super().__init__(
            agent_id="ida_jadx_agent",
            system_prompt=base_prompt,
            bus=bus,
            model=model,
        )

        configs = settings.resolve_mcp_configs("ida_jadx_agent")
        self._mcp_manager = McpClientManager(configs) if configs else None

    async def stop(self) -> None:
        if self._mcp_manager:
            await self._mcp_manager.close()
        await super().stop()

    @property
    def _default_evidence(self) -> str:
        return "analysis based on APK static analysis"

    @staticmethod
    def _extract_evidence(text: str) -> list[str]:
        patterns = [
            r'(?:class|类)\s+([\w.$]+)',
            r'(?:method|方法)\s+([\w.$<>()]+)',
        ]
        evidence: list[str] = []
        for pattern in patterns:
            evidence.extend(re.findall(pattern, text, re.IGNORECASE))
        return evidence[:10]
