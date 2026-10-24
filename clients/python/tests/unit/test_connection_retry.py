"""Un RPC cuya conexión HTTP/2 no llegó a abrirse (`UNAVAILABLE ... timed
out before receiving SETTINGS frame`, visto en AWS justo tras `create()` y
`resume()`) se reintenta unas pocas veces: la petición nunca salió del
cliente, así que vale también para `Execute`, que no se reabre tras un corte.
Cualquier otro error sale al primer intento."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import grpc
import pytest

from rayito._transport import (
    CONNECT_RETRY_DELAYS_SECONDS,
    is_connection_not_established,
    retry_unestablished,
    retry_unestablished_async,
)
from rayito.sandbox_async.main import AsyncSandbox
from rayito.sandbox_sync.main import Sandbox

from .conftest import FakeRpcError

SETTINGS_TIMEOUT = FakeRpcError(
    grpc.StatusCode.UNAVAILABLE,
    details="failed to connect to all addresses",
    debug="Connection timed out before receiving SETTINGS frame",
)
PROXY_502 = FakeRpcError(
    grpc.StatusCode.UNAVAILABLE, details="Received http2 header with status: 502"
)


def flaky(failures: list[Exception], answer: Any) -> tuple[Any, list[int]]:
    calls: list[int] = []

    def call() -> Any:
        calls.append(1)
        if failures:
            raise failures.pop(0)
        return answer

    return call, calls


def test_the_budget_outlasts_a_handshake_closed_for_several_seconds() -> None:
    """En AWS real un VM recién arrancado cerró el handshake TLS
    (`Endpoint closing`) durante más de 2 s: el presupuesto cubre más que
    eso."""
    assert sum(CONNECT_RETRY_DELAYS_SECONDS) > 5.0
    assert len(CONNECT_RETRY_DELAYS_SECONDS) >= 5


def test_only_a_connection_that_never_opened_counts() -> None:
    assert is_connection_not_established(SETTINGS_TIMEOUT)
    assert not is_connection_not_established(PROXY_502)
    assert not is_connection_not_established(FakeRpcError(grpc.StatusCode.UNAVAILABLE))
    assert not is_connection_not_established(
        FakeRpcError(grpc.StatusCode.DEADLINE_EXCEEDED, debug="before receiving SETTINGS frame")
    )


def test_a_connection_that_never_opened_is_retried_with_the_bounded_delays() -> None:
    call, calls = flaky([SETTINGS_TIMEOUT, SETTINGS_TIMEOUT], "ok")
    slept: list[float] = []

    assert retry_unestablished(call, slept.append) == "ok"
    assert len(calls) == 3
    assert slept == list(CONNECT_RETRY_DELAYS_SECONDS[:2])


def test_the_retries_are_bounded() -> None:
    call, calls = flaky([SETTINGS_TIMEOUT] * 10, "ok")

    with pytest.raises(FakeRpcError):
        retry_unestablished(call, lambda _: None)
    assert len(calls) == len(CONNECT_RETRY_DELAYS_SECONDS) + 1


def test_any_other_error_is_not_retried() -> None:
    call, calls = flaky([PROXY_502], "ok")

    with pytest.raises(FakeRpcError):
        retry_unestablished(call, lambda _: None)
    assert len(calls) == 1


def test_the_async_retry_has_the_same_policy() -> None:
    attempts: list[int] = []

    async def call() -> str:
        attempts.append(1)
        if len(attempts) < 3:
            raise SETTINGS_TIMEOUT
        return "ok"

    async def no_wait(_: float) -> None:
        return None

    assert asyncio.run(retry_unestablished_async(call, no_wait)) == "ok"
    assert len(attempts) == 3


def test_a_stream_open_retries_a_connection_that_never_opened(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """También `Execute` (`reconnect=False`): su primer mensaje pasa por
    `_first_message_reminting`."""
    monkeypatch.setattr("rayito._transport.time.sleep", lambda _: None)
    failures: list[Exception] = [SETTINGS_TIMEOUT] * len(CONNECT_RETRY_DELAYS_SECONDS)

    def start(_: Any) -> Any:
        if failures:
            raise failures.pop(0)
        return iter(["first"])

    fake = SimpleNamespace(_logger=None, _refresher=None, sandbox_id="mvm-1")
    opened = Sandbox._first_message_reminting(fake, start, object(), False)  # type: ignore[arg-type]

    assert "first" in opened


def test_an_async_unary_retries_a_connection_that_never_opened(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_wait(_: float) -> None:
        return None

    monkeypatch.setattr("rayito._transport.asyncio.sleep", no_wait)
    attempts: list[int] = []

    async def call() -> str:
        attempts.append(1)
        if len(attempts) == 1:
            raise SETTINGS_TIMEOUT
        return "ok"

    fake = SimpleNamespace(_logger=None, _refresher=None, sandbox_id="mvm-1")
    assert asyncio.run(AsyncSandbox._call_unary_once(fake, call)) == "ok"  # type: ignore[arg-type]
    assert len(attempts) == 2


def test_the_health_probe_never_waits_for_a_connection_that_does_not_open() -> None:
    """La sonda de `Health` de la readiness tiene su propio bucle y plazo:
    `retry_connect=False` la deja intentar una sola vez."""
    call, calls = flaky([SETTINGS_TIMEOUT], "ok")
    fake = SimpleNamespace(_logger=None, _refresher=None, sandbox_id="mvm-1")

    with pytest.raises(FakeRpcError):
        Sandbox._call_unary_once(fake, call, retry_connect=False)  # type: ignore[arg-type]
    assert len(calls) == 1
