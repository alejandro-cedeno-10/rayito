"""`rayito.agent-service/1` del host del agente (`_runner/agent_host/serve.py`)
contra un socket real del loopback y un driver de doble: `/ping`, JSON y SSE,
los códigos del contrato (400, 401, 413, 429, 503), la cabecera de sesión de
AgentCore, el token de un contenedor, los topes de `AgentLimits` y los logs
sin contenido. El host se prueba una vez, aquí (`parity-checklist.md`)."""

from __future__ import annotations

import hmac
import http.client
import json
import logging
import threading
from pathlib import Path
from typing import Any

import pytest

from rayito._agent._runner.agent_host import serve
from rayito._agent._runner.agent_host.limits import Limits
from rayito._agent._runner.agent_host.protocol import AgentFailure, run_to_terminal
from rayito._limits import (
    AGENT_SERVICE_HEALTH_PATH,
    AGENT_SERVICE_INVOKE_PATH,
    AGENT_SERVICE_MAX_CONCURRENCY,
    AGENT_SERVICE_MAX_REQUEST_BYTES,
    AGENT_SERVICE_SCHEMA,
    AGENT_SERVICE_SESSION_HEADER,
)
from tests.unit.agent_host_support import (
    FakeDriver,
    invoke,
    request,
    running_service,
    sse_messages,
    step,
    tiny_limits,
    wait_for,
)

SESSION = "rda_" + "0" * 32
WARM_UP_SECONDS = 0.3


def test_service_constants_match_limits_json() -> None:
    assert serve.HEALTH_PATH == AGENT_SERVICE_HEALTH_PATH
    assert serve.INVOKE_PATH == AGENT_SERVICE_INVOKE_PATH
    assert serve.SESSION_HEADER == AGENT_SERVICE_SESSION_HEADER
    assert serve.MAX_REQUEST_BYTES == AGENT_SERVICE_MAX_REQUEST_BYTES
    assert serve.MAX_CONCURRENCY == AGENT_SERVICE_MAX_CONCURRENCY
    assert serve.SERVICE_SCHEMA == AGENT_SERVICE_SCHEMA


def test_ping_is_healthy_and_time_changes_only_with_the_status() -> None:
    gate = threading.Event()
    driver = FakeDriver(step(1, text="listo"), gate=gate)
    ticks = iter(range(1000, 1100))
    with running_service(driver, clock=lambda: float(next(ticks))) as (_, port):
        first = json.loads(request(port, "GET", "/ping")[2])
        second = json.loads(request(port, "GET", "/ping")[2])
        assert first == second
        assert first["status"] == "Healthy"
        caller = threading.Thread(target=lambda: invoke(port))
        caller.start()
        assert driver.started.wait(5)
        busy = json.loads(request(port, "GET", "/ping")[2])
        assert busy["status"] == "HealthyBusy"
        assert busy["time_of_last_update"] != first["time_of_last_update"]
        gate.set()
        caller.join(5)
        assert wait_for(lambda: json.loads(request(port, "GET", "/ping")[2])["status"] == "Healthy")
        after = json.loads(request(port, "GET", "/ping")[2])
        assert after["time_of_last_update"] not in (
            first["time_of_last_update"],
            busy["time_of_last_update"],
        )


def test_invocation_returns_the_json_result() -> None:
    driver = FakeDriver([*step(1, tokens=7), *step(2, tokens=3, text="respuesta")])
    with running_service(driver) as (_, port):
        status, headers, body = invoke(port, "hola", model="m", reasoning=True)
    assert status == 200
    assert headers["content-type"] == "application/json"
    assert body == {
        "v": 1,
        "text": "respuesta",
        "session_id": SESSION,
        "usage": {"input": 10, "output": 0, "reasoning": 0, "cache_read": 0, "cache_write": 0},
        "usage_reported": True,
        "steps": 2,
        "failed": None,
    }
    (seen,) = driver.requests
    assert (seen.prompt, seen.model, seen.reasoning) == ("hola", "m", True)


def test_a_failing_driver_is_a_failed_result_not_an_http_error() -> None:
    driver = FakeDriver(failure=AgentFailure("model_error", "ThrottlingException"))
    with running_service(driver) as (_, port):
        status, _, body = invoke(port)
    assert status == 200
    assert body["text"] == ""
    assert body["failed"] == {"reason": "model_error", "detail_code": "ThrottlingException"}
    assert body["session_id"] == SESSION


def test_streaming_emits_one_sse_message_per_event_and_ends_in_done() -> None:
    driver = FakeDriver(step(1, text="hola"))
    with running_service(driver) as (_, port):
        status, headers, data = request(
            port,
            "POST",
            "/invocations",
            {"prompt": "x"},
            {"Accept": "text/event-stream"},
        )
    assert status == 200
    assert headers["content-type"] == "text/event-stream"
    messages = sse_messages(data)
    assert [kind for kind, _ in messages] == [
        "session",
        "step_started",
        "text",
        "step_finished",
        "done",
    ]
    assert messages[-1][1] == {"v": 1, "type": "done", "session_id": SESSION}


