"""`AgentDeployment` y `AsyncAgentDeployment` (`_agent/_deployment*.py`)
sobre un transporte HTTP de doble: la invocación JSON y el stream SSE, los
códigos del contrato, el reacuñado del token del proxy ante un `403`, las
apps `raw` y `connect()` desde las etiquetas del sandbox. Cada caso corre
en las dos superficies con el mismo `Harness`."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest

from rayito._agent._deployment import AgentDeployment
from rayito._agent._deployment_async import AsyncAgentDeployment
from rayito._agent._deployment_base import (
    AGENT_DEPLOYMENT_LABEL,
    AGENT_DEPLOYMENT_PORT_LABEL,
    AGENT_DEPLOYMENT_PROTOCOL_LABEL,
    labels_for,
)
from rayito._agent._events import AgentEvent, AgentFailed, AgentResult, Done, StepStarted, Text
from rayito._limits import AGENT_SERVICE_PORT
from rayito.exceptions import (
    AgentException,
    AuthenticationException,
    InvalidArgumentException,
    SandboxException,
    TimeoutException,
    UnimplementedError,
)
from tests.unit.fake_agent_project import (
    ENDPOINT,
    RESULT_BODY,
    SANDBOX_ID,
    AsyncFakeSandbox,
    AsyncScriptedTransport,
    FakeSandbox,
    Scripted,
    ScriptedTransport,
    json_body,
    sse,
)

PROMPT = "prompt-secreto-del-usuario"
SESSION = "ses_0000000000000000000000000a"


class Harness:
    """Una deployment de doble con una API síncrona sobre las dos
    superficies."""

    def __init__(self, flavor: str, *responses: Scripted | Exception, raw: bool = False) -> None:
        self.flavor = flavor
        if flavor == "async":
            self.sandbox: FakeSandbox = AsyncFakeSandbox()
            self.transport: ScriptedTransport = AsyncScriptedTransport(*responses)
        else:
            self.sandbox = FakeSandbox()
            self.transport = ScriptedTransport(*responses)
        self.raw = raw
        self.deployment: Any = self._build()

    def _build(self) -> Any:
        if self.flavor == "async":
            host_access = FakeSandbox.get_host(self.sandbox, AGENT_SERVICE_PORT)
            return AsyncAgentDeployment(
                self.sandbox,
                port=AGENT_SERVICE_PORT,
                host_access=host_access,
                project="support-bot",
                raw=self.raw,
                request_timeout=7.0,
                transport_factory=self.transport.factory,
            )
        return AgentDeployment(
            self.sandbox,
            port=AGENT_SERVICE_PORT,
            project="support-bot",
            raw=self.raw,
            request_timeout=7.0,
            transport_factory=self.transport.factory,
        )

    def call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        result = getattr(self.deployment, name)(*args, **kwargs)
        return asyncio.run(result) if self.flavor == "async" else result

    def events(self, *args: Any, **kwargs: Any) -> tuple[list[AgentEvent], Any]:
        """Todos los eventos de `stream()` y el stream ya consumido."""
        stream = self.deployment.stream(*args, **kwargs)
        if self.flavor == "async":

            async def drain() -> list[AgentEvent]:
                async with stream as opened:
                    return [event async for event in opened]

            return asyncio.run(drain()), stream
        with stream as opened:
            return list(opened), stream

    def result_of_stream(self, *args: Any, **kwargs: Any) -> AgentResult:
        stream = self.deployment.stream(*args, **kwargs)
        if self.flavor == "async":

            async def finish() -> AgentResult:
                async with stream as opened:
                    return await opened.result()  # type: ignore[no-any-return]

            return asyncio.run(finish())
        with stream as opened:
            return opened.result()  # type: ignore[no-any-return]


@pytest.fixture(params=["sync", "async"])
def make(request: pytest.FixtureRequest) -> Callable[..., Harness]:
    def build(*responses: Scripted | Exception, raw: bool = False) -> Harness:
        return Harness(request.param, *responses, raw=raw)

    return build


def test_url_and_headers_are_fresh_proxy_access(make: Callable[..., Harness]) -> None:
    harness = make()
    deployment = harness.deployment
    assert deployment.url == f"https://{ENDPOINT}"
    assert deployment.sandbox_id == SANDBOX_ID
    assert deployment.headers["x-aws-proxy-port"] == str(AGENT_SERVICE_PORT)
    before = deployment.headers["x-aws-proxy-auth"]
    harness.sandbox.token.generation += 1
    assert deployment.headers["x-aws-proxy-auth"] != before


def test_invoke_posts_to_invocations_and_returns_the_result(make: Callable[..., Harness]) -> None:
    harness = make(Scripted(body=json_body(RESULT_BODY)))
    result = harness.call("invoke", PROMPT, session_id=SESSION, model="m", reasoning=True)
    assert result == AgentResult(
        session_id=SESSION,
        text="hola mundo",
        steps=2,
        usage=result.usage,
        exit_code=0,
    )
    assert result.usage.total == 15
    (sent,) = harness.transport.requests
    assert (sent.method, sent.path, sent.timeout) == ("POST", "/invocations", 7.0)
    assert sent.headers["x-aws-proxy-auth"] == "jwe-1"
    assert sent.headers["Accept"] == "application/json"
    assert sent.body is not None
    assert b'"prompt":"prompt-secreto-del-usuario"' in sent.body
    assert b'"session_id":"ses_0000000000000000000000000a"' in sent.body
    assert b'"model":"m"' in sent.body and b'"reasoning":true' in sent.body
    assert all(response.closed for response in harness.transport.opened)


def test_invoke_sends_only_what_was_asked(make: Callable[..., Harness]) -> None:
    harness = make(Scripted(body=json_body(RESULT_BODY)))
    harness.call("invoke", "hola")
    assert harness.transport.requests[0].body == b'{"prompt":"hola"}'


def test_a_failed_result_is_an_agent_exception(make: Callable[..., Harness]) -> None:
    body = {**RESULT_BODY, "text": "", "failed": {"reason": "max_steps", "detail_code": None}}
    harness = make(Scripted(body=json_body(body)))
    with pytest.raises(AgentException) as raised:
        harness.call("invoke", "hola")
    assert raised.value.reason == "max_steps"
    assert raised.value.session_id == SESSION
    assert raised.value.usage is not None and raised.value.usage.total == 15


@pytest.mark.parametrize(
    ("status", "error", "reason"),
    [
        (429, AgentException, "busy"),
        (503, TimeoutException, None),
        (400, InvalidArgumentException, None),
        (413, InvalidArgumentException, None),
        (401, AuthenticationException, None),
        (500, SandboxException, None),
    ],
)
def test_http_statuses_map_to_exceptions(
    make: Callable[..., Harness], status: int, error: type[Exception], reason: str | None
) -> None:
    harness = make(Scripted(status=status, body=b"cuerpo del servidor"))
    with pytest.raises(error) as raised:
        harness.call("invoke", PROMPT)
    assert getattr(raised.value, "reason", None) == reason
    message = str(raised.value)
    assert (
        PROMPT not in message and ENDPOINT not in message and "cuerpo del servidor" not in message
    )
    assert harness.transport.opened[0].closed


def test_a_body_outside_the_contract_is_a_sandbox_error(make: Callable[..., Harness]) -> None:
    harness = make(Scripted(body=b"<html>"))
    with pytest.raises(SandboxException):
        harness.call("invoke", "hola")


def test_a_403_reminted_once_then_succeeds(make: Callable[..., Harness]) -> None:
    harness = make(Scripted(status=403), Scripted(body=json_body(RESULT_BODY)))
    assert harness.call("invoke", "hola").text == "hola mundo"
    first, second = harness.transport.requests
    assert (first.headers["x-aws-proxy-auth"], second.headers["x-aws-proxy-auth"]) == (
        "jwe-1",
        "jwe-2",
    )
    assert harness.sandbox._refresher.refreshed == 1


def test_a_second_403_is_an_authentication_error(make: Callable[..., Harness]) -> None:
    harness = make(Scripted(status=403), Scripted(status=403))
    with pytest.raises(AuthenticationException) as raised:
        harness.call("invoke", "hola")
    assert raised.value.proxy_rejected is True
    assert harness.sandbox._refresher.refreshed == 1
    assert len(harness.transport.requests) == 2


def test_stream_decodes_events_and_ends_in_done(make: Callable[..., Harness]) -> None:
    body = sse(
        ("session", {"session_id": SESSION}),
        ("step_started", {"index": 1}),
        ("text_delta", {"text": "ho"}),
        ("text", {"text": "hola"}),
        (
            "step_finished",
            {"index": 1, "usage": {"input": 4, "output": 1}, "finish_reason": "stop"},
        ),
        ("done", {"session_id": SESSION}),
    )
    harness = make(Scripted(chunks=[body[:30], body[30:]]))
    events, stream = harness.events(PROMPT, session_id=None)
    assert [event.type for event in events] == [
        "step_started",
        "text_delta",
        "text",
        "step_finished",
        "done",
    ]
    assert isinstance(events[0], StepStarted) and isinstance(events[2], Text)
    final = events[-1]
    assert isinstance(final, Done)
    assert final.session_id == SESSION
    assert final.usage.input == 4
    assert stream.session_id == SESSION
    sent = harness.transport.requests[0]
    assert sent.headers["Accept"] == "text/event-stream"


def test_stream_result_collects_the_text_and_steps(make: Callable[..., Harness]) -> None:
    body = sse(
        ("step_started", {"index": 1}),
        ("text", {"text": "listo"}),
        ("step_finished", {"index": 1, "usage": {"input": 2}}),
        ("done", {"session_id": SESSION}),
    )
    result = make(Scripted(body=body)).result_of_stream("hola")
    assert (result.text, result.steps, result.session_id) == ("listo", 1, SESSION)


def test_stream_keeps_the_limit_reasons_of_the_service(make: Callable[..., Harness]) -> None:
    body = sse(
        ("step_started", {"index": 1}),
        ("agent_failed", {"reason": "timeout", "detail_code": None, "session_id": SESSION}),
    )
    events, _ = make(Scripted(body=body)).events("hola")
    failed = events[-1]
    assert isinstance(failed, AgentFailed)
    assert (failed.reason, failed.session_id) == ("timeout", SESSION)
    with pytest.raises(AgentException) as raised:
        make(Scripted(body=body)).result_of_stream("hola")
    assert raised.value.reason == "timeout"


def test_stream_without_a_terminal_event_is_a_protocol_error(make: Callable[..., Harness]) -> None:
    body = sse(("step_started", {"index": 1}))
    with pytest.raises(AgentException) as raised:
        make(Scripted(body=body)).result_of_stream("hola")
    assert raised.value.reason == "protocol_error"


def test_stream_http_errors_raise_when_opened(make: Callable[..., Harness]) -> None:
    harness = make(Scripted(status=429))
    with pytest.raises(AgentException) as raised:
        harness.events("hola")
    assert raised.value.reason == "busy"


def test_an_empty_prompt_is_rejected_before_any_request(make: Callable[..., Harness]) -> None:
    harness = make()
    with pytest.raises(InvalidArgumentException):
        harness.call("invoke", "")
    assert harness.transport.requests == []


def test_a_raw_app_cannot_be_invoked_or_streamed(make: Callable[..., Harness]) -> None:
    harness = make(raw=True)
    with pytest.raises(UnimplementedError):
        harness.call("invoke", "hola")
    with pytest.raises(UnimplementedError):
        harness.deployment.stream("hola")
    assert harness.transport.requests == []
    assert harness.deployment.url.startswith("https://")


@pytest.mark.parametrize(
    ("scripted", "state", "ready", "busy"),
    [
        (Scripted(body=b'{"status":"Healthy","time_of_last_update":1}'), "healthy", True, False),
        (Scripted(body=b'{"status":"HealthyBusy"}'), "busy", True, True),
        (Scripted(status=503, body=b"{}"), "unavailable", False, False),
        (Scripted(body=b"no es json"), "unavailable", False, False),
        (SandboxException("sin conexión"), "unavailable", False, False),
    ],
)
def test_status_reads_ping(
    make: Callable[..., Harness],
    scripted: Scripted | Exception,
    state: str,
    ready: bool,
    busy: bool,
) -> None:
    harness = make(scripted)
    status = harness.call("status")
    assert (status.state, status.ready, status.busy) == (state, ready, busy)
    assert (status.port, status.project) == (AGENT_SERVICE_PORT, "support-bot")
    assert harness.transport.requests[0].path == "/ping"


def test_stop_kills_the_sandbox(make: Callable[..., Harness]) -> None:
    harness = make()
    harness.call("stop")
    assert harness.sandbox.kills == 1


def test_the_access_token_of_the_sandbox_is_exposed_to_reconnect(
    make: Callable[..., Harness],
) -> None:
    harness = make()
    assert harness.deployment.access_token == harness.sandbox.access_token


def test_pause_and_resume_map_to_the_sandbox(make: Callable[..., Harness]) -> None:
    harness = make()
    assert harness.call("pause") is True
    assert harness.sandbox.pause_calls == [{"wait": True}]
    harness.call("pause", wait=False)
    harness.call("resume")
    harness.call("resume", wait=False)
    assert harness.sandbox.pause_calls[1] == {"wait": False}
    assert harness.sandbox.resume_calls == [{"wait": True}, {"wait": False}]
    assert harness.sandbox.kills == 0


def test_the_result_says_when_the_agent_reported_no_usage(make: Callable[..., Harness]) -> None:
    body = {**RESULT_BODY, "usage": {"input": 0, "output": 0}, "usage_reported": False}
    result = make(Scripted(body=json_body(body))).call("invoke", PROMPT)
    assert result.usage_reported is False
    assert result.usage.total == 0


def test_an_older_service_without_the_field_reports_usage(make: Callable[..., Harness]) -> None:
    result = make(Scripted(body=json_body(RESULT_BODY))).call("invoke", PROMPT)
    assert result.usage_reported is True


def test_a_stream_without_usage_in_its_steps_is_unreported(make: Callable[..., Harness]) -> None:
    body = sse(
        ("step_started", {"index": 1}),
        ("text", {"text": "listo"}),
        ("step_finished", {"index": 1, "usage": None}),
        ("done", {"session_id": SESSION}),
    )
    harness = make(Scripted(body=body))
    result = harness.result_of_stream("hola")
    assert result.usage_reported is False
    reported = make(
        Scripted(
            body=sse(
                ("step_started", {"index": 1}),
                ("step_finished", {"index": 1, "usage": {"input": 3}}),
                ("done", {"session_id": SESSION}),
            )
        )
    ).result_of_stream("hola")
    assert reported.usage_reported is True


def test_labels_round_trip() -> None:
    labels = labels_for("support-bot", 8088, raw=False)
    assert labels == {AGENT_DEPLOYMENT_LABEL: "support-bot", AGENT_DEPLOYMENT_PORT_LABEL: "8088"}
    assert labels_for("x", 9, raw=True)[AGENT_DEPLOYMENT_PROTOCOL_LABEL] == "raw"


def test_connect_rebuilds_the_client_from_the_labels(monkeypatch: pytest.MonkeyPatch) -> None:
    sandbox = FakeSandbox(metadata=labels_for("support-bot", 9100, raw=False))
    seen: list[tuple[str, dict[str, Any]]] = []

    def connect(sandbox_id: str, **kwargs: Any) -> FakeSandbox:
        seen.append((sandbox_id, kwargs))
        return sandbox

    monkeypatch.setattr("rayito.sandbox_sync.Sandbox.connect", connect)
    deployment = AgentDeployment.connect(SANDBOX_ID, region="us-east-1")
    assert seen == [(SANDBOX_ID, {"region": "us-east-1"})]
    assert deployment.port == 9100
    assert sandbox.host_ports == []
    assert deployment.headers["x-aws-proxy-port"] == "9100"


def test_async_connect_rebuilds_the_client_from_the_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = AsyncFakeSandbox(metadata=labels_for("support-bot", 9100, raw=True))

    async def connect(sandbox_id: str, **kwargs: Any) -> AsyncFakeSandbox:
        return sandbox

    monkeypatch.setattr("rayito.sandbox_async.AsyncSandbox.connect", connect)
    deployment = asyncio.run(AsyncAgentDeployment.connect(SANDBOX_ID))
    assert deployment.port == 9100
    with pytest.raises(UnimplementedError):
        asyncio.run(deployment.invoke("hola"))


@pytest.mark.parametrize("metadata", [{}, {AGENT_DEPLOYMENT_LABEL: "x"}, {"otra": "cosa"}])
def test_connect_rejects_a_sandbox_that_is_not_a_deployment(
    monkeypatch: pytest.MonkeyPatch, metadata: dict[str, str]
) -> None:
    monkeypatch.setattr(
        "rayito.sandbox_sync.Sandbox.connect",
        lambda sandbox_id, **kwargs: FakeSandbox(metadata=metadata),
    )
    with pytest.raises(InvalidArgumentException):
        AgentDeployment.connect(SANDBOX_ID)
