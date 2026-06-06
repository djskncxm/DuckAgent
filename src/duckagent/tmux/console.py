"""Rich console formatting for agent process stdout.

When an agent runs in a tmux pane, its stdout IS the chat display.
This module provides formatted message printing so the agent's output
looks like a readable chat log.

All formatting uses ``rich`` for colors, panels, and markdown rendering.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

if TYPE_CHECKING:
    from duckagent.bus.models import Message

# ── Agent markers ────────────────────────────────────────────────────

AGENT_MARKERS: dict[str, str] = {
    "human":            "\U0001f464",   # 👤
    "main_agent":       "\U0001f916",   # 🤖
    "trace_agent":      "\U0001f50d",   # 🔍
    "ida_jadx_agent":   "\U0001f52c",   # 🔬
    "system":           "⚙️", # ⚙️
}
FALLBACK_MARKER = "▶"  # ▶

TYPE_BADGES: dict[str, str] = {
    "request":    "\U0001f4e9",   # 📩
    "question":   "❓",       # ❓
    "conclusion": "\U0001f4cb",   # 📋
    "decision":   "⚡",       # ⚡
}

# ── Colour aliases ───────────────────────────────────────────────────

_RX = "bright_blue"     # received message border
_TX = "bright_green"    # sent/reply message border
_TOOL = "bright_yellow"  # tool call border
_ERR = "bright_red"     # error border
_STATUS = "bright_magenta"  # status change
_TS = "dim"             # timestamp style


def _fmt_ts(ts: datetime | str) -> str:
    if isinstance(ts, datetime):
        return ts.strftime("%H:%M:%S")
    return str(ts)[:8]


def _truncate(text: str, max_len: int = 500) -> str:
    """Truncate text for panel display."""
    if len(text) <= max_len:
        return text
    return text[:max_len] + "…"


# ── Public API ───────────────────────────────────────────────────────


class AgentConsole:
    """Rich console wrapper for agent stdout formatting.

    Instantiated once per agent process.  All methods are synchronous
    (rich writes to stdout immediately) so they can be called from
    async handlers without blocking the event loop.

    Parameters
    ----------
    agent_id:
        The agent's identifier, e.g. ``"main_agent"``.  Used to label output.
    width:
        Console width.  Defaults to ``min(terminal_width, 100)``.
    """

    def __init__(self, agent_id: str, width: int | None = None) -> None:
        self.agent_id = agent_id
        if width is None:
            try:
                import os
                width = min(os.get_terminal_size().columns, 100)
            except (ValueError, OSError):
                width = 80
        self._width = width
        self._console = Console(
            width=width,
            force_terminal=True,
            color_system="standard",
        )

    # ── Incoming messages ─────────────────────────────────────────

    def print_received(self, msg: Message) -> None:
        """Print a received message to stdout."""
        ts = _fmt_ts(msg.timestamp)
        from_label = "you" if msg.from_agent == "human" else msg.from_agent
        marker = AGENT_MARKERS.get(msg.from_agent, FALLBACK_MARKER)
        badge = TYPE_BADGES.get(msg.type, "")

        title = Text.assemble(
            (f"{marker} ", ""),
            (f"[{ts}] ", _TS),
            (f"from {from_label}"),
        )
        if badge:
            title.append(f" {badge}")

        if msg.mentions:
            mentions_text = " ".join(f"@{m}" for m in msg.mentions)
            title.append(f"  {mentions_text}", "dim")

        body = _truncate(msg.content)
        if msg.confidence and msg.confidence != "high":
            body = f"[{msg.confidence} confidence]\n{body}"

        md = Markdown(body, code_theme="monokai")
        self._console.print()
        self._console.print(Panel(md, title=title, border_style=_RX))

    # ── Outgoing messages ─────────────────────────────────────────

    def print_response(self, msg: Message) -> None:
        """Print a response/reply the agent is sending."""
        ts = _fmt_ts(msg.timestamp)
        to_label = "you" if msg.to_agent == "human" else (msg.to_agent or "all")

        title = Text.assemble(
            (f"[{ts}] ", _TS),
            (f"reply → {to_label}"),
        )
        if msg.type:
            badge = TYPE_BADGES.get(msg.type, "")
            if badge:
                title.append(f" {badge}")

        body = _truncate(msg.content)
        md = Markdown(body, code_theme="monokai")
        self._console.print()
        self._console.print(Panel(md, title=title, border_style=_TX))

    def print_send_error(self, to_agent: str | None, error: str) -> None:
        """Print a publish failure notice."""
        to_label = to_agent or "all"
        body = f"❌ Send to **{to_label}** failed:\n```\n{error}\n```"
        md = Markdown(body)
        self._console.print()
        self._console.print(Panel(md, title="⚠️ Send Error", border_style=_ERR))

    # ── Tool calls ────────────────────────────────────────────────

    def print_tool_call(self, tool_name: str, arguments: str) -> None:
        """Print a tool call the agent is making."""
        args_summary = _truncate(arguments, 200)
        body = f"**{tool_name}**\n```json\n{args_summary}\n```"
        md = Markdown(body, code_theme="monokai")
        self._console.print()
        self._console.print(Panel(md, title="\U0001f527 tool call", border_style=_TOOL))

    def print_tool_result(self, tool_name: str, result: str) -> None:
        """Print a tool result."""
        result_summary = _truncate(result, 300)
        body = f"**{tool_name}** →\n```\n{result_summary}\n```"
        md = Markdown(body, code_theme="monokai")
        self._console.print(Panel(md, title="✅ tool result", border_style=_TOOL))

    # ── Status ────────────────────────────────────────────────────

    def print_status(self, state: str, task_summary: str = "") -> None:
        """Print agent state change."""
        state_icon = {
            "idle":          "⏸️",
            "thinking":      "\U0001f9e0",
            "tool_calling":  "\U0001f527",
            "error":         "❌",
        }.get(state, "ℹ️")

        body = f"**{state_icon}  {state}**"
        if task_summary:
            body += f"\n{_truncate(task_summary, 200)}"
        md = Markdown(body)
        self._console.print(Panel(md, title=f"{self.agent_id}", border_style=_STATUS))

    # ── Generic / system ──────────────────────────────────────────

    def print_system(self, text: str) -> None:
        """Print a generic system-level message."""
        md = Markdown(text)
        self._console.print(Panel(md, title="⚙️ system", border_style="dim"))

    def print_banner(self, server_url: str) -> None:
        """Print a startup banner."""
        marker = AGENT_MARKERS.get(self.agent_id, FALLBACK_MARKER)
        table = Table.grid(padding=(0, 2))
        table.add_column(style="bold")
        table.add_column()
        table.add_row("Agent", f"{marker}  {self.agent_id}")
        table.add_row("Bus", server_url)
        table.add_row("Width", str(self._width))
        self._console.print()
        self._console.print(
            Panel(table, title="DuckAgent", border_style="cyan", padding=(1, 2))
        )
        self._console.print()


# ── Module-level convenience ────────────────────────────────────────

# Shared singleton for callers that don't need per-agent config.
# Prefer creating an AgentConsole instance in agent processes.
_shared: AgentConsole | None = None


def get_console(agent_id: str = "system") -> AgentConsole:
    """Return a shared ``AgentConsole`` singleton."""
    global _shared
    if _shared is None:
        _shared = AgentConsole(agent_id)
    return _shared
