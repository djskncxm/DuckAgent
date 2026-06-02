import re
from pathlib import Path

import structlog

from duckagent.bus import Message
from duckagent.config import settings
from duckagent.mcp import McpClientManager, McpServerConfig
from .base import BaseAgent

logger = structlog.get_logger()


class TraceAgent(BaseAgent):
    """Agent specialized in analyzing ARM64 execution traces.

    When trace files are configured, connects to MCP servers named in
    ``DUCKAGENT_TRACE_AGENT_MCP_SERVERS`` (default: ``"trace"``).
    MCP servers are resolved from the built-in registry + any custom
    servers defined in ``DUCKAGENT_MCP_SERVERS``.
    """

    def __init__(
        self,
        bus,
        model: str,
        prompts_dir: Path,
        verify_enabled: bool = True,
        verify_max_retries: int = 3,
    ) -> None:
        prompt_file = prompts_dir / "trace_agent.md"
        base_prompt = prompt_file.read_text() if prompt_file.exists() else "你是 Trace 分析 Agent。"

        super().__init__(
            agent_id="trace_agent",
            system_prompt=base_prompt,
            bus=bus,
            model=model,
            verify_enabled=verify_enabled,
            verify_max_retries=verify_max_retries,
        )

        # Only enable MCP if trace files actually exist
        trace_files = settings.trace_files
        existing = {k: v for k, v in trace_files.items() if v.exists()}
        self._mcp_manager: McpClientManager | None = None
        if existing:
            configs = settings.resolve_mcp_configs("trace_agent")
            if configs:
                self._mcp_manager = McpClientManager(configs)

    async def start(self) -> None:
        await super().start()

    async def stop(self) -> None:
        if self._mcp_manager:
            await self._mcp_manager.close()
        await super().stop()

    async def on_message(self, msg: Message) -> None:
        """Handle incoming messages.

        Only processes actionable message types (request, question).
        Being @mentioned in a conclusion/decision is just informational (CC) — no action.
        """
        if msg.type not in ("request", "question"):
            return
        addressed_to_us = (
            msg.to_agent == self.agent_id or
            (msg.mentions and self.agent_id in msg.mentions)
        )
        if not addressed_to_us:
            return

        input_text = msg.content
        if msg.from_agent:
            input_text = f"[来自 {msg.from_agent}]: {input_text}"

        response = await self.think(
            input_text,
            mcp_manager=self._mcp_manager,
        )

        evidence = self._extract_evidence(response)
        reply_to = msg.from_agent if msg.from_agent != "human" else "human"

        await self.send(
            to=reply_to,
            content=response,
            type="conclusion",
            evidence=evidence if evidence else ["analysis based on provided trace"],
            confidence=self._assess_confidence(response),
            reply_to=msg.id,
        )

    @staticmethod
    def _extract_evidence(text: str) -> list[str]:
        pattern = r"line \d+[^.;\n]*"
        return re.findall(pattern, text, re.IGNORECASE)

    @staticmethod
    def _assess_confidence(text: str) -> str:
        low_indicators = ["不确定", "可能", "疑似", "unclear", "might", "possibly"]
        for indicator in low_indicators:
            if indicator in text.lower():
                return "low"
        return "high"