def test_streaming_ends_in_agent_failed_when_the_driver_fails() -> None:
    driver = FakeDriver(failure=AgentFailure("runtime_error", "KeyError"))
    with running_service(driver) as (_, port):
        _, _, data = request(
            port, "POST", "/invocations", {"prompt": "x"}, {"Accept": "text/event-stream"}
        )
    kind, payload = sse_messages(data)[-1]
    assert kind == "agent_failed"
    assert payload["reason"] == "runtime_error"


@pytest.mark.parametrize(
    "raw",
    [b"no es json", b"[]", b'{"prompt": 3}', b'{"prompt": ""}', b'{"prompt":"x","extra":1}'],
)
def test_invalid_bodies_are_400(raw: bytes) -> None:
    with running_service(FakeDriver()) as (_, port):
        status, _, data = request(port, "POST", "/invocations", raw_body=raw)
    assert status == 400
    assert b"invalid_request" in data


def test_a_body_over_the_limit_is_413_before_reading_it() -> None:
    config = serve.ServiceConfig(max_request_bytes=16)
    with running_service(FakeDriver(), config=config) as (_, port):
        status, _, _ = request(port, "POST", "/invocations", {"prompt": "x" * 100})
    assert status == 413


def test_a_second_invocation_gets_429_with_retry_after() -> None:
    gate = threading.Event()
    driver = FakeDriver(step(1, text="a"), gate=gate)
    with running_service(driver) as (_, port):
        first = threading.Thread(target=lambda: invoke(port))
        first.start()
        assert driver.started.wait(5)
        status, headers, _ = invoke(port)
        assert status == 429
        assert headers["retry-after"] == str(serve.RETRY_AFTER_SECONDS)
        gate.set()
        first.join(5)
        assert invoke(port)[0] == 200


#: Lo que tarda en soltarse el cupo en las pruebas de orden: un manejador que
#: lo suelta después de escribir la respuesta deja al cliente ver el `429`.
SLOW_RELEASE_SECONDS = 0.3
ACCEPTS = [{}, {"Accept": "text/event-stream"}]


def _last_kind(headers: dict[str, str], data: bytes) -> str:
    if headers.get("content-type") == "text/event-stream":
        return sse_messages(data)[-1][0]
    body = json.loads(data)
    return "agent_failed" if body.get("failed") else "done"


def _call_until_terminal(port: int, accept: dict[str, str]) -> tuple[int, str]:
    """Una invocación leída como lo hace un cliente: el JSON por su
    `Content-Length` y el SSE hasta el mensaje terminal, sin esperar al cierre
    del socket."""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request(
            "POST",
            "/invocations",
            body=b'{"prompt":"x"}',
            headers={"Content-Type": "application/json", **accept},
        )
        response = connection.getresponse()
        if response.getheader("Content-Type") != serve.EVENT_STREAM:
            return response.status, _last_kind({}, response.read())
        kind = ""
        while kind not in ("done", "agent_failed"):
            line = response.fp.readline().decode()
            if not line:
                break
            if line.startswith("event: "):
                kind = line.removeprefix("event: ").strip()
        return response.status, kind
    finally:
        connection.close()


@pytest.mark.parametrize("accept", ACCEPTS)
def test_the_slot_is_free_once_the_client_has_the_response(
    monkeypatch: pytest.MonkeyPatch, accept: dict[str, str]
) -> None:
    release = serve.Service.release

    def slow_release(self: serve.Service) -> None:
        threading.Event().wait(SLOW_RELEASE_SECONDS)
        release(self)

    monkeypatch.setattr(serve.Service, "release", slow_release)
    with running_service(FakeDriver(step(1, text="a"))) as (_, port):
        for _ in range(2):
            assert _call_until_terminal(port, accept) == (200, "done")
        assert json.loads(request(port, "GET", "/ping")[2])["status"] == "Healthy"


@pytest.mark.parametrize("accept", ACCEPTS)
def test_a_finished_run_is_not_cancelled_while_its_thread_exits(
    monkeypatch: pytest.MonkeyPatch, accept: dict[str, str]
) -> None:
    exiting = threading.Event()

    def lingering(*args: Any) -> None:
        run_to_terminal(*args)
        exiting.wait(5)

    monkeypatch.setattr(serve, "run_to_terminal", lingering)
    driver = FakeDriver(step(1, text="a"))
    try:
        with running_service(driver) as (_, port):
            status, headers, data = request(port, "POST", "/invocations", {"prompt": "x"}, accept)
            assert (status, _last_kind(headers, data)) == (200, "done")
            assert not driver.cancelled.is_set()
    finally:
        exiting.set()


