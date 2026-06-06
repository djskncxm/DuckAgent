"""Agent chat interface — prompt_toolkit app with input at bottom, messages above.

Usage:
    python -m duckagent.tmux.chat_app --agent-id main_agent --server-url http://127.0.0.1:8720

Each agent window runs its own ChatApp instance, connected to the bus as an
observer but filtering for messages relevant to its agent_id.

Features:
    - Markdown rendering via rich → ANSI
    - Streaming display: each message rendered incrementally on arrival
    - Automatic trimming: keeps latest ~5000 chars, discards oldest
    - Clean layout: message area fills space, compact input at bottom
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import structlog
import websockets
import websockets.asyncio.client

from duckagent.bus.models import Message, parse_mentions

logger = structlog.get_logger()


def fmt_ts(ts: datetime | str) -> str:
    if isinstance(ts, datetime):
        return ts.strftime("%H:%M:%S")
    return str(ts)[:8]


# ── Per-agent display markers ─────────────────────────────────────
AGENT_MARKERS: dict[str, str] = {
    "human":            "👤",
    "main_agent":       "🤖",
    "trace_agent":      "🔍",
    "ida_jadx_agent":   "🔬",
    "system":           "⚙️ ",
}
FALLBACK_MARKER = "▶"

TYPE_BADGES: dict[str, str] = {
    "request":    "📩",
    "question":   "❓",
    "conclusion": "📋",
    "decision":   "⚡",
}

# Display trim: keep latest ~N characters in cache, discard older ones.
MAX_DISPLAY_CHARS = 8000
# Display tail: show only the last ~N chars in the visible window.
# This makes newest messages always visible at the bottom without scrolling.
TAIL_CHARS = 3000


class ChatApp:
    """prompt_toolkit-based chat interface for a single agent.

    Parameters
    ----------
    agent_id:
        The agent this window is chatting with (e.g. "main_agent").
    server_url:
        Bus server URL, e.g. "http://127.0.0.1:8720".
    """

    def __init__(self, agent_id: str, server_url: str) -> None:
        self.agent_id = agent_id
        self.server_url = server_url.rstrip("/")
        self._ws_url = server_url.replace("http://", "ws://").rstrip("/") + "/ws?role=observer"
        self._http: httpx.AsyncClient | None = None
        self._ws: websockets.asyncio.client.ClientConnection | None = None
        self._messages: list[Message] = []
        self._seen_ids: set[str] = set()
        self._running = False
        self._connected = False
        self._app: Any = None

        # Cached display: list of (style, text) tuples built incrementally.
        self._display_lines: list[tuple[str, str]] = []
        # Track total character count for trimming.
        self._display_chars: int = 0
        # Scroll offset: 0 = show latest (tail), >0 = scrolled up by N chars.
        # Adjusted via PgUp/PgDn/Home/End. Reset to 0 on new message.
        self._tail_offset: int = 0

    # ── Public API ─────────────────────────────────────────────────

    def run(self) -> None:
        """Blocking entry point — creates asyncio event loop and runs the app."""
        asyncio.run(self._run())

    async def _run(self) -> None:
        """Async entry point."""
        from prompt_toolkit.application import Application
        from prompt_toolkit.buffer import Buffer
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.layout import Layout, HSplit, Window, Dimension
        from prompt_toolkit.layout.controls import FormattedTextControl, BufferControl

        self._running = True
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(30.0))

        # ── Key bindings ──────────────────────────────────────────

        kb = KeyBindings()

        @kb.add("enter")
        def _(event: Any) -> None:
            """Submit message on Enter."""
            buf = event.app.layout.get_buffer_by_name("input")
            text = buf.text.strip()
            if not text:
                return
            buf.text = ""

            # ── Slash commands: handle synchronously, no bus publish ──
            if text.startswith("/"):
                self._handle_command_sync(text)
                return

            # ── Build and display message SYNCHRONOUSLY ──────────────
            # Must happen before we yield control, otherwise UI renders
            # with empty _display_lines → blank screen.
            self._tail_offset = 0  # reset scroll to latest on new message
            mentions = parse_mentions(text)
            msg = Message(
                from_agent="human",
                to_agent=self.agent_id,
                mentions=mentions,
                type="request",
                content=text,
                evidence=[],
                confidence="high",
            )
            self._seen_ids.add(msg.id)
            self._messages.append(msg)
            self._append_to_display(msg)

            # Publish to bus in background (don't block UI)
            asyncio.ensure_future(self._publish_message(msg))

        @kb.add("escape", "enter")
        def _(event: Any) -> None:
            """Shift+Enter: insert newline."""
            event.app.layout.get_buffer_by_name("input").insert_text("\n")

        @kb.add("c-c")
        def _(event: Any) -> None:
            """Ctrl+C: quit."""
            event.app.exit()

        @kb.add("c-d")
        def _(event: Any) -> None:
            """Ctrl+D at empty input: quit."""
            buf = event.app.layout.get_buffer_by_name("input")
            if not buf.text.strip():
                event.app.exit()

        # ── Keyboard scroll (PgUp/PgDn/Home/End) ──────────────────

        @kb.add("pageup")
        def _(event: Any) -> None:
            """Scroll up: show older messages."""
            self._tail_offset += 2000
            event.app.invalidate()

        @kb.add("pagedown")
        def _(event: Any) -> None:
            """Scroll down: show newer messages."""
            self._tail_offset = max(0, self._tail_offset - 2000)
            event.app.invalidate()

        @kb.add("home")
        def _(event: Any) -> None:
            """Jump to oldest messages."""
            self._tail_offset = 10_000_000
            event.app.invalidate()

        @kb.add("end")
        def _(event: Any) -> None:
            """Jump to latest messages."""
            self._tail_offset = 0
            event.app.invalidate()

        # ── Display text builder ──────────────────────────────────

        def _build_welcome() -> list[tuple[str, str]]:
            """Initial/welcome display."""
            lines: list[tuple[str, str]] = []
            if not self._connected:
                lines.append(("", "  ⏳ 正在连接到 "))
                lines.append(("bold", self.server_url))
                lines.append(("", " ...\n"))
                lines.append(("", "     等待 bus server 就绪后自动连接\n"))
            elif not self._messages:
                marker = AGENT_MARKERS.get(self.agent_id, FALLBACK_MARKER)
                lines.append(("", f"  {marker} 没有消息。等待 @{self.agent_id} 的任务...\n"))
                lines.append(("", "   输入消息后按 Enter 发送\n"))
            return lines

        def get_display_text() -> list[tuple[str, str]]:
            """Return visible slice of display text, respecting scroll offset.

            _display_lines is in chronological order (oldest → newest).
            Returns a window of ~TAIL_CHARS chars, offset by _tail_offset
            chars from the end.  Offset 0 = show latest (tail).
            """
            if not self._display_lines:
                return _build_welcome()

            # Walk backwards, skipping _tail_offset chars, collecting TAIL_CHARS
            tail: list[tuple[str, str]] = []
            chars = 0
            skipped = 0
            target_offset = self._tail_offset

            for fragment in reversed(self._display_lines):
                flen = len(fragment[1])
                if skipped < target_offset:
                    skipped += flen
                    continue
                tail.append(fragment)
                chars += flen
                if chars >= TAIL_CHARS:
                    break

            # Clamp offset so we don't get stuck past the end
            if not tail and self._tail_offset > 0:
                self._tail_offset = max(0, self._display_chars - TAIL_CHARS)

            tail.reverse()

            # Scroll indicator when not at bottom
            if self._tail_offset > 0 and tail:
                tail.insert(0, ("class:dim", "  ╭──  ↑ 上方还有更早的消息 (PgDn/End 回到底部)  ──╮\n"))
                tail.insert(1, ("class:dim", "  ╰──────────────────────────────────────────────╯\n"))
                tail.insert(2, ("", "\n"))

            return tail

        # ── Layout ────────────────────────────────────────────────

        message_control = FormattedTextControl(
            text=get_display_text,
            focusable=False,
        )
        message_window = Window(
            content=message_control,
            wrap_lines=True,
            allow_scroll_beyond_bottom=True,
        )

        input_buffer = Buffer(name="input", multiline=True)
        input_window = Window(
            content=BufferControl(buffer=input_buffer),
            height=Dimension(min=1, max=3),
            wrap_lines=True,
            dont_extend_height=True,
        )

        root_container = HSplit([
            message_window,
            Window(height=1, char="─", style="class:separator"),
            input_window,
        ])

        # ── Application ────────────────────────────────────────────

        self._app = Application(
            layout=Layout(root_container, focused_element=input_window),
            key_bindings=kb,
            full_screen=True,
            mouse_support=True,
        )

        # Start WebSocket + refresh
        ws_task = asyncio.create_task(self._ws_loop())
        refresh_task = asyncio.create_task(self._refresh_loop())

        try:
            await self._app.run_async()
        finally:
            self._running = False
            ws_task.cancel()
            refresh_task.cancel()
            try:
                await ws_task
            except asyncio.CancelledError:
                pass
            try:
                await refresh_task
            except asyncio.CancelledError:
                pass
            if self._ws:
                try:
                    await self._ws.close()
                except Exception:
                    pass
            if self._http:
                await self._http.aclose()

    # ── Networking ────────────────────────────────────────────────

    async def _ws_loop(self) -> None:
        """Connect to bus WebSocket and read messages, with reconnect."""
        while self._running:
            try:
                self._ws = await websockets.asyncio.client.connect(
                    self._ws_url, open_timeout=5.0
                )
                self._connected = True
                # Rebuild display cache on reconnect
                self._rebuild_display()
                await self._ws_read_loop()
            except asyncio.CancelledError:
                return
            except (websockets.ConnectionClosed, OSError) as exc:
                self._connected = False
                if not self._running:
                    return
                logger.debug("ws_disconnected", error=str(exc)[:80])
                await asyncio.sleep(2.0)
            except Exception as exc:
                self._connected = False
                if not self._running:
                    return
                logger.debug("ws_connect_failed", error=str(exc)[:80])
                await asyncio.sleep(3.0)

    async def _ws_read_loop(self) -> None:
        """Read messages from the WebSocket — stream each one to display."""
        assert self._ws is not None
        async for raw in self._ws:
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if payload.get("type") != "message":
                continue
            data = payload.get("data")
            if not isinstance(data, dict):
                continue
            try:
                msg = Message(**data)
            except Exception:
                continue

            if msg.id in self._seen_ids:
                continue
            self._seen_ids.add(msg.id)

            if not self._is_relevant(msg):
                continue

            self._messages.append(msg)
            self._append_to_display(msg)

    def _is_relevant(self, msg: Message) -> bool:
        """Check if a message is relevant to this agent's window."""
        if msg.type == "status":
            return False
        if msg.from_agent == self.agent_id:
            return True
        if msg.to_agent == self.agent_id:
            return True
        if self.agent_id in (msg.mentions or []):
            return True
        if msg.from_agent == "human":
            return True
        if msg.to_agent == "human":
            return True
        return False

    # ── Display update ────────────────────────────────────────────

    def _append_to_display(self, msg: Message) -> None:
        """Render a message and append to display cache (chronological order)."""
        rendered = self._render_message(msg)
        # Append: normal chat order — oldest at top, newest at bottom.
        self._display_lines.extend(rendered)
        self._display_chars += sum(len(t) for _, t in rendered)

        # Trim oldest content from the front
        self._trim_display()

        if self._app:
            self._app.invalidate()

    def _trim_display(self) -> None:
        """Remove oldest display lines from the FRONT until within MAX_DISPLAY_CHARS."""
        while self._display_chars > MAX_DISPLAY_CHARS and self._display_lines:
            removed_style, removed_text = self._display_lines.pop(0)
            self._display_chars -= len(removed_text)

    def _render_message(self, msg: Message) -> list[tuple[str, str]]:
        """Render a single message to formatted-text lines.

        Uses rich for markdown → ANSI → prompt_toolkit formatted text.
        Falls back to plain text on any error.
        """
        from prompt_toolkit.formatted_text import ANSI

        lines: list[tuple[str, str]] = []

        ts = fmt_ts(msg.timestamp)
        from_agent = msg.from_agent
        from_label = "you" if from_agent == "human" else from_agent
        to_label = "you" if msg.to_agent == "human" else (msg.to_agent or "all")
        marker = AGENT_MARKERS.get(from_agent, FALLBACK_MARKER)
        type_badge = TYPE_BADGES.get(msg.type, "")

        # ── Header ────────────────────────────────────────────────
        header = f"  {marker} [{ts}] {from_label} → {to_label}"
        if type_badge:
            header += f" {type_badge}"
        lines.append(("bold", header + "\n"))

        # Mentions
        if msg.mentions:
            mentions_str = ", ".join(f"@{m}" for m in msg.mentions)
            lines.append(("class:dim", f"     @mentions: {mentions_str}\n"))

        # Confidence
        if msg.confidence and msg.confidence != "high":
            conf_style = {"medium": "fg:yellow", "low": "fg:red"}.get(
                msg.confidence, ""
            )
            lines.append((conf_style, f"     [{msg.confidence} confidence]\n"))

        # ── Content (markdown → ANSI → formatted text) ────────────
        content = msg.content[:800]
        if len(msg.content) > 800:
            content += "…"

        try:
            from rich.console import Console
            from rich.markdown import Markdown

            md_console = Console(width=80, force_terminal=True, color_system="standard")
            md = Markdown(content, code_theme="monokai")
            with md_console.capture() as capture:
                md_console.print(md, width=76, no_wrap=False, crop=False, soft_wrap=True)
            ansi_text = capture.get()
            # Strip trailing whitespace (rich pads to full width)
            ansi_text = "\n".join(line.rstrip() for line in ansi_text.split("\n"))
            fragments = ANSI(ansi_text).__pt_formatted_text__()
            if fragments:
                lines.extend(fragments)
        except Exception:
            # Fallback: plain text with indent
            for line in content.split("\n"):
                lines.append(("", f"  {line}\n"))

        # ── Separator ─────────────────────────────────────────────
        lines.append(("", "\n"))
        lines.append(("class:dim", "  " + "─" * 50 + "\n"))
        lines.append(("", "\n"))

        return lines

    def _rebuild_display(self) -> None:
        """Rebuild entire display cache from recent messages (chronological order)."""
        self._display_lines.clear()
        self._display_chars = 0
        for msg in self._messages[-50:]:
            rendered = self._render_message(msg)
            self._display_lines.extend(rendered)
            self._display_chars += sum(len(t) for _, t in rendered)
        self._trim_display()
        if self._app:
            self._app.invalidate()

    # ── Send / Commands ───────────────────────────────────────────

    async def _publish_message(self, msg: Message) -> None:
        """Send a message to the bus (async, called in background)."""
        assert self._http is not None
        try:
            resp = await self._http.post(
                f"{self.server_url}/api/v1/publish",
                json=msg.model_dump(mode="json"),
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("publish_failed", error=str(exc)[:120])
            err_msg = Message(
                from_agent="system",
                to_agent="human",
                type="conclusion",
                content=f"❌ 发送失败: {exc}",
                evidence=[],
                confidence="low",
            )
            self._messages.append(err_msg)
            self._append_to_display(err_msg)
        except Exception as exc:
            logger.error("publish_unexpected_error", error=str(exc)[:200])

    def _handle_command_sync(self, text: str) -> None:
        """Handle /slash commands — runs synchronously in key handler."""
        cmd = text.strip().lower()

        if cmd in ("/quit", "/exit", "/q"):
            if self._app:
                self._app.exit()
            sentinel = os.environ.get("DUCKAGENT_TMUX_QUIT_FILE")
            if sentinel:
                Path(sentinel).touch()

        elif cmd in ("/clear", "/cls"):
            self._messages.clear()
            self._display_lines.clear()
            self._display_chars = 0
            if self._app:
                self._app.invalidate()

        elif cmd == "/agents":
            msg = Message(
                from_agent="system",
                to_agent="human",
                type="conclusion",
                content="Agent 状态请查看 tmux 窗口 4 (status)。\n切换: 鼠标点击底部 status bar 或 Ctrl+B 4",
                evidence=[],
                confidence="high",
            )
            self._messages.append(msg)
            self._append_to_display(msg)

        else:
            msg = Message(
                from_agent="system",
                to_agent="human",
                type="conclusion",
                content=f"未知命令: {text}\n可用: /quit, /clear, /agents",
                evidence=[],
                confidence="high",
            )
            self._messages.append(msg)
            self._append_to_display(msg)

    async def _refresh_loop(self) -> None:
        """Periodically invalidate the UI for connection status changes."""
        while self._running:
            await asyncio.sleep(0.3)
            if self._app:
                try:
                    self._app.invalidate()
                except Exception:
                    pass


# ── CLI entry point ────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="DuckAgent chat window")
    parser.add_argument("--agent-id", required=True, help="Agent ID for this window")
    parser.add_argument("--server-url", required=True, help="Bus server URL")
    args = parser.parse_args()

    app = ChatApp(agent_id=args.agent_id, server_url=args.server_url)
    app.run()


if __name__ == "__main__":
    main()
