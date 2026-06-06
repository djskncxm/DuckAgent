"""Standalone prompt_toolkit input process for a tmux agent pane.

Runs in the **bottom** pane of each agent window.  Its only job is to
accept user input and POST it to the bus server.  The agent's own stdout
(seen in the **top** pane) is what displays received messages.

Usage:
    python -m duckagent.tmux.input_pane --agent-id main_agent --server-url http://127.0.0.1:8720

Key bindings:
    Enter       — send message
    Alt+Enter   — insert newline (experimental: Escape then Enter)
    Ctrl+C/D    — quit
    /quit, /q   — quit
    /clear      — clear screen (sends ANSI clear to parent tmux pane — no-op here)
    /agents     — print agent list
"""

from __future__ import annotations

import argparse
import asyncio
import atexit
import os
from pathlib import Path

import httpx

from duckagent.bus.models import Message, parse_mentions

HISTORY_DIR = Path.home() / ".duckagent"
MENTION_WORDS = [
    "@main_agent",
    "@trace_agent",
    "@ida_jadx_agent",
    "@human",
]


def _ensure_history_dir() -> None:
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)


# ── Slash commands ──────────────────────────────────────────────────

def _handle_slash(text: str, agent_id: str) -> str | None:
    """Handle /slash commands.

    Returns 'quit' to signal the main loop to exit, or None to continue.
    """
    cmd = text.strip().lower()

    if cmd in ("/quit", "/exit", "/q"):
        # Touch quit file to signal the session watcher
        quit_file = os.environ.get("DUCKAGENT_TMUX_QUIT_FILE")
        if quit_file:
            Path(quit_file).touch()
        return "quit"

    if cmd in ("/clear", "/cls"):
        # Send ANSI clear screen + home cursor
        print("\033[2J\033[H", end="", flush=True)
        return None

    if cmd == "/agents":
        print(f"\n  Agents: main_agent, trace_agent, ida_jadx_agent")
        print(f"  Current window: {agent_id}\n")
        return None

    if cmd.startswith("/"):
        print(f"\n  Unknown command: {text}")
        print(f"  Available: /quit, /clear, /agents\n")
        return None

    return None


# ── Main ────────────────────────────────────────────────────────────


async def run_input(agent_id: str, server_url: str) -> None:
    """Async entry point — prompt_toolkit loop with httpx sender."""

    # Touch quit file on exit so the session watcher shuts down tmux
    def _signal_quit() -> None:
        quit_file = os.environ.get("DUCKAGENT_TMUX_QUIT_FILE")
        if quit_file:
            Path(quit_file).touch()

    atexit.register(_signal_quit)

    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import WordCompleter
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.styles import Style

    _ensure_history_dir()
    history_file = str(HISTORY_DIR / f"history_{agent_id}")

    completer = WordCompleter(MENTION_WORDS, ignore_case=True, sentence=True)

    kb = KeyBindings()

    @kb.add("escape", "enter")
    def _(event: "Any") -> None:  # noqa: F821
        """Alt+Enter: insert literal newline."""
        event.app.current_buffer.insert_text("\n")

    style = Style.from_dict({
        "prompt": "#00d700 bold",        # bright green prompt "> "
    })

    session = PromptSession(
        history=FileHistory(history_file),
        completer=completer,
        key_bindings=kb,
        style=style,
    )

    print(f"\033[2J\033[H", end="", flush=True)  # clear pane on start
    print(f"  [{agent_id}] 输入消息，Enter 发送，Alt+Enter 换行，/quit 退出")
    print()

    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
        while True:
            try:
                text = await session.prompt_async(
                    [("class:prompt", "> ")],
                )
            except (EOFError, KeyboardInterrupt):
                break

            text = text.strip()
            if not text:
                continue

            # Slash commands
            result = _handle_slash(text, agent_id)
            if result == "quit":
                break
            if result is not None:
                continue
            if text.startswith("/"):
                continue  # already handled

            # Parse @mentions
            mentions = parse_mentions(text)

            msg = Message(
                from_agent="human",
                to_agent=agent_id,
                mentions=mentions,
                type="request",
                content=text,
                evidence=[],
                confidence="high",
            )

            # Send to bus
            try:
                resp = await client.post(
                    f"{server_url}/api/v1/publish",
                    json=msg.model_dump(mode="json"),
                )
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                print(f"\n  ❌ 发送失败: {exc}\n")
            except Exception as exc:
                print(f"\n  ❌ 发送异常: {exc}\n")


# ── CLI entry point ────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="DuckAgent input pane (tmux bottom pane)"
    )
    parser.add_argument(
        "--agent-id",
        required=True,
        help="Agent ID for this window (main_agent, trace_agent, ida_jadx_agent)",
    )
    parser.add_argument(
        "--server-url",
        required=True,
        help="Bus server URL, e.g. http://127.0.0.1:8720",
    )
    args = parser.parse_args()
    asyncio.run(run_input(args.agent_id, args.server_url))


if __name__ == "__main__":
    main()