def test_not_ready_is_503_and_ping_still_answers() -> None:
    config = serve.ServiceConfig(ready_wait_seconds=0.0)
    with running_service(FakeDriver(), config=config, ready=False) as (service, port):
        status, headers, _ = invoke(port)
        assert status == 503
        assert "retry-after" in headers
        assert request(port, "GET", "/ping")[0] == 200
        service.mark_ready()
        assert invoke(port)[0] == 200


def test_a_call_that_arrives_while_the_host_warms_up_waits_instead_of_failing() -> None:
    with running_service(FakeDriver(), ready=False) as (service, port):
        warming = threading.Timer(WARM_UP_SECONDS, service.mark_ready)
        warming.start()
        status, _, body = invoke(port)
        warming.join()
        assert status == 200
        assert "text" in body


def test_a_call_that_waits_for_a_failed_warm_up_gets_503_at_once() -> None:
    with running_service(FakeDriver(), ready=False) as (service, port):
        failing = threading.Timer(WARM_UP_SECONDS, lambda: service.mark_failed("warm_failed"))
        failing.start()
        status, headers, _ = invoke(port)
        failing.join()
        assert status == 503
        assert "retry-after" in headers


def test_the_session_header_is_the_fallback() -> None:
    driver = FakeDriver()
    with running_service(driver) as (_, port):
        invoke(port, headers={AGENT_SERVICE_SESSION_HEADER: "rda_" + "a" * 32})
        invoke(port, session_id="rda_" + "b" * 32, headers={AGENT_SERVICE_SESSION_HEADER: "x"})
    assert [r.session_id for r in driver.requests] == ["rda_" + "a" * 32, "rda_" + "b" * 32]


def test_the_bearer_token_is_required_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    compared: list[tuple[bytes, bytes]] = []
    real = hmac.compare_digest

    def spy(left: bytes, right: bytes) -> bool:
        compared.append((left, right))
        return real(left, right)

    monkeypatch.setattr(hmac, "compare_digest", spy)
    config = serve.ServiceConfig(token="s3cr3t")
    with running_service(FakeDriver(), config=config) as (_, port):
        assert invoke(port)[0] == 401
        assert invoke(port, headers={"Authorization": "Bearer otro"})[0] == 401
        assert invoke(port, headers={"Authorization": "Basic s3cr3t"})[0] == 401
        assert invoke(port, headers={"Authorization": "Bearer s3cr3t"})[0] == 200
        assert request(port, "GET", "/ping")[0] == 200
    assert compared


def test_max_steps_cuts_the_run_at_step_n_plus_one() -> None:
    driver = FakeDriver([*step(1), *step(2), *step(3)])
    config = serve.ServiceConfig(limits=tiny_limits(max_steps=2))
    with running_service(driver, config=config) as (_, port):
        status, _, body = invoke(port)
    assert status == 200
    assert body["failed"]["reason"] == "max_steps"
    assert body["steps"] == 3


def test_the_token_budget_is_checked_after_each_step() -> None:
    driver = FakeDriver([*step(1, tokens=60), *step(2, tokens=60), *step(3, tokens=1)])
    config = serve.ServiceConfig(limits=tiny_limits(max_total_tokens=100))
    with running_service(driver, config=config) as (_, port):
        _, _, body = invoke(port)
    assert body["failed"]["reason"] == "token_budget"
    assert body["usage"]["input"] == 120


def test_the_output_limit_counts_delivered_bytes() -> None:
    driver = FakeDriver([{"type": "text_delta", "text": "x" * 200}] * 5)
    config = serve.ServiceConfig(limits=tiny_limits(max_output_bytes=300))
    with running_service(driver, config=config) as (_, port):
        _, _, body = invoke(port)
    assert body["failed"]["reason"] == "output_limit"


def test_the_timeout_aborts_the_driver() -> None:
    gate = threading.Event()
    driver = FakeDriver(step(1, text="nunca"), gate=gate)
    config = serve.ServiceConfig(limits=Limits(timeout_seconds=0.3))
    with running_service(driver, config=config) as (_, port):
        _, _, body = invoke(port)
        assert body["failed"]["reason"] == "timeout"
        assert driver.cancelled.wait(5)
        assert invoke(port)[0] == 200


def test_a_client_that_leaves_cancels_the_run() -> None:
    gate = threading.Event()
    driver = FakeDriver(step(1, text="x"), gate=gate)
    with running_service(driver) as (_, port):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        connection.request(
            "POST",
            "/invocations",
            body=b'{"prompt":"x"}',
            headers={"Accept": "text/event-stream", "Content-Type": "application/json"},
        )
        response = connection.getresponse()
        response.fp.readline()
        assert driver.started.wait(5)
        connection.close()
        gate.set()
        assert wait_for(lambda: request(port, "GET", "/ping")[2].count(b"Healthy") == 1)


