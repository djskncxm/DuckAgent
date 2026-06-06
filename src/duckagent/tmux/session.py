"""Tmux session manager for DuckAgent.

Creates a tmux session with 5 windows and manages the lifecycle of
the bus server, agent processes, and TUI components.

Key principle: tmux goes up FIRST, then processes run inside it.

Architecture (v2 — two-pane agent windows):
    Window 0-2 (agent):
        top pane (80%)  — agent process, stdout IS the chat display
        bottom pane (20%) — input_pane, sends messages to bus
    Window 3 (messages): bus monitor (WebSocket observer)
    Window 4 (status):   status dashboard (WebSocket status subscriber)
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx
import libtmux
import structlog

logger = structlog.get_logger()

_DEFAULT_AGENTS = ["main_agent", "trace_agent", "ida_jadx_agent"]
_AGENT_WINDOW_NAMES = {
    "main_agent": "main",
    "trace_agent": "trace",
    "ida_jadx_agent": "ida",
}

_HEALTH_POLL_INTERVAL = 0.5
_HEALTH_TIMEOUT = 15.0


class TmuxSession:
    """Manages a DuckAgent tmux session with 5 windows.

    Parameters
    ----------
    server_url:
        Bus server URL, e.g. ``"http://127.0.0.1:8720"``.
    agents:
        List of agent types to spawn (default: all 3).
    server_port:
        Port the bus server listens on.
    """

    def __init__(
        self,
        server_url: str,
        agents: list[str] | None = None,
        server_port: int = 8720,
    ) -> None:
        self.server_url = server_url.rstrip("/")
        self.server_port = server_port
        self._agents = agents or _DEFAULT_AGENTS
        self._server: libtmux.Server | None = None
        self._session: Any = None
        self._server_proc: subprocess.Popen[bytes] | None = None
        self._python = sys.executable
        self._quit_file: Path | None = None

    # ── Public API ─────────────────────────────────────────────────

    def start(self) -> None:
        """Start tmux session, bus server, and agents.  Blocks until exit."""
        self._quit_file = Path(tempfile.mktemp(suffix=".duckagent-quit"))

        try:
            # 1. Create tmux session FIRST (all windows + panes)
            self._create_tmux_session()

            # 2. Start bus server as managed subprocess
            self._start_server()

            # 3. Wait for server readiness
            self._wait_for_server()

            # 4. Start agent processes in agent-window top panes
            for agent_type in self._agents:
                self._start_agent_in_pane(agent_type)

            # 5. Background watcher for quit file
            self._start_watcher()

            # 6. Attach current terminal to the tmux session (blocks)
            subprocess.run(
                ["tmux", "attach-session", "-t", "duckagent"],
                check=False,
            )

        except Exception as exc:
            logger.error("tmux_session_error", error=str(exc))
            raise
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        """Clean up all resources."""
        # Kill server subprocess
        if self._server_proc and self._server_proc.poll() is None:
            self._server_proc.terminate()
            try:
                self._server_proc.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                self._server_proc.kill()

        # Kill tmux session (kills all pane processes)
        if self._session:
            try:
                self._session.kill()
            except Exception:
                pass
            self._session = None

        # Clean up quit file
        if self._quit_file and self._quit_file.exists():
            try:
                self._quit_file.unlink()
            except Exception:
                pass

    # ── Tmux session creation ──────────────────────────────────────

    def _create_tmux_session(self) -> None:
        """Create the tmux session with all 5 windows."""
        self._server = libtmux.Server()

        # Kill existing duckagent session if any
        try:
            existing = self._server.sessions.get(session_name="duckagent")
            if existing:
                existing.kill()
        except Exception:
            pass

        # Create new session — first window auto-created
        self._session = self._server.new_session(
            session_name="duckagent",
            kill_session=True,
            start_directory=str(Path.cwd()),
        )

        # Enable mouse — click status bar to switch windows
        self._session.set_option("mouse", "on")

        # Rename auto-created window to "main"
        first_win = self._session.active_window
        first_win.rename_window("main")

        # Window 0: main_agent — split into top (agent) + bottom (input)
        self._setup_agent_window(first_win, "main_agent")

        # Window 1: trace_agent
        win1 = self._session.new_window(
            "trace", attach=False, start_directory=str(Path.cwd()),
        )
        self._setup_agent_window(win1, "trace_agent")

        # Window 2: ida_jadx_agent
        win2 = self._session.new_window(
            "ida", attach=False, start_directory=str(Path.cwd()),
        )
        self._setup_agent_window(win2, "ida_jadx_agent")

        # Window 3: messages — bus monitor (read-only observer)
        win3 = self._session.new_window(
            "messages", attach=False, start_directory=str(Path.cwd()),
        )
        win3.active_pane.send_keys(
            f"{self._python} -m duckagent.tmux.bus_monitor "
            f"--server-url {self.server_url}",
            enter=True,
        )

        # Window 4: status — agent status dashboard
        win4 = self._session.new_window(
            "status", attach=False, start_directory=str(Path.cwd()),
        )
        win4.active_pane.send_keys(
            f"{self._python} -m duckagent.tmux.status_dashboard "
            f"--server-url {self.server_url}",
            enter=True,
        )

        # Switch back to main window
        try:
            self._session.select_window("main")
        except Exception:
            pass

        logger.info(
            "tmux_session_created",
            windows=["main", "trace", "ida", "messages", "status"],
        )

    def _setup_agent_window(self, window: Any, agent_id: str) -> None:
        """Split a window into top (agent stdout) + bottom (input) panes.

        The agent process is NOT started yet — that happens in
        ``_start_agent_in_pane`` after the bus server is healthy.
        The input process starts immediately (it will retry connection).
        """
        assert self._quit_file is not None
        quit_env = f"DUCKAGENT_TMUX_QUIT_FILE={self._quit_file}"

        # The active pane is the only pane — it will become the TOP pane
        top_pane = window.active_pane

        # Split bottom pane for input (only 1 row)
        bottom_pane = top_pane.split(attach=False)
        # Set bottom to 1 row; tmux gives remaining space to top automatically
        bottom_pane.set_height(height=1)

        # Start input process in bottom pane immediately
        # (it will retry connection until bus is ready, then show prompt)
        bottom_pane.send_keys(
            f"{quit_env} {self._python} -m duckagent.tmux.input_pane "
            f"--agent-id {agent_id} --server-url {self.server_url}",
            enter=True,
        )

        # Store a marker so _start_agent_in_pane can find the top pane
        window.set_window_option(f"@duckagent_agent_id", agent_id)

        logger.info("agent_window_ready", agent_id=agent_id)

    # ── Process management ─────────────────────────────────────────

    def _start_server(self) -> None:
        """Start the bus server as a background subprocess."""
        self._server_proc = subprocess.Popen(
            [
                self._python, "-m", "uvicorn", "duckagent.server.app:app",
                "--host", "127.0.0.1", "--port", str(self.server_port),
                "--log-level", "warning",
            ],
        )
        logger.info("bus_server_started", port=self.server_port)

    def _wait_for_server(self) -> None:
        """Poll /api/v1/health until the server responds or timeout."""
        deadline = time.monotonic() + _HEALTH_TIMEOUT
        while time.monotonic() < deadline:
            if self._server_proc and self._server_proc.poll() is not None:
                raise RuntimeError(
                    f"Bus server exited early with code {self._server_proc.returncode}"
                )
            try:
                resp = httpx.get(
                    f"{self.server_url}/api/v1/health",
                    timeout=1.0,
                )
                if resp.status_code == 200:
                    logger.info("bus_server_ready")
                    return
            except httpx.HTTPError:
                pass
            time.sleep(_HEALTH_POLL_INTERVAL)

        raise TimeoutError(
            f"Bus server did not become healthy within {_HEALTH_TIMEOUT}s"
        )

    def _start_agent_in_pane(self, agent_type: str) -> None:
        """Start the agent process in its window's top pane via send_keys.

        The agent's stdout (with rich-formatted messages) becomes
        the visible chat display in the top pane.
        """
        window_name = _AGENT_WINDOW_NAMES.get(agent_type)
        if not window_name or self._session is None:
            return

        try:
            win = self._session.windows.get(window_name=window_name)
            if win:
                panes = win.panes
                if panes:
                    # First pane (index 0) is the top pane
                    top_pane = panes[0]
                    top_pane.send_keys(
                        f"{self._python} -m duckagent.processes.agent_process "
                        f"{agent_type} --server-url {self.server_url}",
                        enter=True,
                    )
                    logger.info("agent_pane_started", agent_type=agent_type)
        except Exception as exc:
            logger.warning(
                "agent_pane_send_keys_failed",
                agent_type=agent_type,
                error=str(exc)[:100],
            )

    # ── Shutdown handling ──────────────────────────────────────────

    def _start_watcher(self) -> None:
        """Start a daemon thread that monitors the quit file.

        When the quit file is touched (by /quit in any input pane),
        the tmux session is killed, unblocking ``tmux attach``.
        Also watches the server process for unexpected death.
        """
        import threading

        def _watch() -> None:
            while True:
                # Check quit file (touched by /quit in any input pane)
                if self._quit_file and self._quit_file.exists():
                    logger.info("quit_file_detected")
                    self._kill_tmux_session()
                    return
                # Check if server process died unexpectedly
                if self._server_proc and self._server_proc.poll() is not None:
                    logger.warning(
                        "server_process_died",
                        returncode=self._server_proc.returncode,
                    )
                    self._kill_tmux_session()
                    return
                time.sleep(0.5)

        self._watcher_thread = threading.Thread(target=_watch, daemon=True)
        self._watcher_thread.start()

    def _kill_tmux_session(self) -> None:
        """Kill the tmux session (safe to call multiple times)."""
        if self._session:
            try:
                self._session.kill()
            except Exception:
                pass
