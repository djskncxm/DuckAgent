import re
from pathlib import Path
from typing import Any

import structlog

from duckagent.config import settings
from duckagent.mcp import McpClientManager
from duckagent.bus import Message
from .base import BaseAgent

logger = structlog.get_logger()

_INTERNAL_AGENTS = {"trace_agent", "ida_jadx_agent"}

_ROUTING_INSTRUCTION = """

## 多 Agent 协作

**仅在用户明确要求时**才委托给其他 agent。使用 @agent_id 指名：

- `@trace_agent` — 执行流分析，分析 ARM64 trace 数据
- `@ida_jadx_agent` — 静态代码分析，搜索/阅读 APK 反编译代码

委托格式：
```
@trace_agent 请分析这段 trace 中地址 0x7a3c00 处的加密操作
```

**重要规则：**
- 用户只说"测试通信"就别分配实际分析任务，只做通信测试
- 用户说"让 xx agent 回复我一句话"，你就只让它回复一句，别加戏
- 回复用户时**不要 @ 其他 agent**——回复是给用户看的，不是新的委托
- 用户没让做的事不要主动提议
"""


class MainAgent(BaseAgent):
    """Main coordinating agent that routes messages and loads project context."""

    def __init__(
        self,
        bus,
        model: str,
        agent_md_path: Path,
        prompts_dir: Path,
    ) -> None:
        prompt_file = prompts_dir / "main_agent.md"
        base_prompt = prompt_file.read_text() if prompt_file.exists() else "你是主协调 Agent。"

        agent_md_content = ""
        if agent_md_path.exists():
            agent_md_content = agent_md_path.read_text()

        system_prompt = f"{base_prompt}\n\n## 项目上下文\n\n{agent_md_content}{_ROUTING_INSTRUCTION}"

        super().__init__(
            agent_id="main_agent",
            system_prompt=system_prompt,
            bus=bus,
            model=model,
        )

        configs = settings.resolve_mcp_configs("main_agent")
        self._mcp_manager: McpClientManager | None = McpClientManager(configs) if configs else None

    async def stop(self) -> None:
        if self._mcp_manager:
            await self._mcp_manager.close()
        await super().stop()

    async def on_message(self, msg: Message) -> None:
        """Handle incoming message: think using LLM, route by @mentions in response."""

        # Build context-aware input
        context_info = f"[来自 {msg.from_agent}] (type={msg.type})"
        if msg.mentions:
            context_info += f" [提及了: {', '.join(msg.mentions)}]"
        input_text = f"{context_info}: {msg.content}"

        response = await self.think(input_text, mcp_manager=self._mcp_manager)

        # Parse @mentions from LLM's response and route to internal agents
        response_mentions = self._parse_mentions(response)
        internal_mentions = [m for m in response_mentions if m in _INTERNAL_AGENTS]

        if internal_mentions:
            unique_mentions = list(dict.fromkeys(internal_mentions))
            await self.send(
                to=None,  # routed purely by mentions
                content=response,
                type="request",
                mentions=unique_mentions,
                evidence=[msg.id] if msg.id else [],
                confidence="high",
                reply_to=msg.id,
            )
            return

        # No internal mentions — respond to human
        content = self._strip_json_artifacts(response)
        await self.send(
            to="human",
            content=content,
            type="conclusion",
            evidence=["model response"],
            confidence="medium",
            reply_to=msg.id,
        )

    @staticmethod
    def _strip_json_artifacts(text: str) -> str:
        """Remove JSON code blocks or raw JSON objects from display text."""
        # Remove ```json ... ``` blocks entirely
        text = re.sub(r"```(?:json)?\s*\n?\{[^`]*\}\n?\s*```", "", text, flags=re.DOTALL)
        # If the ENTIRE response is a JSON object, replace with a clean message
        stripped = text.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            # Try to extract "content" field as last resort
            m = re.search(r'"content"\s*:\s*"((?:[^"\\]|\\.)*)"', stripped)
            if m:
                try:
                    return m.group(1).encode().decode("unicode_escape")
                except Exception:
                    pass
            return "收到回复，但格式异常，请重新提问。"
        return stripped
