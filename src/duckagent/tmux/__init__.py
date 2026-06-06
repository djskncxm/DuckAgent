"""Tmux-based TUI for DuckAgent (v2 — two-pane agent windows).

Architecture:
    Window 0-2 (agent):
        top pane (80%)  — agent process, stdout IS the chat display
        bottom pane (20%) — input_pane, sends messages to bus via HTTP
    Window 3 (messages): bus monitor (WebSocket observer)
    Window 4 (status):   status dashboard (WebSocket status subscriber)

Modules:
    session.py         — TmuxSession: creates tmux layout + manages lifecycle
    console.py         — AgentConsole: rich-formatted stdout for agent processes
    input_pane.py      — Standalone prompt_toolkit input (bottom pane)
    local_app.py       — Single-process prompt_toolkit chat (--local mode)
    bus_monitor.py     — WebSocket observer (window 3)
    status_dashboard.py — WebSocket status subscriber (window 4)
    chat_app.py        — ⚠️ Deprecated (was v1 combined display+input, replaced by two-pane)
"""

# Lazy imports — import submodules directly to avoid
# __init__.py side-effects when running as __main__.
__all__ = [
    "AgentConsole",
    "BusMonitor",
    "ChatApp",
    "StatusDashboard",
    "TmuxSession",
    "main_input",
    "main_local",
]


def __getattr__(name: str):
    if name == "TmuxSession":
        from duckagent.tmux.session import TmuxSession
        return TmuxSession
    if name == "ChatApp":
        from duckagent.tmux.chat_app import ChatApp
        return ChatApp
    if name == "BusMonitor":
        from duckagent.tmux.bus_monitor import BusMonitor
        return BusMonitor
    if name == "StatusDashboard":
        from duckagent.tmux.status_dashboard import StatusDashboard
        return StatusDashboard
    if name == "AgentConsole":
        from duckagent.tmux.console import AgentConsole
        return AgentConsole
    if name == "main_input":
        from duckagent.tmux.input_pane import main
        return main
    if name == "main_local":
        from duckagent.tmux.local_app import main
        return main
    raise AttributeError(f"module 'duckagent.tmux' has no attribute {name!r}")
