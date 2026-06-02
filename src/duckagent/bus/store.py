import asyncio
from pathlib import Path

import aiosqlite

from . import _db
from .interface import MessageBus
from .models import Message


class LocalMessageBus(MessageBus):
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None
        self._subscribers: dict[str, asyncio.Queue[Message]] = {}
        self._observers: list[asyncio.Queue[Message]] = []

    async def initialize(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._db_path)
        await _db.init_db(self._db)

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None
        self._subscribers.clear()
        self._observers.clear()

    def subscribe(self, agent_id: str) -> asyncio.Queue[Message]:
        queue: asyncio.Queue[Message] = asyncio.Queue()
        self._subscribers[agent_id] = queue
        return queue

    def unsubscribe(self, agent_id: str) -> None:
        self._subscribers.pop(agent_id, None)

    def add_observer(self) -> asyncio.Queue[Message]:
        queue: asyncio.Queue[Message] = asyncio.Queue()
        self._observers.append(queue)
        return queue

    def remove_observer(self, queue: asyncio.Queue[Message]) -> None:
        try:
            self._observers.remove(queue)
        except ValueError:
            pass

    async def publish(self, msg: Message) -> None:
        if msg.type != "status":
            assert self._db is not None
            await _db.insert_message(self._db, msg)
        self._dispatch(msg)

    async def get_history(
        self,
        limit: int = 50,
        from_agent: str | None = None,
        msg_type: str | None = None,
    ) -> list[Message]:
        assert self._db is not None
        return await _db.get_history(self._db, limit=limit, from_agent=from_agent, msg_type=msg_type)

    def _dispatch(self, msg: Message) -> None:
        recipients: set[str] = set()

        if msg.to_agent:
            recipients.add(msg.to_agent)

        for agent_id in msg.mentions:
            recipients.add(agent_id)

        if not recipients:
            for agent_id in self._subscribers:
                if agent_id != msg.from_agent:
                    recipients.add(agent_id)

        recipients.discard(msg.from_agent)

        for agent_id in recipients:
            queue = self._subscribers.get(agent_id)
            if queue:
                queue.put_nowait(msg)

        for obs in self._observers:
            obs.put_nowait(msg)
