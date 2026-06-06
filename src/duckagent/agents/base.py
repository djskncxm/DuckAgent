import asyncio
import json
import subprocess
from typing import Any, Callable

import litellm
import structlog

from duckagent.bus import Message, MessageBus
from duckagent.bus.models import parse_mentions
from duckagent.tmux.console import AgentConsole

logger = structlog.get_logger()

# ── Local tool: shell_exec ───────────────────────────────────────────

SHELL_EXEC_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "shell_exec",
        "description": "Execute a shell command and return stdout+stderr. Use for compiling, running tools, inspecting files, etc.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command to execute",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (default 30)",
                },
            },
            "required": ["command"],
        },
    },
}


DANGEROUS_COMMANDS = {"sudo", "rm", "chmod", "chown", "mkfs", "dd", "shutdown", "reboot", "kill", "killall", "pkill"}


def _check_dangerous(command: str) -> str | None:
    """Return error string if command contains dangerous keywords, else None."""
    # Block output redirections (except /dev/null and &1/&2 fd redirects)
    import re
    # Match > or >> NOT followed by /dev/null or &1/&2
    for m in re.finditer(r'(\d*)>{1,2}\s*(\S+)', command):
        target = m.group(2)
        if target in ("/dev/null", "&1", "&2"):
            continue
        return "[blocked] 文件写入请使用 file_write/file_append 工具，不要用 shell 重定向"

    tokens = command.split()
    for token in tokens:
        base = token.split("/")[-1]
        if base in DANGEROUS_COMMANDS:
            return f"[blocked] 危险命令 `{base}` 需要人工执行，agent 不允许调用"
    if "|" in command or ";" in command or "&&" in command:
        parts = command.replace("|", " ").replace(";", " ").replace("&&", " ").split()
        for part in parts:
            base = part.split("/")[-1]
            if base in DANGEROUS_COMMANDS:
                return f"[blocked] 危险命令 `{base}` 需要人工执行，agent 不允许调用"
    return None


def _exec_shell(command: str, timeout: int = 30) -> str:
    blocked = _check_dangerous(command)
    if blocked:
        return blocked
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=timeout,
        )
        output = result.stdout + result.stderr
        if result.returncode != 0:
            output = f"[exit code {result.returncode}]\n{output}"
        return output.strip() or "(no output)"
    except subprocess.TimeoutExpired:
        return f"[timeout after {timeout}s]"
    except Exception as e:
        return f"[error: {e}]"


# Registry: name → callable(arguments_dict) → str
LOCAL_TOOLS: dict[str, Callable[[dict[str, Any]], str]] = {
    "shell_exec": lambda args: _exec_shell(args["command"], args.get("timeout", 30)),
}

LOCAL_TOOL_SCHEMAS: list[dict[str, Any]] = [SHELL_EXEC_SCHEMA]


