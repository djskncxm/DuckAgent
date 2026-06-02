import re
from pathlib import Path

import structlog

from duckagent.bus import Message
from duckagent.config import settings
from duckagent.mcp import McpClientManager, McpServerConfig
from .base import BaseAgent

logger = structlog.get_logger()


class IdaJadxAgent(BaseAgent):
    """Agent for both IDA Pro and JADX static analysis.

    Connects to MCP servers named in ``DUCKAGENT_JADX_AGENT_MCP_SERVERS``
    (default: ``"ida-pro-mcp,jadx-mcp"``).  The system prompt teaches the
    agent when to use IDA tools vs JADX tools based on user intent.
    """

    def __init__(
        self,
        bus,
        model: str,
        prompts_dir: Path,
        verify_enabled: bool = True,
        verify_max_retries: int = 3,
    ) -> None:
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
            verify_enabled=verify_enabled,
            verify_max_retries=verify_max_retries,
        )

        configs = settings.resolve_mcp_configs("ida_jadx_agent")
        self._mcp_manager = McpClientManager(configs) if configs else None

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
            evidence=evidence if evidence else ["analysis based on APK static analysis"],
            confidence=self._assess_confidence(response),
            reply_to=msg.id,
        )

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

    @staticmethod
    def _assess_confidence(text: str) -> str:
        low_indicators = ["不确定", "可能", "疑似", "unclear", "might", "possibly"]
        for indicator in low_indicators:
            if indicator in text.lower():
                return "low"
        return "high"
