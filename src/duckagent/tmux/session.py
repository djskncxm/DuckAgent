"""Tmux session manager for DuckAgent.

Creates a tmux session with 5 windows and manages the lifecycle of
the bus server and agent processes.

Key principle: tmux goes up FIRST, then tasks connect to it.
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
_HEALTH_POLL_INTERVAL = 0.5
_HEALTH_TIMEOUT = 15.0


class TmuxSession:
    """Manages a DuckAgent tmux session with 5 windows.

    Parameters
    ----------
    server_url:
        Bus server URL, e.g. "http://127.0.0.1:8720".
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
        self._procs: list[tuple[str, subprocess.Popen[bytes]]] = []
        self._python = sys.executable
        self._quit_file: Path | None = None

    # ── Public API ─────────────────────────────────────────────────

    def start(self) -> None:
        """Start tmux session, bus server, and agents. Blocks until exit."""
        self._quit_file = Path(tempfile.mktemp(suffix=".duckagent-quit"))

        try:
            # 1. Create tmux session FIRST (background — no client attached yet)
            self._create_tmux_session()

            # 2. Start bus server
            self._start_server()

            # 3. Wait for server readiness
            self._wait_for_server()

            # 4. Start agent processes
            for agent_type in self._agents:
                self._start_agent(agent_type)

            # 5. Background watcher for quit file / process death
            self._start_watcher()

            # 6. Attach current terminal to the tmux session (blocks until detach/exit)
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
        # Kill agent processes first
        for name, proc in reversed(self._procs):
            if proc.poll() is None:
                proc.terminate()
        # Give them time to exit
        deadline = time.monotonic() + 3.0
        for name, proc in self._procs:
            timeout = max(0.0, deadline - time.monotonic())
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
        self._procs.clear()

        # Kill tmux session
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
        """Create the tmux session with 5 windows."""
        self._server = libtmux.Server()

        # Kill existing duckagent session if any
        try:
            existing = self._server.sessions.get(session_name="duckagent")
            if existing:
                existing.kill()
        except Exception:
            pass

        # Create new session — the first window is auto-created
        self._session = self._server.new_session(
            session_name="duckagent",
            kill_session=True,
            start_directory=str(Path.cwd()),
        )

        # Enable mouse — click to switch windows
        self._session.set_option("mouse", "on")

        # Rename the auto-created first window to "main"
        first_win = self._session.active_window
        first_win.rename_window("main")

        # Start the main_agent chat app in the first window
        assert self._quit_file is not None
        quit_env = f"DUCKAGENT_TMUX_QUIT_FILE={self._quit_file}"
        main_pane = first_win.active_pane
        main_pane.send_keys(
            f"{quit_env} {self._python} -m duckagent.tmux.chat_app "
            f"--agent-id main_agent --server-url {self.server_url}",
            enter=True,
        )

        # Create remaining windows
        remaining = [
            ("trace", "trace_agent"),
            ("ida", "ida_jadx_agent"),
            ("messages", None),   # bus monitor
            ("status", None),     # status dashboard
        ]

        for win_name, agent_id in remaining:
            win = self._session.new_window(
                win_name,
                attach=False,
                start_directory=str(Path.cwd()),
            )
            pane = win.active_pane

            if agent_id:
                # Agent chat window (all have quit sentinel for full shutdown)
                pane.send_keys(
                    f"{quit_env} {self._python} -m duckagent.tmux.chat_app "
                    f"--agent-id {agent_id} --server-url {self.server_url}",
                    enter=True,
                )
            elif win_name == "messages":
                # Bus monitor window
                pane.send_keys(
                    f"{self._python} -m duckagent.tmux.bus_monitor "
                    f"--server-url {self.server_url}",
                    enter=True,
                )
            elif win_name == "status":
                # Status dashboard window
                pane.send_keys(
                    f"{self._python} -m duckagent.tmux.status_dashboard "
                    f"--server-url {self.server_url}",
                    enter=True,
                )

        # Switch back to main window as default
        try:
            self._session.select_window("main")
        except Exception:
            pass

        logger.info("tmux_session_created", windows=["main", "trace", "ida", "messages", "status"])

    # ── Process management ─────────────────────────────────────────

    def _start_server(self) -> None:
        """Start the bus server as a background subprocess."""
        proc = subprocess.Popen(
            [
                self._python, "-m", "uvicorn", "duckagent.server.app:app",
                "--host", "127.0.0.1", "--port", str(self.server_port),
                "--log-level", "warning",
            ],
        )
        self._procs.append(("server", proc))
        logger.info("bus_server_started", port=self.server_port)

    def _wait_for_server(self) -> None:
        """Poll /api/v1/health until the server responds or timeout."""
        deadline = time.monotonic() + _HEALTH_TIMEOUT
        while time.monotonic() < deadline:
            server_name, server_proc = self._procs[0]
            if server_proc.poll() is not None:
                raise RuntimeError(
                    f"Bus server exited early with code {server_proc.returncode}"
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

    def _start_agent(self, agent_type: str) -> None:
        """Start a single agent process."""
        proc = subprocess.Popen(
            [
                self._python, "-m", "duckagent.processes.agent_process",
                agent_type,
                "--server-url", self.server_url,
            ],
        )
        self._procs.append((agent_type, proc))
        logger.info("agent_started", agent_type=agent_type)

    # ── Shutdown handling ──────────────────────────────────────────

    def _start_watcher(self) -> None:
        """Start a daemon thread that monitors the quit file and child procs.

        When a shutdown signal is detected, the tmux session is killed,
        which unblocks ``tmux attach`` in ``start()``.
        """
        import threading

        def _watch() -> None:
            while True:
                # Check quit file (touched by /quit command in any chat window)
                if self._quit_file and self._quit_file.exists():
                    logger.info("quit_file_detected")
                    self._kill_tmux_session()
                    return
                # Check if any child process died unexpectedly
                for name, proc in self._procs:
                    if proc.poll() is not None:
                        logger.warning(
                            "child_process_died",
                            name=name,
                            returncode=proc.returncode,
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
