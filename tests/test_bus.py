import pytest
from datetime import datetime, timezone
from duckagent.bus.models import Message


def test_message_creation():
    msg = Message(
        from_agent="trace_agent",
        to_agent="main_agent",
        type="conclusion",
        content="Identified AES-128-CBC at 0x7a3c00",
        evidence=["line 42: aese v0.16b, v1.16b", "line 43: aesmc v0.16b, v0.16b"],
        confidence="high",
    )
    assert msg.id is not None
    assert msg.timestamp is not None
    assert msg.reply_to is None


def test_message_broadcast():
    msg = Message(
        from_agent="trace_agent",
        to_agent=None,
        type="conclusion",
        content="Found loop structure",
        evidence=["line 10-25: branch back to 0x7a3c00"],
        confidence="medium",
    )
    assert msg.to_agent is None


def test_message_status_type():
    msg = Message(
        from_agent="main_agent",
        to_agent=None,
        type="status",
        content='{"state": "thinking", "task_summary": "分析请求"}',
        evidence=[],
        confidence="high",
    )
    assert msg.type == "status"


def test_message_invalid_type():
    with pytest.raises(ValueError):
        Message(
            from_agent="trace_agent",
            to_agent=None,
            type="invalid_type",
            content="test",
            evidence=[],
            confidence="high",
        )


def test_message_invalid_confidence():
    with pytest.raises(ValueError):
        Message(
            from_agent="trace_agent",
            to_agent=None,
            type="conclusion",
            content="test",
            evidence=[],
            confidence="maybe",
        )


# --- MessageBus tests ---

import asyncio
from pathlib import Path
from duckagent.bus import LocalMessageBus


@pytest.fixture
async def bus(tmp_path):
    b = LocalMessageBus(db_path=tmp_path / "test.db")
    await b.initialize()
    yield b
    await b.close()


@pytest.mark.asyncio
async def test_publish_and_subscribe(bus):
    queue = bus.subscribe("main_agent")

    msg = Message(
        from_agent="trace_agent",
        to_agent="main_agent",
        type="conclusion",
        content="Found AES",
        evidence=["line 42"],
        confidence="high",
    )
    await bus.publish(msg)

    received = await asyncio.wait_for(queue.get(), timeout=1.0)
    assert received.id == msg.id
    assert received.content == "Found AES"


@pytest.mark.asyncio
async def test_broadcast_excludes_sender(bus):
    sender_queue = bus.subscribe("trace_agent")
    receiver_queue = bus.subscribe("main_agent")

    msg = Message(
        from_agent="trace_agent",
        to_agent=None,
        type="conclusion",
        content="Broadcast message",
        evidence=["line 1"],
        confidence="high",
    )
    await bus.publish(msg)

    received = await asyncio.wait_for(receiver_queue.get(), timeout=1.0)
    assert received.content == "Broadcast message"

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(sender_queue.get(), timeout=0.1)


@pytest.mark.asyncio
async def test_private_message_only_to_target(bus):
    target_queue = bus.subscribe("trace_agent")
    other_queue = bus.subscribe("other_agent")

    msg = Message(
        from_agent="main_agent",
        to_agent="trace_agent",
        type="request",
        content="Analyze this",
        evidence=[],
        confidence="high",
    )
    await bus.publish(msg)

    received = await asyncio.wait_for(target_queue.get(), timeout=1.0)
    assert received.content == "Analyze this"

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(other_queue.get(), timeout=0.1)


@pytest.mark.asyncio
async def test_get_history(bus):
    for i in range(3):
        msg = Message(
            from_agent="trace_agent",
            to_agent=None,
            type="conclusion",
            content=f"Message {i}",
            evidence=[f"line {i}"],
            confidence="high",
        )
        await bus.publish(msg)

    history = await bus.get_history(limit=10)
    assert len(history) == 3
    assert history[0].content == "Message 0"


@pytest.mark.asyncio
async def test_get_history_filter_by_agent(bus):
    await bus.publish(Message(
        from_agent="trace_agent", to_agent=None,
        type="conclusion", content="from trace",
        evidence=["x"], confidence="high",
    ))
    await bus.publish(Message(
        from_agent="main_agent", to_agent=None,
        type="decision", content="from main",
        evidence=[], confidence="high",
    ))

    history = await bus.get_history(from_agent="trace_agent")
    assert len(history) == 1
    assert history[0].content == "from trace"


@pytest.mark.asyncio
async def test_status_message_not_persisted(tmp_path):
    bus = LocalMessageBus(db_path=tmp_path / "test.db")
    await bus.initialize()

    msg = Message(
        from_agent="main_agent",
        to_agent=None,
        type="status",
        content='{"state": "thinking"}',
        evidence=[],
        confidence="high",
    )
    await bus.publish(msg)

    history = await bus.get_history(limit=100)
    assert len(history) == 0

    await bus.close()


