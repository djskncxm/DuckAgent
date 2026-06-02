import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from duckagent.bus import Message, LocalMessageBus
from duckagent.agents.main_agent import MainAgent
from duckagent.agents.trace_agent import TraceAgent


@pytest.fixture
async def system(tmp_path):
    db_path = tmp_path / "test.db"
    bus = LocalMessageBus(db_path=db_path)
    await bus.initialize()

    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "main_agent.md").write_text("你是主协调 Agent。")
    (prompts_dir / "trace_agent.md").write_text("你是 Trace 分析 Agent。")

    agent_md = tmp_path / "AGENT.md"
    agent_md.write_text("# Test\n\n逆向测试 APP 签名算法")

    yield {
        "bus": bus,
        "prompts_dir": prompts_dir,
        "agent_md": agent_md,
    }

    await bus.close()


@pytest.mark.asyncio
async def test_full_flow_human_to_trace_and_back(system):
    """Integration test: human → main_agent @trace_agent → trace_agent → main_agent → human.

    Verifies the complete @mention-based peer-to-peer message pipeline with mocked LLM.
    """
    bus = system["bus"]

    # Response 1: main_agent receives human request, @mentions trace_agent
    main_response = (
        "@trace_agent 分析以下 trace 中的加密算法:\n"
        "line 3: aese v0.16b, v1.16b\n"
        "line 4: aesmc v0.16b, v0.16b"
    )
    # Response 2: trace_agent analyzes and responds
    trace_response = (
        "根据 trace 分析，line 3 的 aese 指令和 line 4 的 aesmc 指令表明这是 AES 加密。"
        "具体来说是 AES-128，因为只有两轮 aese+aesmc 组合。"
    )
    # Response 3: main_agent receives trace conclusion, responds to human (no @mentions)
    main_summary = (
        "Trace 分析结果：检测到 AES-128 加密算法。\n\n"
        "证据：\n- line 3: aese 指令\n- line 4: aesmc 指令\n\n"
        "这是典型的 AES-128 加密实现。"
    )

    call_count = {"n": 0}
    responses = [main_response, trace_response, main_summary]

    async def mock_completion(*args, **kwargs):
        idx = min(call_count["n"], len(responses) - 1)
        call_count["n"] += 1
        mock = AsyncMock()
        mock.choices = [AsyncMock(message=AsyncMock(content=responses[idx]))]
        return mock

    with patch("duckagent.agents.base.litellm.acompletion", side_effect=mock_completion):
        main_agent = MainAgent(
            bus=bus,
            model="test-model",
            agent_md_path=system["agent_md"],
            prompts_dir=system["prompts_dir"],
        )
        trace_agent = TraceAgent(
            bus=bus,
            model="test-model",
            prompts_dir=system["prompts_dir"],
        )

        await main_agent.start()
        await trace_agent.start()

        human_queue = bus.subscribe("human")

        await bus.publish(Message(
            from_agent="human",
            to_agent="main_agent",
            type="request",
            content="分析这段 trace 里的加密算法",
            evidence=[],
            confidence="high",
        ))

        # Skip status messages until we get the actual reply
        received = None
        for _ in range(10):
            msg = await asyncio.wait_for(human_queue.get(), timeout=5.0)
            if msg.type != "status":
                received = msg
                break
        assert received is not None, "Timed out waiting for non-status message"
        assert received.to_agent == "human"
        assert received.from_agent == "main_agent"
        assert "AES" in received.content

        # Verify all 3 LLM calls were made (main→trace→main)
        assert call_count["n"] == 3

        await main_agent.stop()
        await trace_agent.stop()


@pytest.mark.asyncio
async def test_main_agent_mention_parsing():
    """Test that @mention parsing correctly extracts agent IDs."""
    from duckagent.agents.base import BaseAgent

    # Test basic parsing
    mentions = BaseAgent._parse_mentions("@trace_agent 分析这个")
    assert mentions == ["trace_agent"]

    # Test multiple mentions
    mentions = BaseAgent._parse_mentions("@trace_agent @ida_jadx_agent 一起分析")
    assert mentions == ["trace_agent", "ida_jadx_agent"]

    # Test deduplication
    mentions = BaseAgent._parse_mentions("@trace_agent @trace_agent 分析")
    assert mentions == ["trace_agent"]

    # Test no mention
    mentions = BaseAgent._parse_mentions("直接分析这个 trace")
    assert mentions == []

    # Test @ in non-mention contexts (email-like patterns use dots, not matched)
    mentions = BaseAgent._parse_mentions("user@host 不是 mention")
    assert mentions == []
