"""Soporte de las pruebas del host del agente (`_runner/agent_host`):
un driver de doble y un servidor real en un puerto efímero del loopback."""

from __future__ import annotations

import http.client
import json
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from typing import Any

from rayito._agent._runner.agent_host import serve
from rayito._agent._runner.agent_host.limits import Limits
from rayito._agent._runner.agent_host.protocol import (
    AgentFailure,
    Emit,
    Request,
    RunState,
    build_event,
)

LOOPBACK = "127.0.0.1"
CONNECT_TIMEOUT_SECONDS = 10.0
POLL_SECONDS = 0.02


class FakeDriver:
    """Un driver que emite una secuencia fija de eventos de progreso,
    opcionalmente esperando a un `threading.Event` entre dos de ellos."""

    def __init__(
        self,
        events: Sequence[dict[str, Any]] = (),
        *,
        gate: threading.Event | None = None,
        gate_after: int = 0,
        failure: AgentFailure | None = None,
        session_id: str = "rda_" + "0" * 32,
    ) -> None:
        self.events = list(events)
        self.gate = gate
        self.gate_after = gate_after
        self.failure = failure
        self.session_id = session_id
        self.requests: list[Request] = []
        self.started = threading.Event()
        self.cancelled = threading.Event()
        self.warmed = False

    def warm(self) -> None:
        self.warmed = True

    def cancel(self) -> None:
        self.cancelled.set()
        if self.gate is not None:
            self.gate.set()

    def run(self, request: Request, emit: Emit, state: RunState) -> None:
        self.requests.append(request)
        state.session_id = request.session_id or self.session_id
        emit(build_event("session", session_id=state.session_id))
        self.started.set()
        for index, event in enumerate(self.events):
            if self.gate is not None and index == self.gate_after:
                self.gate.wait(CONNECT_TIMEOUT_SECONDS)
            emit({"v": 1, **event})
        if self.failure is not None:
            raise self.failure


def step(index: int, *, tokens: int = 0, text: str | None = None) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = [{"type": "step_started", "index": index}]
    if text is not None:
        events.append({"type": "text", "text": text})
    usage = {"input": tokens, "output": 0, "reasoning": 0, "cache_read": 0, "cache_write": 0}
    events.append(
        {"type": "step_finished", "index": index, "usage": usage, "finish_reason": "stop"}
    )
    return events


@contextmanager
def running_service(
    driver: Any,
    *,
    config: serve.ServiceConfig | None = None,
    ready: bool = True,
    clock: Callable[[], float] | None = None,
) -> Iterator[tuple[serve.Service, int]]:
    service = serve.Service(driver, config or serve.ServiceConfig(), *([clock] if clock else []))
    server = serve.build_server(service, 0, LOOPBACK)
    thread = threading.Thread(
        target=lambda: server.serve_forever(poll_interval=POLL_SECONDS), daemon=True
    )
    thread.start()
    if ready:
        service.mark_ready()
    try:
        yield service, server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(CONNECT_TIMEOUT_SECONDS)


def request(
    port: int,
    method: str,
    path: str,
    body: object | None = None,
    headers: dict[str, str] | None = None,
    *,
    raw_body: bytes | None = None,
) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection(LOOPBACK, port, timeout=CONNECT_TIMEOUT_SECONDS)
    payload = (
        raw_body if raw_body is not None else (None if body is None else json.dumps(body).encode())
    )
    send = dict(headers or {})
    if payload is not None:
        send.setdefault("Content-Type", "application/json")
    connection.request(method, path, body=payload, headers=send)
    response = connection.getresponse()
    data = response.read()
    result = (response.status, {k.lower(): v for k, v in response.getheaders()}, data)
    connection.close()
    return result


def invoke(
    port: int,
    prompt: str = "hola",
    *,
    headers: dict[str, str] | None = None,
    **fields: object,
) -> tuple[int, dict[str, str], Any]:
    status, response_headers, data = request(
        port, "POST", "/invocations", {"prompt": prompt, **fields}, headers
    )
    parsed: Any = json.loads(data) if data else None
    return status, response_headers, parsed


def sse_messages(data: bytes) -> list[tuple[str, dict[str, Any]]]:
    messages: list[tuple[str, dict[str, Any]]] = []
    for block in data.decode().split("\n\n"):
        if not block.strip():
            continue
        fields = dict(line.split(": ", 1) for line in block.splitlines())
        messages.append((fields["event"], json.loads(fields["data"])))
    return messages


def wait_for(condition: Callable[[], bool], timeout: float = CONNECT_TIMEOUT_SECONDS) -> bool:
    done = threading.Event()
    deadline = threading.Timer(timeout, done.set)
    deadline.start()
    try:
        while not done.is_set():
            if condition():
                return True
            done.wait(0.01)
        return condition()
    finally:
        deadline.cancel()


def tiny_limits(**overrides: Any) -> Limits:
    return Limits(**{"timeout_seconds": 5.0, **overrides})
