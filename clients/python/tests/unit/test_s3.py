"""Tests puros de `ObjectFetch` (sin sandbox, sin `fake_rayd` ni S3 real):
la sincronización entre `run()` y `cancel()` en las tres franjas de la
carrera con `open_chunks`, secuenciada con `threading.Event` y nunca con
sleeps ni plazos de reloj (ver `docs/research/...` M9.4: la causa raíz de
`test_async_request_timeout_bounds_a_stalled_s3_download` era justo esta
carrera, con `open_chunks` retrasado más allá del plazo)."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from typing import Any, cast

import pytest

from rayito._s3 import ObjectFetch, S3Gateway
from rayito._transfer_base import StagingObject
from rayito.exceptions import TimeoutException

TARGET = StagingObject(bucket="b", key="k", region="us-east-1")


class FakeChunks:
    """Un `ObjectChunks` de mentira: itera una lista fija y cuenta sus
    cierres; nunca toca S3."""

    def __init__(self, items: list[bytes]) -> None:
        self._iter: Iterator[bytes] = iter(items)
        self.close_calls = 0

    def __iter__(self) -> FakeChunks:
        return self

    def __next__(self) -> bytes:
        return next(self._iter)

    def close(self) -> None:
        self.close_calls += 1


class BlockingChunks(FakeChunks):
    """Como `FakeChunks`, pero el primer `__next__` se bloquea hasta que
    `close()` lo libera (simula un hilo detenido leyendo el cuerpo)."""

    def __init__(self) -> None:
        super().__init__([])
        self.entered_next = threading.Event()
        self._unblocked = threading.Event()

    def __next__(self) -> bytes:
        self.entered_next.set()
        self._unblocked.wait(10)
        raise StopIteration

    def close(self) -> None:
        super().close()
        self._unblocked.set()


class FakeGateway:
    """Controla cuándo vuelve `open_chunks` (para forzar cada franja de la
    carrera) y cuenta cuántas veces se llamó (ningún `get_object` de más)."""

    def __init__(self, chunks: Any) -> None:
        self._chunks = chunks
        self.open_calls = 0
        self.entered_open = threading.Event()
        self.release_open = threading.Event()
        self.release_open.set()

    def open_chunks(self, target: StagingObject) -> Any:
        self.open_calls += 1
        self.entered_open.set()
        self.release_open.wait(10)
        return self._chunks

    def as_s3_gateway(self) -> S3Gateway:
        return cast(S3Gateway, self)


def test_cancel_before_run_never_calls_get_object() -> None:
    gateway = FakeGateway(FakeChunks([b"a"]))
    fetch = ObjectFetch(gateway.as_s3_gateway(), TARGET, None)
    fetch.cancel()
    with pytest.raises(TimeoutException):
        fetch.run()
    assert gateway.open_calls == 0


def test_cancel_while_open_chunks_in_flight_closes_the_late_body_once() -> None:
    chunks = FakeChunks([b"a"])
    gateway = FakeGateway(chunks)
    gateway.release_open.clear()
    fetch = ObjectFetch(gateway.as_s3_gateway(), TARGET, None)
    outcome: dict[str, BaseException] = {}

    def run_in_background() -> None:
        try:
            fetch.run()
        except BaseException as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=run_in_background)
    thread.start()
    assert gateway.entered_open.wait(5)
    fetch.cancel()
    gateway.release_open.set()
    thread.join(5)
    assert not thread.is_alive()
    assert isinstance(outcome.get("error"), TimeoutException)
    assert gateway.open_calls == 1
    assert chunks.close_calls == 1


def test_cancel_while_blocked_in_next_closes_the_body_at_once() -> None:
    chunks = BlockingChunks()
    gateway = FakeGateway(chunks)
    fetch = ObjectFetch(gateway.as_s3_gateway(), TARGET, None)
    thread = threading.Thread(target=fetch.run, daemon=True)
    thread.start()
    assert chunks.entered_next.wait(5)
    fetch.cancel()
    assert chunks.close_calls == 1
    thread.join(5)
    assert not thread.is_alive()


class ClosedBodyChunks(BlockingChunks):
    """Como `BlockingChunks`, pero leer tras el cierre falla, como un cuerpo
    HTTP real ya cerrado, y recuerda si el cierre llegó con el lector aún
    bloqueado (sólo la guardia de inactividad puede hacerlo: el `finally`
    de `run()` corre después de que `__next__` vuelva)."""

    def __init__(self) -> None:
        super().__init__()
        self.closed_while_blocked = False

    def __next__(self) -> bytes:
        self.entered_next.set()
        self.closed_while_blocked = self._unblocked.wait(10)
        raise ValueError("cuerpo cerrado")


def test_idle_guard_closes_a_body_blocked_in_next() -> None:
    chunks = ClosedBodyChunks()
    fetch = ObjectFetch(FakeGateway(chunks).as_s3_gateway(), TARGET, 0.05)
    with pytest.raises(TimeoutException, match="stream_idle_timeout"):
        fetch.run()
    assert chunks.closed_while_blocked