class BaseAgent:
    """Base class for all agents in the system.

    Provides lifecycle management, message dispatch, and LLM calling.
    """

    _MID_STEP_INDICATORS = ["接下来", "然后我", "下一步", "让我", "我将", "首先检查", "我先", "我需要先"]
    _FINAL_INDICATORS = ["结论", "结果", "综上", "confidence", "evidence", "不可用", "未连接", "blocked"]

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
        self._console = AgentConsole(agent_id)

    async def start(self) -> None:
        """Subscribe to the bus and start the message processing loop."""
        self._queue = self.bus.subscribe(self.agent_id)
        self._task = asyncio.create_task(self._loop())
        # Print startup banner to stdout (visible in tmux pane)
        server_url = getattr(self.bus, "server_url", "local")
        self._console.print_banner(str(server_url))
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
            # Print received message to stdout (tmux pane display)
            if msg.type in ("request", "question"):
                self._console.print_received(msg)
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

    async def _broadcast_status(
        self,
        state: str,
        task_summary: str = "",
        tool_name: str = "",
        tool_args_summary: str = "",
        last_error: str = "",
        last_api_latency: str = "",
    ) -> None:
        status_data: dict[str, str] = {"state": state, "task_summary": task_summary}
        if tool_name:
            status_data["tool_name"] = tool_name
        if tool_args_summary:
            status_data["tool_args_summary"] = tool_args_summary
        if last_error:
            status_data["last_error"] = last_error
        if last_api_latency:
            status_data["last_api_latency"] = last_api_latency
        content = json.dumps(status_data, ensure_ascii=False)
        msg = Message(
            from_agent=self.agent_id,
            to_agent=None,
            type="status",
            content=content,
            evidence=[],
            confidence="high",
        )
        await self.bus.publish(msg)

    def _is_mid_step_pause(self, text: str) -> bool:
        """Detect if the model paused mid-task instead of giving a final answer."""
        if not text or len(text) > 2000:
            return False
        has_mid = any(ind in text for ind in self._MID_STEP_INDICATORS)
        has_final = any(ind in text for ind in self._FINAL_INDICATORS)
        return has_mid and not has_final

    async def think(self, input_text: str, *, tools: list[dict] | None = None,
                    mcp_manager: Any = None, max_iterations: int = 50,
                    max_continuations: int = 3) -> str:
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
            max_continuations: Max times to nudge the model if it pauses
                mid-task without calling tools or giving a final conclusion.
        """
        self._history.append({"role": "user", "content": input_text})
        await self._broadcast_status("thinking", task_summary=input_text[:80])

        # Resolve tools from MCP manager (lazy connect on first use)
        _tools: list[dict] | None = tools
        if mcp_manager is not None and _tools is None:
            _tools = await mcp_manager.ensure_tools()
        # Merge local tools
        if _tools is None:
            _tools = list(LOCAL_TOOL_SCHEMAS)
        else:
            _tools = list(_tools) + LOCAL_TOOL_SCHEMAS

        # Build context: system prompt + conversation history + local tool loop
        local_tool_messages: list[dict[str, Any]] = []
        continuation_count = 0

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
                text = message.content or ""
                # Detect mid-step pause and nudge the model to continue
                if self._is_mid_step_pause(text) and continuation_count < max_continuations:
                    continuation_count += 1
                    local_tool_messages.append({
                        "role": "user",
                        "content": "继续执行，不要停顿描述计划。直接调用工具或给出最终结论。",
                    })
                    continue
                # Final output
                self._history.append({"role": "assistant", "content": text})
                await self._broadcast_status("idle")
                return text

            if mcp_manager is not None or LOCAL_TOOLS:
                # Broadcast first tool being called for status dashboard
                first_tc = tool_calls[0]
                first_name = first_tc.function.name
                first_args = first_tc.function.arguments
                args_summary = first_args[:80] if len(first_args) > 80 else first_args
                await self._broadcast_status(
                    "tool_calling",
                    task_summary=input_text[:80],
                    tool_name=first_name,
                    tool_args_summary=args_summary,
                )
                for tc in tool_calls:
                    name = tc.function.name
                    arguments = json.loads(tc.function.arguments)
                    # Print tool call to stdout (visible in tmux pane)
                    self._console.print_tool_call(name, tc.function.arguments)
                    # Local tools take priority over MCP
                    if name in LOCAL_TOOLS:
                        result = LOCAL_TOOLS[name](arguments)
                    elif mcp_manager is not None:
                        result = await mcp_manager.call_tool(name, arguments)
                    else:
                        result = f'{{"status": "error", "error": "Unknown tool: {name}"}}'
                    # Print tool result to stdout
                    self._console.print_tool_result(name, result)
                    local_tool_messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "name": name,
                        "content": result,
                    })
                continue

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

        # Print to stdout (visible in tmux pane) — but not status
        if type != "status":
            self._console.print_response(msg)
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

        reply_msg = Message(
            from_agent=self.agent_id,
            to_agent=reply_to,
            type="conclusion",
            content=response,
            evidence=evidence if evidence else [self._default_evidence],
            confidence=self._assess_confidence(response),
            reply_to=msg.id,
        )

        # Print response to stdout (visible in tmux pane)
        self._console.print_response(reply_msg)

        await self.bus.publish(reply_msg)

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
