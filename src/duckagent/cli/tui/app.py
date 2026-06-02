import asyncio
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widgets import Footer, Header

from duckagent.agents.main_agent import MainAgent
from duckagent.agents.trace_agent import TraceAgent
from duckagent.agents.ida_jadx_agent import IdaJadxAgent
from duckagent.bus.interface import MessageBus
from duckagent.bus.store import LocalMessageBus
from duckagent.bus.models import Message
from duckagent.config import settings
from duckagent.cli.tui.widgets.agent_card import AgentCard
from duckagent.cli.tui.widgets.input_area import InputArea
from duckagent.cli.tui.widgets.message import MessageWidget
from duckagent.cli.tui.worker import consume_status_queue, consume_observer_queue

import structlog
logger = structlog.get_logger()

_PROJECT_ROOT = Path(__file__).resolve().parents[4]


class DuckApp(App):
    CSS_PATH = str(Path(__file__).parent / "app.tcss")
    TITLE = "DuckAgent"
    BINDINGS = [
        Binding("ctrl+l", "clear_messages", "清屏"),
        Binding("ctrl+d", "quit", "退出", priority=True),
    ]

    def __init__(self, bus: MessageBus | None = None, http_mode: bool = False) -> None:
        super().__init__()
        self._external_bus = bus
        self._http_mode = http_mode
        self._bus: MessageBus | None = None
        self._main_agent: MainAgent | None = None
        self._trace_agent: TraceAgent | None = None
        self._ida_jadx_agent: IdaJadxAgent | None = None
        self._status_task: asyncio.Task[None] | None = None
        self._observer_task: asyncio.Task[None] | None = None
        self._observer_queue: asyncio.Queue[Message] | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield VerticalScroll(id="messages")
        yield VerticalScroll(id="agents")
        yield InputArea()
        yield Footer()

    async def on_mount(self) -> None:
        agents_panel = self.query_one("#agents", VerticalScroll)
        await agents_panel.mount(AgentCard(agent_id="main_agent"))
        await agents_panel.mount(AgentCard(agent_id="trace_agent"))
        await agents_panel.mount(AgentCard(agent_id="ida_jadx_agent"))

        if self._http_mode and self._external_bus:
            # HTTP mode: use the provided bus (agents run in separate processes)
            self._bus = self._external_bus
            await self._bus.initialize()
        else:
            # Local mode: create bus, agents, and start everything in-process
            self._bus = LocalMessageBus(db_path=settings.db_path)
            await self._bus.initialize()

            prompts_dir = Path(settings.prompts_dir)
            agent_md_path = _PROJECT_ROOT / "AGENT.md"

            self._main_agent = MainAgent(
                bus=self._bus,
                model=settings.litellm_model,
                agent_md_path=agent_md_path,
                prompts_dir=prompts_dir,
            )
            self._trace_agent = TraceAgent(
                bus=self._bus,
                model=settings.litellm_model,
                prompts_dir=prompts_dir,
            )
            self._ida_jadx_agent = IdaJadxAgent(
                bus=self._bus,
                model=settings.litellm_model,
                prompts_dir=prompts_dir,
            )

            await self._main_agent.start()
            await self._trace_agent.start()
            await self._ida_jadx_agent.start()

        # Observer sees ALL messages (agent↔agent + agent→human)
        self._observer_queue = self._bus.add_observer()
        status_queue = self._bus.subscribe("_tui")

        self._observer_task = asyncio.create_task(consume_observer_queue(self, self._observer_queue))
        self._status_task = asyncio.create_task(consume_status_queue(self, status_queue))

        # Load recent message history in background so UI appears immediately
        asyncio.create_task(self._load_history())

    async def _load_history(self) -> None:
        """Load recent messages from DB after UI is visible."""
        assert self._bus is not None
        try:
            history = await self._bus.get_history(limit=20)
        except Exception:
            return
        container = self.query_one("#messages", VerticalScroll)
        for msg in history:
            if msg.type == "status":
                continue
            container.mount(MessageWidget(msg))
        if history:
            container.scroll_end(animate=False)

    async def on_unmount(self) -> None:
        if self._status_task:
            self._status_task.cancel()
        if self._observer_task:
            self._observer_task.cancel()
        if self._observer_queue and self._bus:
            self._bus.remove_observer(self._observer_queue)
        if self._main_agent:
            await self._main_agent.stop()
        if self._trace_agent:
            await self._trace_agent.stop()
        if self._ida_jadx_agent:
            await self._ida_jadx_agent.stop()
        if self._bus:
            await self._bus.close()

    def on_input_area_submitted(self, event: InputArea.Submitted) -> None:
        from duckagent.bus.models import parse_mentions
        mentions = parse_mentions(event.value)

        msg = Message(
            from_agent="human",
            to_agent="main_agent",
            mentions=mentions,
            type="request",
            content=event.value,
            evidence=[],
            confidence="high",
        )
        container = self.query_one("#messages", VerticalScroll)
        container.mount(MessageWidget(msg))
        container.scroll_end(animate=False)

        async def _publish_safe(msg: Message) -> None:
            try:
                assert self._bus is not None
                await self._bus.publish(msg)
            except Exception as e:
                logger.error("publish_failed", error=str(e))

        asyncio.create_task(_publish_safe(msg))

    def action_clear_messages(self) -> None:
        container = self.query_one("#messages", VerticalScroll)
        container.remove_children()
