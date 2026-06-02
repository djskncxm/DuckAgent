"""HttpMessageBus — HTTP + WebSocket client for the message bus server.

Implements the MessageBus ABC so agent code works unchanged.
Uses httpx for HTTP requests (publish, history) and websockets for
real-time message receipt.

Architecture:
- Agent mode (agent_id set): WebSocket connects as ?agent_id=<id>,
  server pushes only messages routed to this agent.
- Observer mode (agent_id=None): WebSocket connects as ?role=observer,
  server pushes ALL messages (TUI use).

subscribe() and add_observer() create asyncio.Queue objects synchronously
and a background _ws_reader() coroutine fans incoming messages to all queues.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import structlog
import websockets
import websockets.asyncio.client

from duckagent.config import settings

from .interface import MessageBus
from .models import Message

logger = structlog.get_logger()

# How long to wait for server health check on connect / reconnect
_CONNECT_TIMEOUT = 10.0


class ConnectionError(Exception):
    """Raised when the bus server is unreachable."""


class HttpMessageBus(MessageBus):
    """Message bus client that communicates with the FastAPI server.

    Parameters
    ----------
    server_url:
        Base URL of the bus server, e.g. "http://127.0.0.1:8720".
    agent_id:
        When set, the WebSocket connects in agent mode and only receives
        messages addressed to this agent. When None (default), connects
        in observer mode and receives ALL messages.
    """

    def __init__(
        self,
        server_url: str | None = None,
        agent_id: str | None = None,
    ) -> None:
        self._server_url = (server_url or settings.bus_server_url).rstrip("/")
        self._agent_id = agent_id
        self._http: httpx.AsyncClient | None = None
        self._ws: websockets.asyncio.client.ClientConnection | None = None
        self._ws_task: asyncio.Task[None] | None = None
        self._queues: list[asyncio.Queue[Message]] = []
        self._agent_queues: dict[str, asyncio.Queue[Message]] = {}
        self._connected = False

    # --- MessageBus interface ---

    async def initialize(self) -> None:
        """Connect to the bus server: create HTTP client, open WebSocket."""
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(30.0))
        await self._connect_ws()

    async def close(self) -> None:
        """Disconnect from the bus server and release all resources."""
        self._connected = False

        if self._ws_task:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass
            self._ws_task = None

        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

        if self._http:
            await self._http.aclose()
            self._http = None

        self._queues.clear()
        self._agent_queues.clear()

    def subscribe(self, agent_id: str) -> asyncio.Queue[Message]:
        """Create a queue that receives messages for the given agent.

        In agent mode the WebSocket already filters; the returned queue
        receives every message that arrives on the WebSocket.  In observer
        mode ALL messages are fanned out.
        """
        queue: asyncio.Queue[Message] = asyncio.Queue()
        self._queues.append(queue)
        self._agent_queues[agent_id] = queue
        return queue

    def unsubscribe(self, agent_id: str) -> None:
        """Remove the queue associated with this agent."""
        queue = self._agent_queues.pop(agent_id, None)
        if queue is not None:
            try:
                self._queues.remove(queue)
            except ValueError:
                pass

    def add_observer(self) -> asyncio.Queue[Message]:
        """Create a queue that receives every message (observer pattern).

        Multiple observers can coexist — each gets a copy.
        """
        queue: asyncio.Queue[Message] = asyncio.Queue()
        self._queues.append(queue)
        return queue

    def remove_observer(self, queue: asyncio.Queue[Message]) -> None:
        """Remove an observer queue."""
        try:
            self._queues.remove(queue)
        except ValueError:
            pass

    async def publish(self, msg: Message) -> None:
        """Publish a message via HTTP POST."""
        if not self._http:
            raise ConnectionError("HttpMessageBus not initialized")
        try:
            resp = await self._http.post(
                f"{self._server_url}/api/v1/publish",
                json=msg.model_dump(mode="json"),
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise ConnectionError(f"Failed to publish message: {exc}") from exc

    async def get_history(
        self,
        limit: int = 50,
        from_agent: str | None = None,
        msg_type: str | None = None,
    ) -> list[Message]:
        """Retrieve historical messages via HTTP GET."""
        if not self._http:
            raise ConnectionError("HttpMessageBus not initialized")
        params: dict[str, str] = {"limit": str(limit)}
        if from_agent:
            params["from"] = from_agent
        if msg_type:
            params["type"] = msg_type
        try:
            resp = await self._http.get(
                f"{self._server_url}/api/v1/history",
                params=params,
            )
            resp.raise_for_status()
            return [Message(**item) for item in resp.json()]
        except httpx.HTTPError as exc:
            raise ConnectionError(f"Failed to fetch history: {exc}") from exc

    # --- WebSocket lifecycle ---

    async def _connect_ws(self) -> None:
        """Open the WebSocket connection and start the reader task.

        Chooses the query param based on agent/observer mode.
        """
        ws_url = self._server_url.replace("http://", "ws://").replace("https://", "wss://")
        if self._agent_id:
            ws_url += f"/ws?agent_id={self._agent_id}"
        else:
            ws_url += "/ws?role=observer"

        self._ws = await websockets.asyncio.client.connect(
            ws_url, open_timeout=_CONNECT_TIMEOUT
        )
        self._connected = True
        self._ws_task = asyncio.create_task(self._ws_reader())
        logger.info(
            "http_bus_connected",
            server_url=self._server_url,
            agent_id=self._agent_id or "observer",
        )

    # --- WebSocket reader ---

    async def _ws_reader(self) -> None:
        """Background task: read messages from WebSocket, fan out to all queues.

        Runs until the WebSocket closes or is cancelled.
        """
        assert self._ws is not None
        try:
            async for raw in self._ws:
                try:
                    payload: dict[str, Any] = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning("ws_bad_json", raw=raw[:200])
                    continue

                if payload.get("type") != "message":
                    continue

                data = payload.get("data")
                if not isinstance(data, dict):
                    continue

                try:
                    msg = Message(**data)
                except Exception:
                    logger.warning("ws_bad_message", data=str(data)[:200])
                    continue

                # Fan out to every registered queue
                for q in self._queues:
                    q.put_nowait(msg)

        except websockets.ConnectionClosed:
            logger.info("ws_connection_closed", agent_id=self._agent_id or "observer")
        except asyncio.CancelledError:
            pass
        finally:
            self._connected = False
