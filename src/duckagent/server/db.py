"""SQLite persistence layer for the message bus server.

Thin wrapper around the shared bus._db module, providing an
object-oriented interface for the FastAPI lifespan.
"""

from pathlib import Path

import aiosqlite

from duckagent.bus import _db
from duckagent.bus.models import Message


class Database:
    """Thin wrapper around aiosqlite for message persistence."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._db_path)
        await _db.init_db(self._db)

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    async def insert_message(self, msg: Message) -> None:
        assert self._db is not None
        await _db.insert_message(self._db, msg)

    async def get_history(
        self,
        limit: int = 50,
        from_agent: str | None = None,
        msg_type: str | None = None,
    ) -> list[Message]:
        assert self._db is not None
        return await _db.get_history(self._db, limit=limit, from_agent=from_agent, msg_type=msg_type)
