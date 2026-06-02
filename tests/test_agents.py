import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from duckagent.agents.base import BaseAgent
from duckagent.bus import Message, LocalMessageBus


class EchoAgent(BaseAgent):
    """Test agent that echoes back messages."""

    async def on_message(self, msg: Message) -> None:
        response = await self.think(msg.content)
        await self.send(
            to=msg.from_agent,
            content=response,
            type="conclusion",
            evidence=["echo test"],
        )


@pytest.fixture
async def bus(tmp_path):
    b = LocalMessageBus(db_path=tmp_path / "test.db")
    await b.initialize()
    yield b
    await b.close()


@pytest.mark.asyncio
async def test_agent_receives_message(bus):
    with patch("duckagent.agents.base.litellm.acompletion") as mock_llm:
        mock_llm.return_value = AsyncMock(
            choices=[AsyncMock(message=AsyncMock(content="echoed back"))]
        )

        agent = EchoAgent(
            agent_id="echo_agent",
            system_prompt="You are an echo agent.",
            bus=bus,
            model="test-model",
        )
        await agent.start()

        human_queue = bus.subscribe("human")

        await bus.publish(Message(
            from_agent="human",
            to_agent="echo_agent",
            type="request",
            content="hello",
            evidence=[],
            confidence="high",
        ))

        # Drain any status broadcasts before checking real response
        while True:
            received = await asyncio.wait_for(human_queue.get(), timeout=2.0)
            if received.type != "status":
                break

        assert received.content == "echoed back"
        assert received.from_agent == "echo_agent"

        await agent.stop()


@pytest.mark.asyncio
async def test_agent_context_is_per_call(bus):
    """Context is built fresh each think() call, not accumulated across messages."""
    with patch("duckagent.agents.base.litellm.acompletion") as mock_llm:
        mock_llm.return_value = AsyncMock(
            choices=[AsyncMock(message=AsyncMock(content="response"))]
        )

        agent = EchoAgent(
            agent_id="echo_agent",
            system_prompt="You are an echo agent.",
            bus=bus,
            model="test-model",
        )
        await agent.start()

        await bus.publish(Message(
            from_agent="human",
            to_agent="echo_agent",
            type="request",
            content="first",
            evidence=[],
            confidence="high",
        ))
        await asyncio.sleep(0.2)

        # Context is NOT a persistent attribute — each think() builds its own
        assert not hasattr(agent, "context")

        await agent.stop()


# --- MainAgent tests ---

from duckagent.agents.main_agent import MainAgent


@pytest.fixture
def agent_md(tmp_path):
    md = tmp_path / "AGENT.md"
    md.write_text("# Test Project\n\nReverse engineering test app signature.")
    return md


@pytest.mark.asyncio
async def test_main_agent_loads_agent_md(bus, agent_md):
    with patch("duckagent.agents.base.litellm.acompletion") as mock_llm:
        mock_llm.return_value = AsyncMock(
            choices=[AsyncMock(message=AsyncMock(content="I'll analyze this for you."))]
        )

        agent = MainAgent(
            bus=bus,
            model="test-model",
            agent_md_path=agent_md,
            prompts_dir=agent_md.parent,
        )
        await agent.start()

        assert "Reverse engineering test app" in agent.system_prompt
        await agent.stop()


@pytest.mark.asyncio
async def test_main_agent_responds_to_human(bus, agent_md):
    with patch("duckagent.agents.base.litellm.acompletion") as mock_llm:
        mock_llm.return_value = AsyncMock(
            choices=[AsyncMock(message=AsyncMock(
                content='{"action": "respond", "to": "human", "content": "Got it, analyzing now.", "type": "conclusion", "evidence": ["user request"], "confidence": "high"}'
            ))]
        )

        agent = MainAgent(
            bus=bus,
            model="test-model",
            agent_md_path=agent_md,
            prompts_dir=agent_md.parent,
        )
        await agent.start()

        human_queue = bus.subscribe("human")

        await bus.publish(Message(
            from_agent="human",
            to_agent="main_agent",
            type="request",
            content="分析一下这个 trace",
            evidence=[],
            confidence="high",
        ))

        received = await asyncio.wait_for(human_queue.get(), timeout=2.0)
        assert received.from_agent == "main_agent"
        await agent.stop()


# --- TraceAgent tests ---

from duckagent.agents.trace_agent import TraceAgent


@pytest.mark.asyncio
async def test_trace_agent_analyzes_request(bus, tmp_path):
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()

    with patch("duckagent.agents.base.litellm.acompletion") as mock_llm:
        mock_llm.return_value = AsyncMock(
            choices=[AsyncMock(message=AsyncMock(
                content="Identified AES-128-CBC. The aese instruction at line 42 confirms AES encryption."
            ))]
        )

        agent = TraceAgent(
            bus=bus,
            model="test-model",
            prompts_dir=prompts_dir,
        )
        await agent.start()

        main_queue = bus.subscribe("main_agent")

        await bus.publish(Message(
            from_agent="main_agent",
            to_agent="trace_agent",
            type="request",
            content="分析以下 trace 片段:\nline 42: 0x7a3c00 | aese v0.16b, v1.16b | v0=00112233...",
            evidence=[],
            confidence="high",
        ))

        # Drain any status broadcasts before checking real response
        while True:
            received = await asyncio.wait_for(main_queue.get(), timeout=2.0)
            if received.type != "status":
                break

        assert received.from_agent == "trace_agent"
        assert received.type == "conclusion"
        assert "AES" in received.content
        await agent.stop()


@pytest.mark.asyncio
async def test_agent_broadcasts_thinking_status(tmp_path):
    bus = LocalMessageBus(db_path=tmp_path / "test.db")
    await bus.initialize()

    status_queue = bus.subscribe("_tui")

    agent = BaseAgent(
        agent_id="test_agent",
        system_prompt="test",
        bus=bus,
        model="fake/model",
    )

    with patch("litellm.acompletion") as mock_llm:
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "hello"
        mock_response.choices[0].message.tool_calls = None
        mock_llm.return_value = mock_response

        await agent.think("test input")

    messages = []
    while not status_queue.empty():
        messages.append(await status_queue.get())

    status_msgs = [m for m in messages if m.type == "status"]
    assert len(status_msgs) >= 2
    assert '"state": "thinking"' in status_msgs[0].content
    assert '"state": "idle"' in status_msgs[-1].content

    await bus.close()
