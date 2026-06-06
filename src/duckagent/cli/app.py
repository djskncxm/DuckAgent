import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import typer
import structlog

from duckagent.bus import Message, LocalMessageBus
from duckagent.config import settings

logger = structlog.get_logger()
app = typer.Typer(name="duck", help="DuckAgent - Android 逆向 Multi-Agent 系统")


@asynccontextmanager
async def get_bus():
    bus = LocalMessageBus(db_path=settings.db_path)
    await bus.initialize()
    try:
        yield bus
    finally:
        await bus.close()


def format_message(msg: Message) -> str:
    ts = msg.timestamp.strftime("%H:%M") if isinstance(msg.timestamp, datetime) else str(msg.timestamp)[:5]
    target = msg.to_agent or "all"
    if target == "human":
        target = "you"
    return f"[{ts}] {msg.from_agent} → {target}: {msg.content}"


# ── Core commands ──────────────────────────────────────────────


@app.command()
def run(
    local: bool = typer.Option(False, "--local", "-l", help="单进程模式（所有 agent 在同一进程）"),
    connect: str | None = typer.Option(None, "--connect", help="仅 TUI，连接到已有 bus server URL"),
    port: int | None = typer.Option(None, "--port", help=f"Bus server 端口（默认 {settings.bus_server_port}）"),
    no_tmux: bool = typer.Option(False, "--no-tmux", help="不使用 tmux，回退到旧 Textual TUI"),
):
    """启动 DuckAgent（默认多进程模式：tmux + server + agents）"""
    if local:
        # 单进程本地模式
        from duckagent.cli.tui.app import DuckApp
        duck_app = DuckApp()
        duck_app.run()
    elif connect:
        # TUI 仅连接模式：连接到已有 bus server
        from duckagent.bus.http_client import HttpMessageBus
        from duckagent.cli.tui.app import DuckApp
        bus = HttpMessageBus(server_url=connect, agent_id=None)
        duck_app = DuckApp(bus=bus, http_mode=True)
        duck_app.run()
    else:
        # 默认：多进程模式，启动 server + agents + tmux（或旧 TUI）
        from duckagent.launcher import Launcher
        actual_port = port or settings.bus_server_port
        launcher = Launcher(server_port=actual_port, use_tmux=not no_tmux)
        launcher.start()


@app.command()
def log(
    from_agent: str = typer.Option(None, "--from", help="按发送者过滤"),
    limit: int = typer.Option(50, "--limit", help="消息数量限制"),
    msg_type: str = typer.Option(None, "--type", help="按消息类型过滤"),
):
    """查看消息历史"""
    asyncio.run(_show_log(from_agent, limit, msg_type))


@app.command()
def send(message: str):
    """发送消息给主 Agent（非交互模式）"""
    asyncio.run(_send_message(message))


# ── HTTP / multi-process commands ──────────────────────────────


@app.command()
def server(
    port: int | None = typer.Option(None, "--port", help=f"Bus server 端口（默认 {settings.bus_server_port}）"),
):
    """启动 HTTP 消息总线服务"""
    import uvicorn
    from duckagent.server.app import app as bus_app
    actual_port = port or settings.bus_server_port
    uvicorn.run(bus_app, host="127.0.0.1", port=actual_port, log_level="info")


@app.command()
def agent(
    agent_type: str = typer.Argument(..., help="Agent type: main_agent | trace_agent | ida_jadx_agent"),
    server_url: str | None = typer.Option(None, "--server-url", help=f"Bus server URL（默认 {settings.bus_server_url}）"),
):
    """启动单个 Agent 进程"""
    from duckagent.processes.agent_process import run_agent
    actual_url = server_url or settings.bus_server_url
    asyncio.run(run_agent(agent_type, actual_url))


# ── Internals ──────────────────────────────────────────────────


async def _show_log(from_agent: str | None, limit: int, msg_type: str | None):
    async with get_bus() as bus:
        history = await bus.get_history(
            limit=limit, from_agent=from_agent, msg_type=msg_type
        )
        if not history:
            typer.echo("没有消息")
            return
        for msg in history:
            typer.echo(format_message(msg))


async def _send_message(content: str):
    async with get_bus() as bus:
        msg = Message(
            from_agent="human",
            to_agent="main_agent",
            type="request",
            content=content,
            evidence=[],
            confidence="high",
        )
        await bus.publish(msg)
        typer.echo(f"已发送: {content}")
