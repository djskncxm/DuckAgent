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
_TS = "dim"             # timestamp style


def _fmt_ts(ts: datetime | str) -> str:
    if isinstance(ts, datetime):
        return ts.strftime("%H:%M:%S")
    return str(ts)[:8]


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
                width = os.get_terminal_size().columns
            except (ValueError, OSError):
                width = None  # rich auto-detects
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

        body = msg.content
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

        body = msg.content
        md = Markdown(body, code_theme="monokai")
        self._console.print()
        self._console.print(Panel(md, title=title, border_style=_TX))

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


