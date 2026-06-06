"""Tmux-based TUI for DuckAgent.

Replaces the Textual TUI with a tmux session containing 5 windows:
- 3 agent chat windows (prompt_toolkit interactive)
- 1 bus monitor window (read-only message stream)
- 1 status dashboard window (read-only agent status)
"""

# Lazy imports — import submodules directly to avoid
# __init__.py side-effects when running as __main__.
__all__ = ["TmuxSession", "ChatApp", "BusMonitor", "StatusDashboard"]


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
    raise AttributeError(f"module 'duckagent.tmux' has no attribute {name!r}")
