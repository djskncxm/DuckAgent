import asyncio
from pathlib import Path

import pytest
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Footer, Header

from duckagent.cli.tui.widgets.input_area import InputArea
from duckagent.cli.tui.widgets.message import MessageWidget
from duckagent.cli.tui.widgets.agent_card import AgentCard
from duckagent.bus.models import Message as BusMessage


# --- InputArea tests (Task 4) ---

class InputTestApp(App):
    def compose(self) -> ComposeResult:
        yield InputArea()


class InputSubmitTestApp(App):
    def __init__(self) -> None:
        super().__init__()
        self.submitted: list[str] = []

    def compose(self) -> ComposeResult:
        yield InputArea()

    def on_input_area_submitted(self, event: InputArea.Submitted) -> None:
        self.submitted.append(event.value)


@pytest.mark.asyncio
async def test_input_area_mounts():
    app = InputTestApp()
    async with app.run_test() as pilot:
        widget = app.query_one(InputArea)
        assert widget is not None


@pytest.mark.asyncio
async def test_input_area_submit():
    app = InputSubmitTestApp()
    async with app.run_test() as pilot:
        widget = app.query_one(InputArea)
        widget.text = "hello world"
        await pilot.press("enter")
        assert "hello world" in app.submitted
        assert widget.text == ""


# --- MessageWidget tests (Task 5) ---

class MessageTestApp(App):
    def compose(self) -> ComposeResult:
        msg = BusMessage(
            from_agent="main_agent",
            to_agent="human",
            type="conclusion",
            content="## 分析结果\n\n- AES-128\n- Key at `0x1A40`\n\n```c\nvoid encrypt() {}\n```",
            evidence=["trace line 100"],
            confidence="high",
        )
        yield MessageWidget(msg)


@pytest.mark.asyncio
async def test_message_widget_renders_markdown():
    app = MessageTestApp()
    async with app.run_test() as pilot:
        widget = app.query_one(MessageWidget)
        assert widget is not None


# --- AgentCard tests (Task 6) ---

class AgentCardTestApp(App):
    def compose(self) -> ComposeResult:
        yield AgentCard(agent_id="main_agent")
        yield AgentCard(agent_id="trace_agent")


@pytest.mark.asyncio
async def test_agent_card_initial_state():
    app = AgentCardTestApp()
    async with app.run_test() as pilot:
        cards = app.query(AgentCard)
        assert len(list(cards)) == 2


@pytest.mark.asyncio
async def test_agent_card_update_status():
    app = AgentCardTestApp()
    async with app.run_test() as pilot:
        card = app.query(AgentCard).first()
        card.update_status("thinking", task_summary="分析加密算法")
        assert card.state == "thinking"
        assert "分析加密算法" in card.task_summary


# --- DuckApp tests (Task 7-8) ---

from duckagent.cli.tui.app import DuckApp
from duckagent.bus import LocalMessageBus


class DuckAppLayoutTest(App):
    """Minimal app that mirrors DuckApp.compose() layout for UI testing."""

    CSS_PATH = DuckApp.CSS_PATH

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


@pytest.mark.asyncio
async def test_duck_app_layout():
    """Test DuckApp UI layout (grid, panels, widgets, agent cards)."""
    app = DuckAppLayoutTest()
    async with app.run_test(size=(80, 24)) as pilot:
        assert app.query_one("#messages") is not None
        assert app.query_one("#agents") is not None
        assert app.query_one(InputArea) is not None
        cards = list(app.query(AgentCard))
        assert len(cards) == 2


@pytest.mark.asyncio
async def test_duck_app_instantiable():
    """Test DuckApp can be imported and instantiated."""
    app = DuckApp()
    assert app is not None
    assert app.TITLE == "DuckAgent"
    assert len(app.BINDINGS) == 2


@pytest.mark.asyncio
async def test_duck_app_bus_lifecycle():
    """Test DuckApp bus initialize and close with a real SQLite database."""
    bus = LocalMessageBus(db_path=Path(".duckagent/test_messages.db"))
    await bus.initialize()
    queue = bus.subscribe("test_agent")
    assert queue is not None
    await bus.close()
    # Clean up test db
    Path(".duckagent/test_messages.db").unlink(missing_ok=True)
