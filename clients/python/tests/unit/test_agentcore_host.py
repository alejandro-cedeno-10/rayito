"""Lo que el host del agente hace por AgentCore (ADR-031): leer las
credenciales del rol antes de cada invocación para los adaptadores de Node que
sólo usan variables de entorno, y traducir el id de sesión de la cabecera de
AgentCore al del driver. El host se prueba una vez, aquí
(`parity-checklist.md`)."""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from rayito._agent._runner.agent_host import serve
from rayito._agent._runner.agent_host.aws_credentials import (
    FAILURE_DETAIL_CODE,
    HELPER_TIMEOUT_SECONDS,
    CredentialsHelper,
)
from rayito._agent._runner.agent_host.protocol import AgentFailure, Request
from rayito._limits import AGENT_SERVICE_SESSION_HEADER
from tests.unit.agent_host_support import (
    FakeDriver,
    invoke,
    request,
    running_service,
    sse_messages,
    step,
)

AGENTCORE_SESSION = "rayito-" + "a" * 32
SECOND_SESSION = "rayito-" + "b" * 32


def script(tmp_path: Path, body: str) -> CredentialsHelper:
    path = tmp_path / "helper.py"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return CredentialsHelper(sys.executable, str(path))


def test_the_helper_reads_the_roles_credentials_from_the_tool(tmp_path: Path) -> None:
    helper = script(
        tmp_path,
        """
        import json
        print(json.dumps({"AWS_ACCESS_KEY_ID": "AKIDEXAMPLE", "AWS_SECRET_ACCESS_KEY": "s",
                          "AWS_SESSION_TOKEN": "t"}))
        """,
    )
    assert dict(helper()) == {
        "AWS_ACCESS_KEY_ID": "AKIDEXAMPLE",
        "AWS_SECRET_ACCESS_KEY": "s",
        "AWS_SESSION_TOKEN": "t",
    }


@pytest.mark.parametrize(
    "body",
    [
        "import sys; sys.exit(1)",
        "print('no es json')",
        "print('[]')",
        'print(\'{"AWS_ACCESS_KEY_ID": "a"}\')',
        'print(\'{"AWS_ACCESS_KEY_ID": "a", "AWS_SECRET_ACCESS_KEY": "b", "OTRA": "c"}\')',
        'print(\'{"AWS_ACCESS_KEY_ID": 1, "AWS_SECRET_ACCESS_KEY": "b"}\')',
    ],
)
def test_a_tool_that_fails_or_answers_badly_is_aws_credentials_unavailable(
    tmp_path: Path, body: str
) -> None:
    with pytest.raises(AgentFailure) as raised:
        script(tmp_path, body)()
    assert (raised.value.reason, raised.value.detail_code) == (
        "runtime_error",
        FAILURE_DETAIL_CODE,
    )


def test_a_missing_tool_is_aws_credentials_unavailable() -> None:
    with pytest.raises(AgentFailure) as raised:
        CredentialsHelper("/no/existe/python", "/no/existe.py")()
    assert raised.value.detail_code == "aws_credentials_unavailable"


def test_the_tool_has_a_ten_second_deadline() -> None:
    assert HELPER_TIMEOUT_SECONDS == 10


def credentials_config() -> serve.ServiceConfig:
    return serve.ServiceConfig(
        aws_credentials=lambda: {"AWS_ACCESS_KEY_ID": "AKIDEXAMPLE", "AWS_SECRET_ACCESS_KEY": "s"}
    )


def test_every_invocation_carries_fresh_credentials_to_the_driver() -> None:
    driver = FakeDriver(step(1, text="42"))
    with running_service(driver, config=credentials_config()) as (_, port):
        invoke(port)
        invoke(port)
    assert [dict(request.aws_env or {}) for request in driver.requests] == [
        {"AWS_ACCESS_KEY_ID": "AKIDEXAMPLE", "AWS_SECRET_ACCESS_KEY": "s"}
    ] * 2


def test_without_credentials_configured_the_driver_gets_none() -> None:
    driver = FakeDriver(step(1, text="42"))
    with running_service(driver) as (_, port):
        invoke(port)
    assert [request.aws_env for request in driver.requests] == [None]


def test_unreadable_credentials_end_the_invocation_with_a_closed_code() -> None:
    def unavailable() -> dict[str, str]:
        raise AgentFailure("runtime_error", FAILURE_DETAIL_CODE)

    driver = FakeDriver(step(1, text="42"))
    config = serve.ServiceConfig(aws_credentials=unavailable)
    with running_service(driver, config=config) as (_, port):
        status, _, body = invoke(port)
    assert status == 200
    assert body["failed"] == {"reason": "runtime_error", "detail_code": FAILURE_DETAIL_CODE}
    assert driver.requests == []


def header(session: str) -> dict[str, str]:
    return {AGENT_SERVICE_SESSION_HEADER: session}


def test_a_new_agentcore_session_starts_a_driver_session_and_answers_with_the_header_id() -> None:
    driver = FakeDriver(step(1, text="42"))
    with running_service(driver) as (_, port):
        status, _, body = invoke(port, headers=header(AGENTCORE_SESSION))
    assert status == 200
    assert driver.requests[0].session_id is None
    assert body["session_id"] == AGENTCORE_SESSION


def test_the_next_call_of_the_same_agentcore_session_continues_the_driver_session() -> None:
    driver = FakeDriver(step(1, text="42"), session_id="rda_" + "1" * 32)
    with running_service(driver) as (_, port):
        invoke(port, headers=header(AGENTCORE_SESSION))
        _, _, body = invoke(port, headers=header(AGENTCORE_SESSION))
        invoke(port, headers=header(SECOND_SESSION))
    assert [request.session_id for request in driver.requests] == [
        None,
        "rda_" + "1" * 32,
        None,
    ]
    assert body["session_id"] == AGENTCORE_SESSION


def test_the_stream_of_an_agentcore_session_carries_the_header_id_in_every_event() -> None:
    driver = FakeDriver(step(1, text="42"), session_id="rda_" + "2" * 32)
    with running_service(driver) as (_, port):
        _, _, data = request(
            port,
            "POST",
            "/invocations",
            {"prompt": "hola"},
            {"Accept": "text/event-stream", **header(AGENTCORE_SESSION)},
        )
    events: list[dict[str, Any]] = [payload for _, payload in sse_messages(data)]
    with_session = [event for event in events if "session_id" in event]
    assert {event["session_id"] for event in with_session} == {AGENTCORE_SESSION}
    assert events[-1]["type"] == "done"
    assert driver.requests[0].session_id is None


def test_the_request_repr_does_not_print_the_credentials() -> None:
    request = Request(
        prompt="hola",
        aws_env={"AWS_SECRET_ACCESS_KEY": "wJalrXUtnFEMI-example-key-material"},
    )
    assert "example-key-material" not in repr(request)
    assert "AWS_SECRET_ACCESS_KEY" not in repr(request)
