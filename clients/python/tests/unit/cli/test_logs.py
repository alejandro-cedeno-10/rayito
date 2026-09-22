"""`rayito.cli._logs` y `rayito sandbox logs`: stream exacto, recorrido de
respaldo acotado a 10 páginas, grupo inexistente, `--since` y la paginación
de eventos que para en el token repetido y en `--limit`."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from typer.testing import CliRunner

from rayito.cli import _logs
from rayito.cli._session import Clients
from rayito.cli.app import app

from .conftest import FakeControlPlane, Stubs, sandbox_info

GROUP = "/rayito/rayito-base"
EXACT = "2026/09/15[1.0]microvm-x"
LATER = "2026/09/16[1.0]microvm-x"
SCAN_PARAMS: dict[str, Any] = {
    "logGroupName": GROUP,
    "orderBy": "LastEventTime",
    "descending": True,
    "limit": 50,
}


@pytest.fixture
def info() -> Any:
    return sandbox_info("microvm-x")


def stream(name: str) -> dict[str, Any]:
    return {"logStreamName": name}


def test_expected_stream_name_and_default_group(info: Any) -> None:
    assert _logs.expected_stream_name(info) == EXACT
    assert _logs.default_log_group(info.template) == GROUP
    shifted = sandbox_info("microvm-x", started_at=datetime(2026, 9, 15, 23, 30, tzinfo=UTC))
    assert _logs.expected_stream_name(shifted) == EXACT


def test_exact_stream_found(stubbed_clients: Stubs, info: Any) -> None:
    stubbed_clients.logs.add_response(
        "describe_log_streams",
        {"logStreams": [stream(EXACT), stream(EXACT + "-other")]},
        {"logGroupName": GROUP, "logStreamNamePrefix": EXACT},
    )
    assert _logs.find_streams(stubbed_clients.clients["logs"], GROUP, info) == [EXACT]


def test_fallback_scan_finds_a_later_day(stubbed_clients: Stubs, info: Any) -> None:
    stubbed_clients.logs.add_response(
        "describe_log_streams",
        {"logStreams": []},
        {"logGroupName": GROUP, "logStreamNamePrefix": EXACT},
    )
    stubbed_clients.logs.add_response(
        "describe_log_streams",
        {"logStreams": [stream("2026/09/16[1.0]microvm-y"), stream(LATER)], "nextToken": "t1"},
        SCAN_PARAMS,
    )
    stubbed_clients.logs.add_response(
        "describe_log_streams",
        {"logStreams": [stream("2026/09/14[1.0]microvm-x-not")]},
        {**SCAN_PARAMS, "nextToken": "t1"},
    )
    assert _logs.find_streams(stubbed_clients.clients["logs"], GROUP, info) == [LATER]


def test_scan_stops_after_ten_pages(stubbed_clients: Stubs, info: Any) -> None:
    stubbed_clients.logs.add_response(
        "describe_log_streams",
        {"logStreams": []},
        {"logGroupName": GROUP, "logStreamNamePrefix": EXACT},
    )
    token: str | None = None
    for page in range(10):
        params = dict(SCAN_PARAMS) if token is None else {**SCAN_PARAMS, "nextToken": token}
        token = f"t{page}"
        stubbed_clients.logs.add_response(
            "describe_log_streams", {"logStreams": [stream("x")], "nextToken": token}, params
        )
    with pytest.raises(_logs.LogsNotFound, match="sin logs"):
        _logs.find_streams(stubbed_clients.clients["logs"], GROUP, info)


def test_missing_group_is_logs_not_found(stubbed_clients: Stubs, info: Any) -> None:
    stubbed_clients.logs.add_client_error(
        "describe_log_streams",
        service_error_code="ResourceNotFoundException",
        service_message="The specified log group does not exist.",
    )
    with pytest.raises(_logs.LogsNotFound) as raised:
        _logs.find_streams(stubbed_clients.clients["logs"], GROUP, info)
    message = str(raised.value)
    assert (
        "logging disabled" in message and "executionRoleArn" in message and "--log-group" in message
    )


def test_parse_since() -> None:
    now = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
    assert _logs.parse_since("30m", now) == datetime(2026, 9, 16, 11, 30, tzinfo=UTC)
    assert _logs.parse_since("2h", now) == datetime(2026, 9, 16, 10, 0, tzinfo=UTC)
    assert _logs.parse_since("1d", now) == datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
    assert _logs.parse_since("2026-09-16T10:00:00Z", now) == datetime(2026, 9, 16, 10, tzinfo=UTC)
    assert _logs.parse_since("2026-09-16T10:00:00", now).tzinfo is UTC
    with pytest.raises(ValueError, match="--since"):
        _logs.parse_since("ayer", now)


def test_iter_events_stops_on_repeated_token_and_at_limit(stubbed_clients: Stubs) -> None:
    logs = stubbed_clients.clients["logs"]
    base = {"logGroupName": GROUP, "logStreamName": EXACT, "startFromHead": True}
    stubbed_clients.logs.add_response(
        "get_log_events",
        {
            "events": [{"timestamp": 1, "message": "a"}, {"timestamp": 2, "message": "b"}],
            "nextForwardToken": "f1",
        },
        base,
    )
    stubbed_clients.logs.add_response(
        "get_log_events",
        {"events": [{"timestamp": 3, "message": "c"}], "nextForwardToken": "f2"},
        {**base, "nextToken": "f1"},
    )
    stubbed_clients.logs.add_response(
        "get_log_events", {"events": [], "nextForwardToken": "f2"}, {**base, "nextToken": "f2"}
    )
    assert [e["message"] for e in _logs.iter_events(logs, GROUP, EXACT)] == ["a", "b", "c"]
    stubbed_clients.logs.add_response(
        "get_log_events",
        {
            "events": [{"timestamp": 1, "message": "a"}, {"timestamp": 2, "message": "b"}],
            "nextForwardToken": "f1",
        },
        {**base, "startTime": 5},
    )
    limited = list(_logs.iter_events(logs, GROUP, EXACT, start_time_ms=5, limit=1))
    assert [e["message"] for e in limited] == ["a"]


def test_logs_command_prints_iso_timestamps(
    runner: CliRunner, clients: Clients, stubbed_clients: Stubs, fake_plane: FakeControlPlane
) -> None:
    fake_plane.infos["microvm-x"] = sandbox_info("microvm-x")
    stubbed_clients.logs.add_response(
        "describe_log_streams",
        {"logStreams": [stream(EXACT)]},
        {"logGroupName": GROUP, "logStreamNamePrefix": EXACT},
    )
    stubbed_clients.logs.add_response(
        "get_log_events",
        {
            "events": [{"timestamp": 1_789_000_000_123, "message": "rayd listo\n"}],
            "nextForwardToken": "f",
        },
        {"logGroupName": GROUP, "logStreamName": EXACT, "startFromHead": True},
    )
    stubbed_clients.logs.add_response(
        "get_log_events",
        {"events": [], "nextForwardToken": "f"},
        {"logGroupName": GROUP, "logStreamName": EXACT, "startFromHead": True, "nextToken": "f"},
    )
    result = runner.invoke(app, ["sandbox", "logs", "microvm-x"], obj=clients)
    assert result.exit_code == 0, result.stderr
    assert result.stdout == "2026-09-10T00:26:40.123Z rayd listo\n"
    assert fake_plane.tokens == []


def test_logs_command_json_and_absence(
    runner: CliRunner, clients: Clients, stubbed_clients: Stubs, fake_plane: FakeControlPlane
) -> None:
    fake_plane.infos["microvm-x"] = sandbox_info("microvm-x")
    stubbed_clients.logs.add_response(
        "describe_log_streams",
        {"logStreams": []},
        {"logGroupName": "/custom", "logStreamNamePrefix": EXACT},
    )
    stubbed_clients.logs.add_response(
        "describe_log_streams", {"logStreams": []}, {**SCAN_PARAMS, "logGroupName": "/custom"}
    )
    result = runner.invoke(
        app, ["--json", "sandbox", "logs", "microvm-x", "--log-group", "/custom"], obj=clients
    )
    assert result.exit_code == 1
    assert "sin logs" in result.stderr and result.stdout == ""
    stubbed_clients.logs.add_response(
        "describe_log_streams",
        {"logStreams": [stream(EXACT)]},
        {"logGroupName": GROUP, "logStreamNamePrefix": EXACT},
    )
    stubbed_clients.logs.add_response(
        "get_log_events",
        {"events": [{"timestamp": 1, "message": "x"}]},
        {"logGroupName": GROUP, "logStreamName": EXACT, "startFromHead": True, "startTime": 0},
    )
    result = runner.invoke(
        app,
        ["--json", "sandbox", "logs", "microvm-x", "--since", "1970-01-01T00:00:00Z"],
        obj=clients,
    )
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout) == [
        {"timestamp": "1970-01-01T00:00:00.001Z", "message": "x", "stream": EXACT}
    ]
