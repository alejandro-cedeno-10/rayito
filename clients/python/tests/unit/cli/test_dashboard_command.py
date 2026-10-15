"""`rayito dashboard`: validación de argumentos antes de tocar nada, la URL
con el token impresa una sola vez, el modo (sólo lectura por defecto) y que
construir el panel no hace ninguna llamada a AWS."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rayito._dashboard._fleet import Fleet
from rayito._limits import (
    COST_CACHE_TTL_SECONDS,
    COST_EXPLORER_REQUEST_PRICE_USD,
    PORT_MAX,
    SNAPSHOT_READ_PRICE_USD_PER_GB,
    SNAPSHOT_STORAGE_PRICE_USD_PER_GB_MONTH,
    SNAPSHOT_WRITE_PRICE_USD_PER_GB,
)
from rayito.cli import _dashboard_server
from rayito.cli._session import Clients
from rayito.cli.app import app
from rayito.cli.dashboard import (
    ALLOW_ACTIONS_HELP,
    COST_EXPLORER_HELP,
    EVENTS_STACK_HELP,
    LOG_GROUP_HELP,
    usd_es,
)

from .conftest import FakeControlPlane, Stubs

TOKEN_URL = r"http://127\.0\.0\.1:\d+/\?token=[A-Za-z0-9_-]+"


@pytest.fixture
def stop_immediately(monkeypatch: pytest.MonkeyPatch) -> None:
    def interrupt(self: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(_dashboard_server.LocalDashboard, "serve_forever", interrupt)


def test_it_prints_the_start_url_once_and_starts_read_only(
    runner: CliRunner, clients: Clients, stop_immediately: None, stubbed_clients: Stubs
) -> None:
    result = runner.invoke(app, ["dashboard"], obj=clients)

    assert result.exit_code == 0, result.output
    assert len(re.findall(TOKEN_URL, result.output)) == 1
    assert "sólo lectura" in result.output
    assert "--allow-actions" not in result.output
    assert "Panel cerrado." in result.output


def test_the_cost_explorer_help_quotes_the_constants() -> None:
    assert f"{COST_EXPLORER_REQUEST_PRICE_USD:.2f} USD" in COST_EXPLORER_HELP
    assert f"{COST_CACHE_TTL_SECONDS // 3600} h" in COST_EXPLORER_HELP


def test_the_allow_actions_help_says_that_resume_bills_compute_again() -> None:
    assert "resume" in ALLOW_ACTIONS_HELP
    assert "vuelve a facturar cómputo" in ALLOW_ACTIONS_HELP
    for price in (
        SNAPSHOT_WRITE_PRICE_USD_PER_GB,
        SNAPSHOT_READ_PRICE_USD_PER_GB,
        SNAPSHOT_STORAGE_PRICE_USD_PER_GB_MONTH,
    ):
        assert usd_es(price) in ALLOW_ACTIONS_HELP


def test_the_sdk_docstring_quotes_the_same_action_prices_as_the_cli_help() -> None:
    text = " ".join((Fleet.__doc__ or "").split())
    for price in (
        SNAPSHOT_WRITE_PRICE_USD_PER_GB,
        SNAPSHOT_READ_PRICE_USD_PER_GB,
        SNAPSHOT_STORAGE_PRICE_USD_PER_GB_MONTH,
    ):
        assert usd_es(price) in text


def test_the_events_and_logs_help_state_their_cost() -> None:
    assert "DE PAGO" in EVENTS_STACK_HELP
    assert "DynamoDB" in EVENTS_STACK_HELP
    assert "sin tarifa propia" in LOG_GROUP_HELP


def test_the_command_help_lists_what_costs(runner: CliRunner) -> None:
    output = " ".join(runner.invoke(app, ["dashboard", "--help"]).output.split())
    assert "--cost-explorer" in output
    assert "de pago por consulta" in output
    assert "resume vuelve a facturar" in output


def test_the_banner_announces_the_paid_cost_explorer_mode(
    runner: CliRunner, clients: Clients, stop_immediately: None
) -> None:
    default = runner.invoke(app, ["dashboard"], obj=clients)
    paid = runner.invoke(app, ["dashboard", "--cost-explorer"], obj=clients)
    assert "coste facturado activado" not in default.output
    assert "coste facturado activado" in paid.output
    assert f"{COST_EXPLORER_REQUEST_PRICE_USD:.2f} USD" in paid.output


def test_allow_actions_is_announced(
    runner: CliRunner, clients: Clients, stop_immediately: None
) -> None:
    result = runner.invoke(app, ["dashboard", "--allow-actions"], obj=clients)
    assert result.exit_code == 0
    assert "con acciones" in result.output


def test_it_makes_no_aws_call_until_the_spa_asks(
    runner: CliRunner,
    clients: Clients,
    fake_plane: FakeControlPlane,
    stubbed_clients: Stubs,
    stop_immediately: None,
) -> None:
    result = runner.invoke(
        app,
        ["dashboard", "--cost-explorer", "--allow-actions", "--events-stack", "ev"],
        obj=clients,
    )
    assert result.exit_code == 0, result.output
    assert fake_plane.terminated == []
    for stubber in stubbed_clients.stubbers.values():
        stubber.assert_no_pending_responses()


@pytest.mark.parametrize("port", ["-1", str(PORT_MAX + 1)])
def test_an_invalid_port_exits_2_before_any_call(
    runner: CliRunner, clients: Clients, port: str
) -> None:
    result = runner.invoke(app, ["dashboard", "--port", port], obj=clients)
    assert result.exit_code == 2
    assert "--port" in result.output


def test_a_busy_port_exits_2(runner: CliRunner, clients: Clients) -> None:
    import socket

    with socket.socket() as held:
        held.bind(("127.0.0.1", 0))
        held.listen()
        busy = held.getsockname()[1]
        result = runner.invoke(app, ["dashboard", "--port", str(busy)], obj=clients)
    assert result.exit_code == 2
    assert "puerto" in result.output


def test_dashboard_is_listed_in_the_help(runner: CliRunner) -> None:
    assert "dashboard" in runner.invoke(app, ["--help"]).output


# ------------------------------------------------------------ tema y logo


def test_theme_and_logo_are_validated_before_anything_starts(
    runner: CliRunner, clients: Clients, stop_immediately: None, tmp_path: Path
) -> None:
    theme = tmp_path / "t.json"
    theme.write_text('{"version": 1, "name": "demo", "dark": {"bg": "#000"}}', encoding="utf-8")
    logo = tmp_path / "logo.svg"
    logo.write_text('<svg xmlns="http://www.w3.org/2000/svg"/>', encoding="utf-8")
    result = runner.invoke(
        app, ["dashboard", "--theme", str(theme), "--logo", str(logo)], obj=clients
    )
    assert result.exit_code == 0, result.output


def test_the_theme_can_come_from_the_environment(
    runner: CliRunner,
    clients: Clients,
    stop_immediately: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    theme = tmp_path / "t.css"
    theme.write_text(":root { --bg: #000; }", encoding="utf-8")
    monkeypatch.setenv("RAYITO_DASHBOARD_THEME", str(theme))
    assert runner.invoke(app, ["dashboard"], obj=clients).exit_code == 0
    theme.write_text(":root { --bg: url(x); }", encoding="utf-8")
    assert runner.invoke(app, ["dashboard"], obj=clients).exit_code == 2


@pytest.mark.parametrize(
    ("name", "content", "flag"),
    [
        ("t.json", '{"version": 1, "tokens": {"nope": "1"}}', "--theme"),
        ("t.txt", "{}", "--theme"),
        ("t.json", "\xff", "--theme"),
        ("logo.svg", "<svg xmlns='http://www.w3.org/2000/svg'><script/></svg>", "--logo"),
        ("logo.png", "no es un png", "--logo"),
    ],
)
def test_an_invalid_theme_or_logo_exits_2_without_echoing_values(
    runner: CliRunner, clients: Clients, tmp_path: Path, name: str, content: str, flag: str
) -> None:
    path = tmp_path / name
    path.write_bytes(content.encode("latin-1"))
    result = runner.invoke(app, ["dashboard", flag, str(path)], obj=clients)
    assert result.exit_code == 2
    assert flag in result.output or "tema" in result.output or "logo" in result.output
    assert str(tmp_path) not in result.output


def test_a_missing_theme_file_exits_2(runner: CliRunner, clients: Clients, tmp_path: Path) -> None:
    result = runner.invoke(app, ["dashboard", "--theme", str(tmp_path / "no.json")], obj=clients)
    assert result.exit_code == 2 and str(tmp_path) not in result.output
