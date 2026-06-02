"""Agent process entry point.

Each agent runs in its own OS process, communicating with the bus server
via HttpMessageBus (HTTP POST for publish, WebSocket for receive).

Usage:
    python -m duckagent.processes.agent_process main_agent --server-url http://127.0.0.1:8720
"""

from __future__ import annotations

import argparse
import asyncio
import signal
from pathlib import Path
from typing import Any

import structlog

from duckagent.bus.http_client import HttpMessageBus
from duckagent.config import settings

logger = structlog.get_logger()

_PROJECT_ROOT = Path(__file__).resolve().parents[3]


async def run_agent(agent_type: str, server_url: str) -> None:
    """Create and run a single agent, blocking until SIGTERM/SIGINT."""
    bus = HttpMessageBus(server_url=server_url, agent_id=agent_type)
    await bus.initialize()

    agent = _create_agent(agent_type, bus)
    await agent.start()

    logger.info("agent_process_ready", agent_type=agent_type)

    # Block until shutdown signal
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    await stop_event.wait()

    logger.info("agent_process_shutting_down", agent_type=agent_type)
    await agent.stop()
    await bus.close()


def _create_agent(agent_type: str, bus: HttpMessageBus) -> Any:
    """Factory: create the right Agent subclass for the given type."""
    prompts_dir = Path(settings.prompts_dir)
    agent_md_path = _PROJECT_ROOT / "AGENT.md"

    if agent_type == "main_agent":
        from duckagent.agents.main_agent import MainAgent

        return MainAgent(
            bus=bus,
            model=settings.litellm_model,
            agent_md_path=agent_md_path,
            prompts_dir=prompts_dir,
            verify_enabled=settings.verify_enabled,
            verify_max_retries=settings.verify_max_retries,
        )

    if agent_type == "trace_agent":
        from duckagent.agents.trace_agent import TraceAgent

        return TraceAgent(
            bus=bus,
            model=settings.litellm_model,
            prompts_dir=prompts_dir,
            verify_enabled=settings.verify_enabled,
            verify_max_retries=settings.verify_max_retries,
        )

    if agent_type == "ida_jadx_agent":
        from duckagent.agents.ida_jadx_agent import IdaJadxAgent

        return IdaJadxAgent(
            bus=bus,
            model=settings.litellm_model,
            prompts_dir=prompts_dir,
            verify_enabled=settings.verify_enabled,
            verify_max_retries=settings.verify_max_retries,
        )

    raise ValueError(f"Unknown agent type: {agent_type}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a DuckAgent agent process")
    parser.add_argument(
        "agent_type",
        choices=["main_agent", "trace_agent", "ida_jadx_agent"],
        help="Which agent to run",
    )
    parser.add_argument(
        "--server-url",
        required=True,
        help="Bus server URL, e.g. http://127.0.0.1:8720",
    )
    args = parser.parse_args()
    asyncio.run(run_agent(args.agent_type, args.server_url))


if __name__ == "__main__":
    main()
