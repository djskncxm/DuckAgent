"""Model status dashboard — real-time agent state display.

Usage:
    python -m duckagent.tmux.status_dashboard --server-url http://127.0.0.1:8720

Connects to the bus status WebSocket and displays a live-updating dashboard
showing each agent's state, current task, and tool usage.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time

import structlog
import websockets
import websockets.asyncio.client

logger = structlog.get_logger()

_STATE_GLYPHS = {
    "idle": ".",
    "thinking": "*",
    "tool_calling": "#",
    "error": "!",
}

_KNOWN_AGENTS = ["main_agent", "trace_agent", "ida_jadx_agent"]


def _ansi_len(s: str) -> int:
    """Visible length of a string (strip ANSI escape sequences)."""
    import re
    return len(re.sub(r"\033\[[0-9;]*m", "", s))


class StatusDashboard:
    """Live agent status dashboard.

    Parameters
    ----------
    server_url:
        Bus server URL, e.g. "http://127.0.0.1:8720".
    """

    def __init__(self, server_url: str) -> None:
        self.server_url = server_url.rstrip("/")
        self._ws_url = server_url.replace("http://", "ws://").rstrip("/") + "/ws?role=status"
        self._running = False
        self._started_at = time.monotonic()
        self._msg_count = 0
        self._agent_states: dict[str, dict] = {
            agent: {
                "state": "idle",
                "task_summary": "-",
                "tool_name": "",
                "tool_args_summary": "",
                "last_error": "",
                "last_api_latency": "",
            }
            for agent in _KNOWN_AGENTS
        }
        self._width = 60

    def run(self) -> None:
        """Blocking entry point."""
        asyncio.run(self._run())

    async def _run(self) -> None:
        self._running = True

        ws_task = asyncio.create_task(self._ws_loop())
        redraw_task = asyncio.create_task(self._redraw_loop())

        try:
            await asyncio.gather(ws_task, redraw_task)
        except asyncio.CancelledError:
            pass
        finally:
            ws_task.cancel()
            redraw_task.cancel()

    async def _ws_loop(self) -> None:
        """Connect to status WebSocket and update agent states."""
        while self._running:
            try:
                ws = await websockets.asyncio.client.connect(
                    self._ws_url, open_timeout=5.0
                )
                async for raw in ws:
                    try:
                        payload = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if payload.get("type") != "message":
                        continue
                    data = payload.get("data")
                    if not isinstance(data, dict):
                        continue
                    if data.get("type") != "status":
                        continue

                    try:
                        status = json.loads(data.get("content", "{}"))
                    except json.JSONDecodeError:
                        continue

                    agent_id = data.get("from_agent", "")
                    if agent_id not in self._agent_states:
                        self._agent_states[agent_id] = {}

                    entry = self._agent_states[agent_id]
                    entry["state"] = status.get("state", entry.get("state", "idle"))
                    entry["task_summary"] = status.get("task_summary", entry.get("task_summary", "-"))
                    entry["tool_name"] = status.get("tool_name", entry.get("tool_name", ""))
                    entry["tool_args_summary"] = status.get("tool_args_summary", entry.get("tool_args_summary", ""))
                    entry["last_error"] = status.get("last_error", entry.get("last_error", ""))
                    entry["last_api_latency"] = status.get("last_api_latency", entry.get("last_api_latency", ""))

                    self._msg_count += 1
            except asyncio.CancelledError:
                return
            except Exception:
                await asyncio.sleep(2.0)

    async def _redraw_loop(self) -> None:
        """Redraw the dashboard every second."""
        while self._running:
            self._render()
            await asyncio.sleep(1.0)

    def _render(self) -> None:
        """Render the full dashboard to stdout."""
        try:
            self._width = max(60, os.get_terminal_size().columns)
        except (ValueError, OSError):
            pass
        w = self._width

        sys.stdout.write("\033[H\033[J")  # clear screen
        print("=" * w)
        print("  DuckAgent Status Dashboard")
        print("=" * w)
        print()

        for agent_id in _KNOWN_AGENTS:
            entry = self._agent_states.get(agent_id, {})
            state = entry.get("state", "idle")
            glyph = _STATE_GLYPHS.get(state, "?")
            task = entry.get("task_summary", "-") or "-"
            tool = entry.get("tool_name", "")
            tool_args = entry.get("tool_args_summary", "")
            latency = entry.get("last_api_latency", "")
            error = entry.get("last_error", "")

            print(f"  [{glyph}] {agent_id:20s}  {state}")

            task_display = task[:w - 30]
            print(f"       task: {task_display}")

            if tool:
                tool_display = f"{tool}({tool_args})" if tool_args else tool
                print(f"       tool: {tool_display[:w - 30]}")

            if latency:
                print(f"       latency: {latency}")

            if error:
                print(f"       ERROR: {error[:w - 30]}")

            print()

        runtime = time.monotonic() - self._started_at
        runtime_str = f"{int(runtime // 60)}m {int(runtime % 60)}s"

        print("-" * w)
        print(f"  Bus: {self.server_url}  |  Messages: {self._msg_count}  |  Uptime: {runtime_str}")
        print("=" * w)

        sys.stdout.flush()


# ── CLI entry point ────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="DuckAgent status dashboard")
    parser.add_argument("--server-url", required=True, help="Bus server URL")
    args = parser.parse_args()

    dashboard = StatusDashboard(server_url=args.server_url)
    try:
        dashboard.run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