def test_logs_never_carry_the_prompt_headers_or_the_token(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger=serve.LOGGER_NAME)
    config = serve.ServiceConfig(token="token-secreto")
    with running_service(FakeDriver(step(1, text="respuesta secreta")), config=config) as (_, port):
        invoke(
            port,
            "prompt secreto",
            headers={"Authorization": "Bearer token-secreto", "X-Otra": "valor-de-cabecera"},
        )
        invoke(port, "x", headers={"Authorization": "Bearer mal"})
        request(port, "POST", "/invocations", raw_body=b"{basura secreta")
        request(port, "GET", "/ping?token=en-la-consulta")
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "POST /invocations -> 200" in logged
    for secret in (
        "prompt secreto",
        "respuesta secreta",
        "token-secreto",
        "valor-de-cabecera",
        "basura secreta",
        "en-la-consulta",
    ):
        assert secret not in logged


def test_unknown_paths_and_methods_are_404() -> None:
    with running_service(FakeDriver()) as (_, port):
        assert request(port, "GET", "/otra")[0] == 404
        assert request(port, "POST", "/otra", {"prompt": "x"})[0] == 404
        assert request(port, "PUT", "/invocations", {"prompt": "x"})[0] == 404


def test_the_ready_marker_is_written_when_the_service_is_ready(tmp_path: Path) -> None:
    marker = tmp_path / "host" / "ready"
    config = serve.ServiceConfig(ready_marker=str(marker))
    with running_service(FakeDriver(), config=config, ready=False) as (service, _):
        assert not marker.exists()
        service.mark_ready()
        assert marker.read_text() == "ready\n"


def test_a_client_that_announces_a_body_and_never_sends_it_is_dropped() -> None:
    import socket

    config = serve.ServiceConfig(io_timeout_seconds=0.3)
    with running_service(FakeDriver(), config=config) as (_, port):
        with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
            client.sendall(b"POST /invocations HTTP/1.1\r\nHost: x\r\nContent-Length: 100\r\n\r\n{")
            assert wait_for(lambda: client.recv(1) == b"", 5)
        assert request(port, "GET", "/ping")[0] == 200


def test_a_malformed_request_line_is_400_and_logs_no_traceback(
    caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture[str]
) -> None:
    import socket

    caplog.set_level(logging.DEBUG, logger=serve.LOGGER_NAME)
    with running_service(FakeDriver()) as (_, port):
        with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
            client.sendall(b"GARBAGE\r\n\r\n")
            assert _read_until_closed(client).startswith(b"HTTP/1.1 400")
        assert request(port, "GET", "/ping")[0] == 200
    logged = "\n".join(record.getMessage() for record in caplog.records)
    stderr = capfd.readouterr().err
    assert "-> 400" in logged
    for text in (logged, stderr):
        assert "AttributeError" not in text
        assert "Traceback" not in text


def _read_until_closed(client: Any) -> bytes:
    """Toda la respuesta hasta que el servidor cierra: cerrar antes con datos
    sin leer haría que el kernel mande un RST a media escritura."""
    received = b""
    while chunk := client.recv(4096):
        received += chunk
    return received


def test_a_client_that_hangs_up_mid_response_logs_no_traceback(
    capfd: pytest.CaptureFixture[str],
) -> None:
    import socket

    with running_service(FakeDriver()) as (service, _):
        server = serve.build_server(service, 0, "127.0.0.1")
        with socket.socket() as hung_up:
            try:
                raise ConnectionResetError
            except ConnectionResetError:
                server.handle_error(hung_up, ("127.0.0.1", 1))
            finally:
                server.server_close()

    assert "Traceback" not in capfd.readouterr().err


class _RawDriver(FakeDriver):
    raw = True

    def forward(self, *_: object) -> object:
        raise AssertionError("an invalid body must be rejected before forwarding")


@pytest.mark.parametrize(
    ("length", "status"),
    [("-1", 400), ("abc", 400), (str(AGENT_SERVICE_MAX_REQUEST_BYTES + 1), 413)],
)
def test_raw_forwarding_rejects_bad_content_length_without_reading(
    length: str, status: int
) -> None:
    import socket

    with (
        running_service(_RawDriver()) as (_, port),
        socket.create_connection(("127.0.0.1", port), timeout=5) as client,
    ):
        client.sendall(
            f"POST /mcp HTTP/1.1\r\nHost: x\r\nContent-Length: {length}\r\n\r\n".encode()
        )
        assert client.recv(64).startswith(f"HTTP/1.1 {status}".encode())
