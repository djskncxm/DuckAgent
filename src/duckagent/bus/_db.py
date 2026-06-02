"""Shared SQLite persistence for the message bus.

Contains the schema, migration logic, and row-to-message mapping used
by both LocalMessageBus (in-process) and the FastAPI server Database.
"""

import json
from pathlib import Path

import aiosqlite

from .models import Message

CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    from_agent TEXT NOT NULL,
    to_agent TEXT,
    mentions TEXT NOT NULL DEFAULT '[]',
    type TEXT NOT NULL,
    content TEXT NOT NULL,
    evidence TEXT NOT NULL,
    confidence TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    reply_to TEXT
)
"""


async def init_db(db: aiosqlite.Connection) -> None:
    """Create schema and run migrations."""
    await db.execute(CREATE_TABLE)
    try:
        await db.execute("SELECT mentions FROM messages LIMIT 1")
    except aiosqlite.OperationalError:
        await db.execute(
            "ALTER TABLE messages ADD COLUMN mentions TEXT NOT NULL DEFAULT '[]'"
        )
    await db.commit()


async def insert_message(db: aiosqlite.Connection, msg: Message) -> None:
    await db.execute(
        "INSERT INTO messages (id, from_agent, to_agent, mentions, type, "
        "content, evidence, confidence, timestamp, reply_to) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            msg.id,
            msg.from_agent,
            msg.to_agent,
            json.dumps(msg.mentions),
            msg.type,
            msg.content,
            json.dumps(msg.evidence),
            msg.confidence,
            msg.timestamp.isoformat(),
            msg.reply_to,
        ),
    )
    await db.commit()


async def get_history(
    db: aiosqlite.Connection,
    limit: int = 50,
    from_agent: str | None = None,
    msg_type: str | None = None,
) -> list[Message]:
    query = (
        "SELECT id, from_agent, to_agent, mentions, type, content, "
        "evidence, confidence, timestamp, reply_to "
        "FROM messages WHERE 1=1"
    )
    params: list[str] = []

    if from_agent:
        query += " AND from_agent = ?"
        params.append(from_agent)
    if msg_type:
        query += " AND type = ?"
        params.append(msg_type)

    query += " ORDER BY timestamp ASC LIMIT ?"
    params.append(str(limit))

    async with db.execute(query, params) as cursor:
        rows = await cursor.fetchall()

    return [row_to_message(row) for row in rows]


def row_to_message(row: tuple) -> Message:
    return Message(
        id=row[0],
        from_agent=row[1],
        to_agent=row[2],
        mentions=json.loads(row[3]),
        type=row[4],
        content=row[5],
        evidence=json.loads(row[6]),
        confidence=row[7],
        timestamp=row[8],
        reply_to=row[9],
    )
