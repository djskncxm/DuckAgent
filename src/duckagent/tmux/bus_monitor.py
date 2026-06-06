"""Bus monitor — read-only view of all messages flowing through the bus.

Usage:
    python -m duckagent.tmux.bus_monitor --server-url http://127.0.0.1:8720

Connects as a WebSocket observer and displays every non-status message.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime

import structlog
import websockets
import websockets.asyncio.client

from duckagent.bus.models import Message

logger = structlog.get_logger()


def fmt_ts(ts: datetime | str) -> str:
    if isinstance(ts, datetime):
        return ts.strftime("%H:%M:%S")
    return str(ts)[:8]


def render(msg: Message) -> str:
    """Render one message as a single line."""
    ts = fmt_ts(msg.timestamp)
    from_label = "you" if msg.from_agent == "human" else msg.from_agent
    to_label = "you" if msg.to_agent == "human" else (msg.to_agent or "all")
    type_label = msg.type[:4].upper()

    # Truncate content
    content = msg.content.replace("\n", " ")[:120]
    if len(msg.content) > 120:
        content += "…"

    parts = [
        f"[{ts}]",
        f"{from_label} -> {to_label}",
        f"[{type_label}]",
        content,
    ]

    if msg.mentions:
        parts.append(" ".join(f"@{m}" for m in msg.mentions))

    return "  ".join(parts)


class BusMonitor:
    """Read-only WebSocket observer that prints all bus messages.

    Parameters
    ----------
    server_url:
        Bus server URL, e.g. "http://127.0.0.1:8720".
    """

    def __init__(self, server_url: str) -> None:
        self.server_url = server_url.rstrip("/")
        self._ws_url = server_url.replace("http://", "ws://").rstrip("/") + "/ws?role=observer"
        self._running = False
        self._msg_count = 0

    def run(self) -> None:
        """Blocking entry point."""
        asyncio.run(self._run())

    async def _run(self) -> None:
        self._running = True
        print(f"=== DuckAgent Bus Monitor ===")
        print(f"Server: {self.server_url}")
        print(f"Waiting for messages...\n")
        sys.stdout.flush()

        while self._running:
            try:
                ws = await websockets.asyncio.client.connect(
                    self._ws_url, open_timeout=5.0
                )
                print("[connected]\n")
                sys.stdout.flush()
                await self._read_loop(ws)
            except asyncio.CancelledError:
                return
            except (websockets.ConnectionClosed, OSError) as exc:
                print(f"[disconnected: {str(exc)[:60]}]")
                print("[reconnecting in 2s...]")
                sys.stdout.flush()
                await asyncio.sleep(2.0)
            except Exception as exc:
                print(f"[connection failed: {str(exc)[:60]}]")
                print("[retrying in 3s...]")
                sys.stdout.flush()
                await asyncio.sleep(3.0)

    async def _read_loop(self, ws) -> None:
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
            try:
                msg = Message(**data)
            except Exception:
                continue

            if msg.type == "status":
                continue

            self._msg_count += 1
            print(render(msg))
            sys.stdout.flush()


# ── CLI entry point ────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="DuckAgent bus monitor")
    parser.add_argument("--server-url", required=True, help="Bus server URL")
    args = parser.parse_args()

    monitor = BusMonitor(server_url=args.server_url)
    try:
        monitor.run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
