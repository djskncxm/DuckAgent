"""Multi-process launcher.

Spawns the tmux session, bus server, and agent processes.

Usage:
    uv run duck run
"""

from __future__ import annotations

from duckagent.tmux.session import TmuxSession

_DEFAULT_PORT = 8720
_DEFAULT_AGENTS = ["main_agent", "trace_agent", "ida_jadx_agent"]


class Launcher:
    """Manages the lifecycle of all DuckAgent processes via tmux."""

    def __init__(
        self,
        server_port: int = _DEFAULT_PORT,
        agents: list[str] | None = None,
        use_tmux: bool = True,
    ) -> None:
        self._server_port = server_port
        self._agents = agents or _DEFAULT_AGENTS
        self._use_tmux = use_tmux

    def start(self) -> None:
        """Start the tmux session + server + agents. Blocks until exit."""
        server_url = f"http://127.0.0.1:{self._server_port}"

        if not self._use_tmux:
            raise RuntimeError(
                "Non-tmux mode has been removed. Use --local for single-process mode."
            )

        tmux = TmuxSession(
            server_url=server_url,
            agents=self._agents,
            server_port=self._server_port,
        )
        tmux.start()
