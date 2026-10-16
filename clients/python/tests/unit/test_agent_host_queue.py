"""La cola acotada del servicio del host (`serve.py`, 0.14 D2): con `queue_max`
mayor que 0 una invocación por encima de `max_concurrency` espera su turno en
orden de llegada hasta `queue_timeout_seconds`, y sólo entonces (o con la cola
llena, o apagada, que es el valor por defecto) recibe el `429`. Contra un
socket real del loopback y un driver de doble. El host se prueba una vez,
aquí (`parity-checklist.md`)."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

import pytest

from rayito._agent._runner.agent_host import __main__ as host_main
from rayito._agent._runner.agent_host import serve
from rayito._limits import (
    AGENT_SERVICE_QUEUE_MAX,
    AGENT_SERVICE_QUEUE_MAX_DEFAULT,
    AGENT_SERVICE_QUEUE_TIMEOUT_SECONDS_DEFAULT,
    AGENT_SERVICE_QUEUE_TIMEOUT_SECONDS_MAX,
)
from tests.unit.agent_host_support import (
    FakeDriver,
    invoke,
    request,
    running_service,
    step,
    wait_for,
)

BOUNDED_WAIT_SECONDS = 10.0
SHORT_QUEUE_TIMEOUT_SECONDS = 0.3


def test_queue_constants_match_limits_json() -> None:
    assert serve.QUEUE_MAX_DEFAULT == AGENT_SERVICE_QUEUE_MAX_DEFAULT
    assert serve.QUEUE_MAX == AGENT_SERVICE_QUEUE_MAX
    assert serve.QUEUE_TIMEOUT_SECONDS_DEFAULT == AGENT_SERVICE_QUEUE_TIMEOUT_SECONDS_DEFAULT
    assert serve.QUEUE_TIMEOUT_SECONDS_MAX == AGENT_SERVICE_QUEUE_TIMEOUT_SECONDS_MAX


def test_the_queue_is_off_by_default() -> None:
    config = serve.ServiceConfig()
    assert config.queue_max == 0


def _in_background(call: Callable[[], Any], results: list[Any]) -> threading.Thread:
    thread = threading.Thread(target=lambda: results.append(call()), daemon=True)
    thread.start()
    return thread


def _waiting_count(service: serve.Service, expected: int) -> bool:
    return wait_for(lambda: len(service._waiting) == expected)


def _queued_prompt(port: int, position: int, results: list[Any]) -> threading.Thread:
    return _in_background(lambda: invoke(port, f"p{position}"), results)


def test_a_queued_invocation_runs_when_the_slot_frees_up() -> None:
    gate = threading.Event()
    driver = FakeDriver(step(1, text="hecho"), gate=gate)
    config = serve.ServiceConfig(queue_max=1, queue_timeout_seconds=BOUNDED_WAIT_SECONDS)
    with running_service(driver, config=config) as (service, port):
        first: list[Any] = []
        second: list[Any] = []
        running = _in_background(lambda: invoke(port, "uno"), first)
        assert driver.started.wait(BOUNDED_WAIT_SECONDS)
        queued = _in_background(lambda: invoke(port, "dos"), second)
        assert wait_for(lambda: len(service._waiting) == 1)
        gate.set()
        running.join(BOUNDED_WAIT_SECONDS)
        queued.join(BOUNDED_WAIT_SECONDS)
    assert [status for status, _, _ in (*first, *second)] == [200, 200]
    assert [item.prompt for item in driver.requests] == ["uno", "dos"]


def test_a_full_queue_answers_429_and_the_waiter_is_not_lost() -> None:
    gate = threading.Event()
    driver = FakeDriver(step(1, text="hecho"), gate=gate)
    config = serve.ServiceConfig(queue_max=1, queue_timeout_seconds=BOUNDED_WAIT_SECONDS)
    with running_service(driver, config=config) as (service, port):
        outcomes: list[Any] = []
        running = _in_background(lambda: invoke(port, "uno"), outcomes)
        assert driver.started.wait(BOUNDED_WAIT_SECONDS)
        queued = _in_background(lambda: invoke(port, "dos"), outcomes)
        assert wait_for(lambda: len(service._waiting) == 1)
        status, headers, body = invoke(port, "tres")
        assert (status, body) == (429, {"error": "busy"})
        assert headers["retry-after"] == str(serve.RETRY_AFTER_SECONDS)
        gate.set()
        running.join(BOUNDED_WAIT_SECONDS)
        queued.join(BOUNDED_WAIT_SECONDS)
    assert sorted(status for status, _, _ in outcomes) == [200, 200]


def test_a_queued_invocation_times_out_with_429() -> None:
    gate = threading.Event()
    driver = FakeDriver(step(1, text="hecho"), gate=gate)
    config = serve.ServiceConfig(queue_max=2, queue_timeout_seconds=SHORT_QUEUE_TIMEOUT_SECONDS)
    with running_service(driver, config=config) as (service, port):
        running = _in_background(lambda: invoke(port, "uno"), [])
        assert driver.started.wait(BOUNDED_WAIT_SECONDS)
        status, headers, body = invoke(port, "dos")
        assert (status, body) == (429, {"error": "queue_timeout"})
        assert "retry-after" in headers
        assert not service._waiting
        gate.set()
        running.join(BOUNDED_WAIT_SECONDS)


def test_the_queue_is_fifo_and_a_new_request_does_not_jump_it() -> None:
    gate = threading.Event()
    driver = FakeDriver(step(1, text="hecho"), gate=gate)
    config = serve.ServiceConfig(queue_max=3, queue_timeout_seconds=BOUNDED_WAIT_SECONDS)
    with running_service(driver, config=config) as (service, port):
        outcomes: list[Any] = []
        threads = [_in_background(lambda: invoke(port, "p0"), outcomes)]
        assert driver.started.wait(BOUNDED_WAIT_SECONDS)
        for position in (1, 2, 3):
            threads.append(_queued_prompt(port, position, outcomes))
            assert _waiting_count(service, position)
        gate.set()
        for thread in threads:
            thread.join(BOUNDED_WAIT_SECONDS)
    assert [item.prompt for item in driver.requests] == ["p0", "p1", "p2", "p3"]


def test_ping_stays_busy_while_requests_wait() -> None:
    gate = threading.Event()
    driver = FakeDriver(step(1, text="hecho"), gate=gate)
    config = serve.ServiceConfig(queue_max=1, queue_timeout_seconds=BOUNDED_WAIT_SECONDS)
    with running_service(driver, config=config) as (service, port):
        running = _in_background(lambda: invoke(port, "uno"), [])
        assert driver.started.wait(BOUNDED_WAIT_SECONDS)
        queued = _in_background(lambda: invoke(port, "dos"), [])
        assert wait_for(lambda: len(service._waiting) == 1)
        status, _, body = request(port, "GET", "/ping")
        assert status == 200
        assert b"HealthyBusy" in body
        gate.set()
        running.join(BOUNDED_WAIT_SECONDS)
        queued.join(BOUNDED_WAIT_SECONDS)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, 0),
        (-1, 0),
        ("8", 0),
        (True, 0),
        (4, 4),
        (AGENT_SERVICE_QUEUE_MAX + 100, AGENT_SERVICE_QUEUE_MAX),
    ],
)
def test_the_host_clamps_an_invalid_queue_size(value: object, expected: int) -> None:
    bounded = host_main._bounded(value, serve.QUEUE_MAX_DEFAULT, serve.QUEUE_MAX)
    assert bounded == expected
