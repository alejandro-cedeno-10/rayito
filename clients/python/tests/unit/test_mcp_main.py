"""`python -m rayito.mcp`: `parse_args`, `run` con un runner que graba los
argumentos exactos, `main` con un entorno malformado y el aviso de HTTP sin
autenticación fuera de loopback (design D9)."""

from __future__ import annotations

import logging
import subprocess
import sys
from typing import Any

import pytest

import rayito.mcp._cli as entrypoint
from rayito.mcp import McpSettings, build_server
from rayito.mcp._cli import (
    RunOptions,
    http_authority,
    is_loopback,
    is_wildcard,
    main,
    parse_args,
    run,
)
from rayito.mcp._settings import IDLE_ENV_VAR, TIMEOUT_ENV_VAR

Call = tuple[tuple[Any, ...], dict[str, Any]]


class RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[Call] = []

    def __call__(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append((args, kwargs))


def test_parse_args_defaults_to_stdio() -> None:
    assert parse_args([]) == RunOptions(http=False, host="127.0.0.1", port=8000)


def test_parse_args_http_defaults() -> None:
    assert parse_args(["--http"]) == RunOptions(http=True, host="127.0.0.1", port=8000)


def test_parse_args_http_host_and_port() -> None:
    options = parse_args(["--http", "--host", "192.168.1.5", "--port", "9000"])
    assert options == RunOptions(http=True, host="192.168.1.5", port=9000)


@pytest.mark.parametrize("host", ["0.0.0.0", "::"])
def test_parse_args_rejects_a_wildcard_host(host: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        parse_args(["--http", "--host", host])
    assert exit_info.value.code == 2
    assert "Host" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [["--port", "1"], ["--host", "192.168.1.5"]])
def test_transport_flags_without_http_are_a_usage_error(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        parse_args(argv)
    assert exit_info.value.code == 2
    assert "--http" in capsys.readouterr().err


def test_run_stdio_invokes_the_runner_without_arguments() -> None:
    runner = RecordingRunner()
    run(build_server(McpSettings()), parse_args([]), runner=runner)
    assert runner.calls == [((), {})]


def test_run_http_invokes_the_runner_with_transport_host_and_port(
    caplog: pytest.LogCaptureFixture,
) -> None:
    runner = RecordingRunner()
    with caplog.at_level(logging.WARNING, logger="rayito.mcp"):
        run(build_server(McpSettings()), parse_args(["--http"]), runner=runner)
    ((), kwargs) = runner.calls[0]
    assert len(runner.calls) == 1
    assert kwargs["transport"] == "streamable-http"
    assert (kwargs["host"], kwargs["port"]) == ("127.0.0.1", 8000)
    assert kwargs["transport_security"].allowed_hosts == ["127.0.0.1:8000"]
    assert "sin autenticación" not in caplog.text


def test_run_http_outside_loopback_warns(caplog: pytest.LogCaptureFixture) -> None:
    runner = RecordingRunner()
    options = parse_args(["--http", "--host", "192.168.1.5", "--port", "9000"])
    with caplog.at_level(logging.WARNING, logger="rayito.mcp"):
        run(build_server(McpSettings()), options, runner=runner)
    ((), kwargs) = runner.calls[0]
    assert len(runner.calls) == 1
    assert (kwargs["host"], kwargs["port"]) == ("192.168.1.5", 9000)
    assert kwargs["transport_security"].allowed_hosts == ["192.168.1.5:9000"]
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert any("sin autenticación" in record.getMessage() for record in warnings)
    assert any("192.168.1.5:9000" in record.getMessage() for record in warnings)


@pytest.mark.parametrize(
    ("host", "loopback"),
    [("127.0.0.1", True), ("::1", True), ("localhost", True), ("0.0.0.0", False), ("host", False)],
)
def test_is_loopback(host: str, loopback: bool) -> None:
    assert is_loopback(host) is loopback


@pytest.mark.parametrize(
    ("host", "wildcard"),
    [("0.0.0.0", True), ("::", True), ("127.0.0.1", False), ("::1", False), ("host", False)],
)
def test_is_wildcard(host: str, wildcard: bool) -> None:
    assert is_wildcard(host) is wildcard


@pytest.mark.parametrize(
    ("host", "authority"),
    [
        ("127.0.0.1", "127.0.0.1:8000"),
        ("127.0.0.2", "127.0.0.2:8000"),
        ("::1", "[::1]:8000"),
    ],
)
def test_run_http_always_passes_explicit_transport_security(host: str, authority: str) -> None:
    runner = RecordingRunner()
    options = parse_args(["--http", "--host", host])
    run(build_server(McpSettings()), options, runner=runner)
    ((), kwargs) = runner.calls[0]
    settings = kwargs["transport_security"]
    assert settings.enable_dns_rebinding_protection is True
    assert settings.allowed_hosts == [authority]
    assert settings.allowed_origins == [f"http://{authority}"]


def test_http_authority_brackets_ipv6() -> None:
    assert http_authority("127.0.0.1", 8000) == "127.0.0.1:8000"
    assert http_authority("::1", 8000) == "[::1]:8000"
    assert http_authority("fe80::1", 9000) == "[fe80::1]:9000"
    assert http_authority("localhost", 8000) == "localhost:8000"


def test_main_returns_2_on_a_malformed_environment(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(IDLE_ENV_VAR, "abc")
    assert main([]) == 2
    err = capsys.readouterr().err
    assert IDLE_ENV_VAR in err
    assert "abc" in err


def test_main_builds_the_server_from_the_environment_and_runs_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[McpSettings, RunOptions]] = []

    def fake_run(server: Any, options: RunOptions, *, runner: Any = None) -> None:
        seen.append((server.settings_for_test, options))

    def fake_build(settings: McpSettings) -> Any:
        return type("Built", (), {"settings_for_test": settings})()

    monkeypatch.setenv(TIMEOUT_ENV_VAR, "900")
    monkeypatch.setattr(entrypoint, "build_server", fake_build)
    monkeypatch.setattr(entrypoint, "run", fake_run)
    assert main(["--http", "--port", "9001"]) == 0
    assert len(seen) == 1
    settings, options = seen[0]
    assert settings.timeout_seconds == 900
    assert options == RunOptions(http=True, port=9001)


def test_module_help_lists_the_transport_flags_without_double_import() -> None:
    completed = subprocess.run(
        [sys.executable, "-W", "error::RuntimeWarning", "-m", "rayito.mcp", "--help"],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    assert "--http" in completed.stdout
    assert "--host" in completed.stdout
    assert "--port" in completed.stdout
    assert "found in sys.modules" not in completed.stderr
