"""El adaptador de logs del panel local: lee el stream del sandbox con las
funciones de `rayito sandbox logs`, acota las líneas y sólo reporta que hay
más; sin logs (grupo inexistente) devuelve una página vacía."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from botocore.exceptions import ClientError

from rayito._dashboard._logs_aws import CloudWatchLogsSource
from rayito._limits import DASHBOARD_LOG_LINE_MAX_CHARS
from rayito.cli._session import Clients
from rayito.exceptions import AuthenticationException

from .conftest import FakeControlPlane, Stubs, sandbox_info

SANDBOX = "microvm-x"
GROUP = "/rayito/rayito-base"
STREAM = "2026/09/15[1.0]microvm-x"
STREAM_PARAMS = {"logGroupName": GROUP, "logStreamNamePrefix": STREAM}
EVENT_PARAMS = {"logGroupName": GROUP, "logStreamName": STREAM, "startFromHead": False}


def tail_params(limit: int, **extra: object) -> dict[str, object]:
    return {**EVENT_PARAMS, "limit": limit, **extra}


TIMESTAMP_MS = int(datetime(2026, 9, 15, 14, 40, tzinfo=UTC).timestamp() * 1000)


@pytest.fixture(autouse=True)
def known_sandbox(fake_plane: FakeControlPlane) -> None:
    fake_plane.infos[SANDBOX] = sandbox_info(SANDBOX)


def events(*messages: str) -> dict[str, Any]:
    return {
        "events": [{"timestamp": TIMESTAMP_MS, "message": message} for message in messages],
        "nextBackwardToken": "start",
    }


def test_it_reads_the_stream_and_trims_each_line(clients: Clients, stubbed_clients: Stubs) -> None:
    stubbed_clients.logs.add_response(
        "describe_log_streams", {"logStreams": [{"logStreamName": STREAM}]}, STREAM_PARAMS
    )
    long_line = "x" * (DASHBOARD_LOG_LINE_MAX_CHARS + 50)
    stubbed_clients.logs.add_response(
        "get_log_events", events("hola\n", long_line), tail_params(11)
    )
    stubbed_clients.logs.add_response("get_log_events", events(), tail_params(9, nextToken="start"))

    page = CloudWatchLogsSource(clients.logs, clients.control_plane).read(SANDBOX, limit=10)

    assert page.truncated is False
    assert page.lines[0].message == "hola"
    assert len(page.lines[1].message) == DASHBOARD_LOG_LINE_MAX_CHARS
    assert page.lines[0].stream == STREAM
    assert page.lines[0].timestamp.endswith("Z")


def test_it_says_when_there_are_older_lines_and_keeps_the_newest(
    clients: Clients, stubbed_clients: Stubs
) -> None:
    stubbed_clients.logs.add_response(
        "describe_log_streams", {"logStreams": [{"logStreamName": STREAM}]}, STREAM_PARAMS
    )
    stubbed_clients.logs.add_response("get_log_events", events("a", "b", "c"), tail_params(3))

    page = CloudWatchLogsSource(clients.logs, clients.control_plane).read(SANDBOX, limit=2)

    assert [line.message for line in page.lines] == ["b", "c"]
    assert page.truncated is True


def test_the_name_search_fallback_is_flagged(clients: Clients, stubbed_clients: Stubs) -> None:
    found = "2099/01/01[1.0]microvm-x"
    stubbed_clients.logs.add_response("describe_log_streams", {"logStreams": []}, STREAM_PARAMS)
    stubbed_clients.logs.add_response(
        "describe_log_streams",
        {"logStreams": [{"logStreamName": found}]},
        {"logGroupName": GROUP, "orderBy": "LastEventTime", "descending": True, "limit": 50},
    )
    stubbed_clients.logs.add_response(
        "get_log_events",
        events("a"),
        {**EVENT_PARAMS, "logStreamName": found, "limit": 11},
    )
    stubbed_clients.logs.add_response(
        "get_log_events",
        events(),
        {**EVENT_PARAMS, "logStreamName": found, "limit": 10, "nextToken": "start"},
    )
    page = CloudWatchLogsSource(clients.logs, clients.control_plane).read(SANDBOX, limit=10)
    assert page.fallback is True and page.lines[0].stream == found


def test_the_expected_stream_is_not_flagged(clients: Clients, stubbed_clients: Stubs) -> None:
    stubbed_clients.logs.add_response(
        "describe_log_streams", {"logStreams": [{"logStreamName": STREAM}]}, STREAM_PARAMS
    )
    stubbed_clients.logs.add_response("get_log_events", events("a"), tail_params(11))
    stubbed_clients.logs.add_response(
        "get_log_events", events(), tail_params(10, nextToken="start")
    )
    page = CloudWatchLogsSource(clients.logs, clients.control_plane).read(SANDBOX, limit=10)
    assert page.fallback is False


def test_a_missing_log_group_is_an_empty_page(clients: Clients, stubbed_clients: Stubs) -> None:
    stubbed_clients.logs.add_client_error(
        "describe_log_streams", service_error_code="ResourceNotFoundException"
    )
    page = CloudWatchLogsSource(clients.logs, clients.control_plane).read(SANDBOX, limit=10)
    assert page.lines == () and page.truncated is False


def test_the_log_group_can_be_overridden(clients: Clients, stubbed_clients: Stubs) -> None:
    stubbed_clients.logs.add_response(
        "describe_log_streams",
        {"logStreams": []},
        {"logGroupName": "/custom", "logStreamNamePrefix": STREAM},
    )
    stubbed_clients.logs.add_response(
        "describe_log_streams",
        {"logStreams": []},
        {"logGroupName": "/custom", "orderBy": "LastEventTime", "descending": True, "limit": 50},
    )
    page = CloudWatchLogsSource(clients.logs, clients.control_plane, log_group="/custom").read(
        SANDBOX, limit=10
    )
    assert page.lines == ()


def test_a_denied_logs_call_is_an_sdk_authentication_error(
    clients: Clients, stubbed_clients: Stubs
) -> None:
    stubbed_clients.logs.add_client_error(
        "describe_log_streams", service_error_code="AccessDeniedException"
    )
    with pytest.raises(AuthenticationException) as raised:
        CloudWatchLogsSource(clients.logs, clients.control_plane).read(SANDBOX, limit=10)
    assert not isinstance(raised.value.__cause__, ClientError)
    assert raised.value.aws_code == "AccessDeniedException"
