import asyncio
import json
from typing import Any

import litellm
import structlog

from duckagent.bus import Message, MessageBus
from duckagent.bus.models import parse_mentions

logger = structlog.get_logger()


class BaseAgent:
    """Base class for all agents in the system.

    Provides lifecycle management, message dispatch, and LLM calling.
    """

    def __init__(
        self,
        agent_id: str,
        system_prompt: str,
        bus: MessageBus,
        model: str,
    ) -> None:
        self.agent_id = agent_id
        self.system_prompt = system_prompt
        self.bus = bus
        self.model = model
        self._queue: asyncio.Queue[Message] | None = None
        self._task: asyncio.Task | None = None
        self._history: list[dict[str, Any]] = []

    async def start(self) -> None:
        """Subscribe to the bus and start the message processing loop."""
        self._queue = self.bus.subscribe(self.agent_id)
        self._task = asyncio.create_task(self._loop())
        logger.info("agent_started", agent_id=self.agent_id)

    async def stop(self) -> None:
        """Cancel the processing loop and unsubscribe from the bus."""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self.bus.unsubscribe(self.agent_id)
        logger.info("agent_stopped", agent_id=self.agent_id)

    async def _loop(self) -> None:
        """Main message processing loop."""
        assert self._queue is not None
        while True:
            msg = await self._queue.get()
            if msg.type == "status":
                continue
            try:
                await self.on_message(msg)
            except Exception as e:
                logger.error("agent_error", agent_id=self.agent_id, error=str(e))
                await self._broadcast_status("idle", task_summary=f"错误: {e}")
                await self.bus.publish(Message(
                    from_agent=self.agent_id,
                    to_agent="human",
                    type="conclusion",
                    content=f"❌ 处理消息时出错: {e}",
                    evidence=[str(msg.id)],
                    confidence="low",
                    reply_to=msg.id,
                ))

    @staticmethod
    def _parse_mentions(text: str) -> list[str]:
        """Extract unique @agent_id mentions in order of first appearance."""
        return parse_mentions(text)

    async def on_message(self, msg: Message) -> None:
        """Handle an incoming message.

        Default behavior: if mentions is non-empty and this agent is not
        mentioned, skip processing. If mentions is empty (broadcast), process.
        Subclasses should override for specific behavior but may call
        super().on_message(msg) as a guard.
        """
        if msg.mentions and self.agent_id not in msg.mentions:
            return
        raise NotImplementedError

    async def _broadcast_status(self, state: str, task_summary: str = "") -> None:
        content = json.dumps({"state": state, "task_summary": task_summary}, ensure_ascii=False)
        msg = Message(
            from_agent=self.agent_id,
            to_agent=None,
            type="status",
            content=content,
            evidence=[],
            confidence="high",
        )
        await self.bus.publish(msg)

    async def think(self, input_text: str, *, tools: list[dict] | None = None,
                    mcp_manager: Any = None, max_iterations: int = 50) -> str:
        """Call the LLM with full conversation history for multi-turn coherence.

        Maintains ``self._history`` across calls so the agent remembers prior
        exchanges. Tool-calling loops expand context locally within a single
        call; only the final assistant response is persisted to history.

        If the model returns a context-length error, the oldest messages are
        trimmed and the call is retried automatically.

        Args:
            input_text: The user/agent input to reason about.
            tools: OpenAI-format tool schemas. If ``None`` and ``mcp_manager``
                is provided, tools are fetched dynamically from MCP servers.
            mcp_manager: ``McpClientManager`` for MCP-based async tool calls.
            max_iterations: Max tool-calling loop iterations.
        """
        self._history.append({"role": "user", "content": input_text})
        await self._broadcast_status("thinking", task_summary=input_text[:80])

        # Resolve tools from MCP manager (lazy connect on first use)
        _tools: list[dict] | None = tools
        if mcp_manager is not None and _tools is None:
            _tools = await mcp_manager.ensure_tools()

        # Build context: system prompt + conversation history + local tool loop
        local_tool_messages: list[dict[str, Any]] = []

        for _ in range(max_iterations):
            context: list[dict[str, Any]] = [
                {"role": "system", "content": self.system_prompt},
                *self._history,
                *local_tool_messages,
            ]

            kwargs: dict[str, Any] = {"model": self.model, "messages": context}
            if _tools:
                kwargs["tools"] = _tools
                kwargs["tool_choice"] = "auto"

            response = await self._call_llm_with_context_retry(
                context, local_tool_messages, **kwargs
            )
            message = response.choices[0].message

            assistant_entry: dict[str, Any] = {"role": "assistant", "content": message.content or ""}
            tool_calls = getattr(message, "tool_calls", None)
            if isinstance(tool_calls, list) and tool_calls:
                assistant_entry["tool_calls"] = [
                    tc.model_dump() if hasattr(tc, "model_dump") else tc
                    for tc in tool_calls
                ]
            local_tool_messages.append(assistant_entry)

            if not isinstance(tool_calls, list) or not tool_calls:
                # Persist only the final text response to history
                self._history.append({"role": "assistant", "content": message.content or ""})
                await self._broadcast_status("idle")
                return message.content or ""

            if mcp_manager is not None:
                await self._broadcast_status("tool_calling")
                for tc in tool_calls:
                    name = tc.function.name
                    arguments = json.loads(tc.function.arguments)
                    result = await mcp_manager.call_tool(name, arguments)
                    local_tool_messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "name": name,
                        "content": result,
                    })
                continue

            # No tool executor available
            self._history.append({"role": "assistant", "content": message.content or ""})
            await self._broadcast_status("idle")
            return message.content or ""

        await self._broadcast_status("idle")
        return "Reached max iterations without final answer."

    async def _call_llm_with_context_retry(
        self,
        context: list[dict[str, Any]],
        local_tool_messages: list[dict[str, Any]],
        **kwargs,
    ) -> Any:
        """Call LLM; on context-length error, trim oldest history and retry."""
        try:
            return await self._call_llm_with_retry(**kwargs)
        except Exception as e:
            error_str = str(e).lower()
            if "context" not in error_str and "length" not in error_str and "token" not in error_str:
                raise
            # Trim oldest history pairs (keep at least the latest user message)
            trimmed = 0
            while len(self._history) > 2:
                self._history.pop(0)
                trimmed += 1
                if trimmed >= 4:
                    break
            logger.warning("context_trimmed", agent_id=self.agent_id, trimmed=trimmed,
                           remaining=len(self._history))
            # Rebuild context and retry once
            kwargs["messages"] = [
                {"role": "system", "content": self.system_prompt},
                *self._history,
                *local_tool_messages,
            ]
            return await self._call_llm_with_retry(**kwargs)

    async def _call_llm_with_retry(self, max_retries: int = 3, **kwargs) -> Any:
        """Call litellm with retry on transient errors."""
        for attempt in range(max_retries):
            try:
                return await litellm.acompletion(**kwargs)
            except Exception as e:
                if attempt == max_retries - 1:
                    raise
                error_name = type(e).__name__
                if "BadGateway" in error_name or "Timeout" in error_name or "Connection" in error_name:
                    logger.warning("llm_retry", agent_id=self.agent_id, attempt=attempt + 1, error=str(e)[:100])
                    await asyncio.sleep(2 ** attempt)
                else:
                    raise

    async def send(
        self,
        to: str | None,
        content: str,
        type: str = "conclusion",
        evidence: list[str] | None = None,
        confidence: str = "high",
        reply_to: str | None = None,
        mentions: list[str] | None = None,
    ) -> None:
        """Send a message through the bus.

        If `mentions` is None, @agent_id patterns are auto-parsed from content.
        Pass an explicit list (including empty) to override.
        """
        if mentions is None:
            mentions = self._parse_mentions(content)

        msg = Message(
            from_agent=self.agent_id,
            to_agent=to,
            mentions=mentions,
            type=type,
            content=content,
            evidence=evidence or [],
            confidence=confidence,
            reply_to=reply_to,
        )

        await self.bus.publish(msg)


class ToolAgent(BaseAgent):
    """Base class for agents that use MCP tools to process requests.

    Provides a standard on_message flow: filter → think → reply.
    Subclasses set ``_mcp_manager`` and optionally override ``_extract_evidence``.
    """

    _mcp_manager: Any = None

    async def on_message(self, msg: Message) -> None:
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

        response = await self.think(input_text, mcp_manager=self._mcp_manager)

        evidence = self._extract_evidence(response)
        reply_to = msg.from_agent if msg.from_agent != "human" else "human"

        await self.send(
            to=reply_to,
            content=response,
            type="conclusion",
            evidence=evidence if evidence else [self._default_evidence],
            confidence=self._assess_confidence(response),
            reply_to=msg.id,
        )

    @property
    def _default_evidence(self) -> str:
        return "analysis"

    @staticmethod
    def _extract_evidence(text: str) -> list[str]:
        """Override in subclasses for domain-specific evidence extraction."""
        return []

    @staticmethod
    def _assess_confidence(text: str) -> str:
        low_indicators = ["不确定", "可能", "疑似", "unclear", "might", "possibly"]
        for indicator in low_indicators:
            if indicator in text.lower():
                return "low"
        return "high"
