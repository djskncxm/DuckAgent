"""Agent chat interface — prompt_toolkit app with input at bottom, messages above.

Usage:
    python -m duckagent.tmux.chat_app --agent-id main_agent --server-url http://127.0.0.1:8720

Each agent window runs its own ChatApp instance, connected to the bus as an
observer but filtering for messages relevant to its agent_id.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
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
            text = event.app.layout.get_buffer_by_name("input").text.strip()
            if text:
                asyncio.create_task(self._send_message(text))
                event.app.layout.get_buffer_by_name("input").text = ""

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

        # ── Message display ───────────────────────────────────────

        def get_message_text() -> list[tuple[str, str]]:
            """Build formatted text for the message area."""
            lines: list[tuple[str, str]] = []

            if not self._connected:
                lines.append(("", "  ⏳ 正在连接到 "))
                lines.append(("bold", self.server_url))
                lines.append(("", " ...\n"))
                lines.append(("", "     等待 bus server 就绪后自动连接\n"))
                return lines

            if not self._messages:
                lines.append(("", f"  没有消息。等待 @{self.agent_id} 的任务...\n"))
                lines.append(("", "  输入消息后按 Enter 发送\n"))
                return lines

            # Show last N messages
            visible = self._messages[-50:]
            for msg in visible:
                ts = fmt_ts(msg.timestamp)
                from_label = "you" if msg.from_agent == "human" else msg.from_agent
                to_label = "you" if msg.to_agent == "human" else (msg.to_agent or "all")

                # Direction tag
                if msg.from_agent == "human":
                    tag = f"[{ts}] {from_label} → {to_label}"
                elif msg.to_agent == "human":
                    tag = f"[{ts}] {from_label} → {to_label}"
                else:
                    tag = f"[{ts}] {from_label} → {to_label}"

                lines.append(("bold", f"  {tag}\n"))

                # Mentions
                if msg.mentions:
                    mentions_str = ", ".join(f"@{m}" for m in msg.mentions)
                    lines.append(("", f"  @mentions: {mentions_str}\n"))

                # Content
                content = msg.content[:800]
                if len(msg.content) > 800:
                    content += "…"
                lines.append(("", f"  {content}\n"))
                lines.append(("", "\n"))

            return lines

        # ── Layout ────────────────────────────────────────────────

        message_control = FormattedTextControl(
            text=get_message_text,
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
            height=Dimension(min=1, max=6),
            wrap_lines=True,
        )

        root_container = HSplit([
            message_window,
            Window(height=1, char="─"),
            input_window,
        ])

        # ── Application ────────────────────────────────────────────

        self._app = Application(
            layout=Layout(root_container),
            key_bindings=kb,
            full_screen=True,
        )

        # Start WebSocket connection task
        ws_task = asyncio.create_task(self._ws_loop())
        # Refresh the display periodically so new messages appear promptly
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
        """Read messages from the WebSocket."""
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

            # Deduplicate: skip messages we've already seen
            if msg.id in self._seen_ids:
                continue
            self._seen_ids.add(msg.id)

            # Filter: show messages relevant to this agent
            if not self._is_relevant(msg):
                continue

            self._messages.append(msg)
            if self._app:
                self._app.invalidate()

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

    async def _send_message(self, text: str) -> None:
        """Publish a user message to the bus."""
        # Handle special commands
        if text.startswith("/"):
            await self._handle_command(text)
            return

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

        # Track locally so we don't duplicate when WS echoes it back
        self._seen_ids.add(msg.id)
        self._messages.append(msg)
        if self._app:
            self._app.invalidate()

        # Publish to bus
        assert self._http is not None
        try:
            resp = await self._http.post(
                f"{self.server_url}/api/v1/publish",
                json=msg.model_dump(mode="json"),
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            err_msg = Message(
                from_agent="system",
                to_agent="human",
                type="conclusion",
                content=f"❌ 发送失败: {exc}",
                evidence=[],
                confidence="low",
            )
            self._messages.append(err_msg)
            if self._app:
                self._app.invalidate()

    async def _handle_command(self, text: str) -> None:
        """Handle /slash commands."""
        cmd = text.strip().lower()

        if cmd in ("/quit", "/exit", "/q"):
            if self._app:
                self._app.exit()
            sentinel = os.environ.get("DUCKAGENT_TMUX_QUIT_FILE")
            if sentinel:
                Path(sentinel).touch()

        elif cmd in ("/clear", "/cls"):
            self._messages.clear()
            if self._app:
                self._app.invalidate()

        elif cmd == "/agents":
            self._add_system_msg(
                "Agent 状态请查看 tmux 窗口 4 (status)。\n切换: 鼠标点击底部 status bar 或 Ctrl+B 4"
            )

        else:
            self._add_system_msg(f"未知命令: {text}\n可用: /quit, /clear, /agents")

    def _add_system_msg(self, content: str) -> None:
        """Show a system message locally (not sent to bus)."""
        msg = Message(
            from_agent="system",
            to_agent="human",
            type="conclusion",
            content=content,
            evidence=[],
            confidence="high",
        )
        self._messages.append(msg)
        if self._app:
            self._app.invalidate()

    async def _refresh_loop(self) -> None:
        """Periodically invalidate the UI so new messages appear."""
        while self._running:
            await asyncio.sleep(0.2)
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