@pytest.mark.asyncio
async def test_observer_receives_all_messages(bus):
    """Observer receives copies of all messages regardless of routing."""
    obs = bus.add_observer()
    target_queue = bus.subscribe("trace_agent")

    # Private message
    private_msg = Message(
        from_agent="main_agent",
        to_agent="trace_agent",
        type="request",
        content="private",
        evidence=[],
        confidence="high",
    )
    await bus.publish(private_msg)

    # Target gets it
    private_received = await asyncio.wait_for(target_queue.get(), timeout=1.0)
    assert private_received.content == "private"

    # Observer also gets a copy
    obs_received = await asyncio.wait_for(obs.get(), timeout=1.0)
    assert obs_received.content == "private"

    # Broadcast message
    broadcast_msg = Message(
        from_agent="trace_agent",
        to_agent=None,
        type="conclusion",
        content="broadcast",
        evidence=["line 1"],
        confidence="high",
    )
    await bus.publish(broadcast_msg)

    obs_received2 = await asyncio.wait_for(obs.get(), timeout=1.0)
    assert obs_received2.content == "broadcast"

    bus.remove_observer(obs)


@pytest.mark.asyncio
async def test_persistence_across_instances(tmp_path):
    db_path = tmp_path / "persist.db"

    bus1 = LocalMessageBus(db_path=db_path)
    await bus1.initialize()
    await bus1.publish(Message(
        from_agent="trace_agent", to_agent=None,
        type="conclusion", content="persisted",
        evidence=["line 1"], confidence="high",
    ))
    await bus1.close()

    bus2 = LocalMessageBus(db_path=db_path)
    await bus2.initialize()
    history = await bus2.get_history()
    assert len(history) == 1
    assert history[0].content == "persisted"
    await bus2.close()


# --- Mention-related tests ---


def test_message_mentions_default_empty():
    """Message.mentions should default to empty list."""
    msg = Message(
        from_agent="trace_agent",
        to_agent=None,
        type="conclusion",
        content="test",
        evidence=[],
        confidence="high",
    )
    assert msg.mentions == []


def test_message_with_mentions():
    """Message should accept and store mentions."""
    msg = Message(
        from_agent="main_agent",
        to_agent=None,
        mentions=["trace_agent", "ida_jadx_agent"],
        type="request",
        content="@trace_agent @ida_jadx_agent 分析这个",
        evidence=[],
        confidence="high",
    )
    assert msg.mentions == ["trace_agent", "ida_jadx_agent"]


@pytest.mark.asyncio
async def test_dispatch_by_mentions(bus):
    """Publishing with mentions routes to mentioned agents."""
    target_queue = bus.subscribe("trace_agent")
    other_queue = bus.subscribe("other_agent")

    msg = Message(
        from_agent="main_agent",
        to_agent=None,
        mentions=["trace_agent"],
        type="request",
        content="@trace_agent 分析",
        evidence=[],
        confidence="high",
    )
    await bus.publish(msg)

    # trace_agent receives it
    received = await asyncio.wait_for(target_queue.get(), timeout=1.0)
    assert received.content == "@trace_agent 分析"

    # other_agent does NOT receive it
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(other_queue.get(), timeout=0.1)


@pytest.mark.asyncio
async def test_dispatch_mentions_excludes_sender(bus):
    """Sender should never receive their own message even if they mention themselves."""
    queue = bus.subscribe("trace_agent")

    msg = Message(
        from_agent="trace_agent",
        to_agent=None,
        mentions=["trace_agent"],
        type="conclusion",
        content="self mention",
        evidence=[],
        confidence="high",
    )
    await bus.publish(msg)

    # trace_agent should NOT receive it (sender exclusion)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(queue.get(), timeout=0.1)


@pytest.mark.asyncio
async def test_mentions_and_to_agent_combined(bus):
    """When both to_agent and mentions are set, both recipients get it."""
    trace_queue = bus.subscribe("trace_agent")
    jadx_queue = bus.subscribe("ida_jadx_agent")
    other_queue = bus.subscribe("other_agent")

    msg = Message(
        from_agent="main_agent",
        to_agent="trace_agent",
        mentions=["ida_jadx_agent"],
        type="request",
        content="collaborate",
        evidence=[],
        confidence="high",
    )
    await bus.publish(msg)

    # trace_agent receives via to_agent
    received_trace = await asyncio.wait_for(trace_queue.get(), timeout=1.0)
    assert received_trace.content == "collaborate"

    # ida_jadx_agent receives via mentions
    received_jadx = await asyncio.wait_for(jadx_queue.get(), timeout=1.0)
    assert received_jadx.content == "collaborate"

    # other_agent does NOT receive
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(other_queue.get(), timeout=0.1)


@pytest.mark.asyncio
async def test_mentions_persisted_and_restored(tmp_path):
    """Mentions should survive persistence across MessageBus instances."""
    db_path = tmp_path / "mentions_persist.db"

    bus1 = LocalMessageBus(db_path=db_path)
    await bus1.initialize()
    await bus1.publish(Message(
        from_agent="main_agent",
        to_agent=None,
        mentions=["trace_agent", "ida_jadx_agent"],
        type="request",
        content="tagged message",
        evidence=[],
        confidence="high",
    ))
    await bus1.close()

    bus2 = LocalMessageBus(db_path=db_path)
    await bus2.initialize()
    history = await bus2.get_history()
    assert len(history) == 1
    assert history[0].content == "tagged message"
    assert history[0].mentions == ["trace_agent", "ida_jadx_agent"]
    await bus2.close()
