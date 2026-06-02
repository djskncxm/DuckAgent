import re
from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field

AT_MENTION_RE = re.compile(r"(?<!\w)@(\w[\w_-]*\w)")


def parse_mentions(text: str) -> list[str]:
    """Extract unique @agent_id mentions in order of first appearance."""
    return list(dict.fromkeys(AT_MENTION_RE.findall(text)))


class Message(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    from_agent: str
    to_agent: str | None = None
    mentions: list[str] = Field(default_factory=list)
    type: Literal["conclusion", "request", "question", "decision", "status"]
    content: str
    evidence: list[str] = Field(default_factory=list)
    confidence: Literal["high", "medium", "low"] = "high"
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    reply_to: str | None = None
