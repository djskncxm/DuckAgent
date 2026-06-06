"""Prompt_toolkit single-process local chat (--local mode).

All agents run in the same asyncio event loop with LocalMessageBus.
A single prompt_toolkit input sends to main_agent by default;
@mention routes to specific agents.

Usage:
    python -m duckagent.tmux.local_app
"""

from __future__ import annotations

import asyncio

from rich.console import Console

from duckagent.bus import LocalMessageBus, Message
from duckagent.bus.models import parse_mentions
from duckagent.config import settings


async def run_local(port: int | None = None) -> None:
    """Run the local (single-process) TUI.

    Parameters
    ----------
    port:
        Ignored in local mode (all communication is in-process via
        ``LocalMessageBus``).  Accepted for CLI compatibility.
    """
    # Use Rich console for clean output
    console = Console(force_terminal=True, color_system="standard")
    console.print()
    console.rule("[bold cyan]DuckAgent — Local Mode[/bold cyan]")
    console.print(
        "  [dim]All agents run in-process with LocalMessageBus.[/dim]"
    )
    console.print(
        "  [dim]Enter sends to @main_agent by default.  Use @trace_agent or @ida_jadx_agent to route.[/dim]"
    )
    console.print("  [dim]/quit to exit, /clear to clear screen[/dim]")
    console.print()

    # ── Create bus and agents ──────────────────────────────────────
    bus = LocalMessageBus(db_path=settings.db_path)
    await bus.initialize()

    agents = []
    try:
        from duckagent.agents.main_agent import MainAgent
        from duckagent.agents.trace_agent import TraceAgent
        from duckagent.agents.ida_jadx_agent import IdaJadxAgent
        from pathlib import Path

        prompts_dir = Path(settings.prompts_dir)
        agent_md_path = Path(__file__).resolve().parents[3] / "AGENT.md"

        main = MainAgent(
            bus=bus, model=settings.litellm_model,
            agent_md_path=agent_md_path, prompts_dir=prompts_dir,
        )
        trace = TraceAgent(
            bus=bus, model=settings.litellm_model, prompts_dir=prompts_dir,
        )
        ida_jadx = IdaJadxAgent(
            bus=bus, model=settings.litellm_model, prompts_dir=prompts_dir,
        )

        for agent in [main, trace, ida_jadx]:
            await agent.start()
            agents.append(agent)

        console.print("[green]All agents started.[/green]")
        console.print()

        # ── prompt_toolkit input loop ──────────────────────────────
        from prompt_toolkit import PromptSession
        from prompt_toolkit.completion import WordCompleter
        from prompt_toolkit.history import FileHistory
        from prompt_toolkit.styles import Style
        from pathlib import Path as Pt

        history_dir = Pt.home() / ".duckagent"
        history_dir.mkdir(parents=True, exist_ok=True)

        completer = WordCompleter(
            ["@main_agent", "@trace_agent", "@ida_jadx_agent", "@human"],
            ignore_case=True, sentence=True,
        )
        style = Style.from_dict({"prompt": "#00d700 bold"})
        session = PromptSession(
            history=FileHistory(str(history_dir / "history_local")),
            completer=completer,
            style=style,
        )

        while True:
            try:
                text = await session.prompt_async([("class:prompt", "> ")])
            except (EOFError, KeyboardInterrupt):
                break

            text = text.strip()
            if not text:
                continue

            if text.lower() in ("/quit", "/exit", "/q"):
                break

            if text.lower() in ("/clear", "/cls"):
                console.clear()
                continue

            if text.lower() == "/agents":
                console.print("  Agents: main_agent, trace_agent, ida_jadx_agent")
                continue

            # Route: @mention → specific agent, otherwise main_agent
            mentions = parse_mentions(text)
            target = mentions[0] if mentions else "main_agent"

            msg = Message(
                from_agent="human",
                to_agent=target,
                mentions=mentions,
                type="request",
                content=text,
                evidence=[],
                confidence="high",
            )
            await bus.publish(msg)

    finally:
        for agent in reversed(agents):
            try:
                await agent.stop()
            except Exception:
                pass
        await bus.close()

    console.print("[dim]Shutdown complete.[/dim]")


def main() -> None:
    """CLI entry point."""
    asyncio.run(run_local())
