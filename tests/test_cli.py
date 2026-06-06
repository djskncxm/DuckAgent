import pytest
from typer.testing import CliRunner
from unittest.mock import patch, AsyncMock

from duckagent.cli.app import app

runner = CliRunner()


def test_cli_help():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "run" in result.stdout
    assert "log" in result.stdout
    assert "send" in result.stdout


def test_cli_run_help():
    result = runner.invoke(app, ["run", "--help"])
    assert result.exit_code == 0
    assert "TUI" in result.stdout or "交互" in result.stdout
    assert "--local" in result.stdout
    assert "--connect" in result.stdout
    assert "--port" in result.stdout


def test_cli_server_help():
    result = runner.invoke(app, ["server", "--help"])
    assert result.exit_code == 0
    assert "--port" in result.stdout


def test_cli_agent_help():
    result = runner.invoke(app, ["agent", "--help"])
    assert result.exit_code == 0
    assert "--server-url" in result.stdout


def test_cli_log_help():
    result = runner.invoke(app, ["log", "--help"])
    assert result.exit_code == 0
    assert "--from" in result.stdout
    assert "--limit" in result.stdout
    assert "--type" in result.stdout


def test_cli_send_help():
    result = runner.invoke(app, ["send", "--help"])
    assert result.exit_code == 0


@patch("duckagent.cli.app._show_log", new_callable=AsyncMock)
def test_cli_log_invokes_show_log(mock_show_log):
    result = runner.invoke(app, ["log"])
    assert result.exit_code == 0


@patch("duckagent.cli.app._send_message", new_callable=AsyncMock)
def test_cli_send_invokes_send_message(mock_send):
    result = runner.invoke(app, ["send", "hello agent"])
    assert result.exit_code == 0
