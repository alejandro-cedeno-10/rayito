"""Ayuda compartida por `test_transfer_sync.py` y `test_transfer_async.py`
para una pata S3 colgada, sin duplicar `StalledS3` entre los dos árboles.

Dos puertas controlan cada franja de la carrera entre `cancel()` y
`ObjectFetch.run()` (M9.4, causa raíz reproducida retrasando `open_chunks`
más allá del plazo): `open_gate` retrasa `S3Gateway.open_chunks` (para que el
plazo venza mientras el `get_object` sigue en vuelo, el escenario real) y
`read_gate` retrasa cada trozo ya abierto (para `stream_idle_timeout`).
`opened`/`closed` cuentan cuántos cuerpos se abrieron y cuántos se llegaron a
cerrar (una sola vez cada uno, aunque `close()` real es idempotente y tanto
la guardia de inactividad como el `finally` de `run()` pueden llamarla) —el
invariante que debe sostenerse para cualquier interleaving es
`opened == closed`—, y `fetch_finished` se activa cuando `ObjectFetch.run`
termina, así los tests esperan con un `Event`, nunca con un sleep."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from typing import Any

import pytest

from rayito import _s3


class StalledS3:
    def __init__(self) -> None:
        self.released = threading.Event()
        self.open_gate = threading.Event()
        self.open_gate.set()
        self.read_gate = threading.Event()
        self.read_gate.set()
        self.opened = 0
        self.closed = 0
        self.fetch_finished = threading.Event()
        self._lock = threading.Lock()
        self._closed_ids: set[int] = set()

    def upload(self, *args: Any, **kwargs: Any) -> None:
        """La subida nunca vuelve dentro del plazo del test."""
        self.released.wait(10)


@pytest.fixture
def stalled_s3(monkeypatch: pytest.MonkeyPatch) -> Iterator[StalledS3]:
    stalled = StalledS3()
    original_open_chunks = _s3.S3Gateway.open_chunks
    original_next = _s3.ObjectChunks.__next__
    original_close = _s3.ObjectChunks.close
    original_run = _s3.ObjectFetch.run

    def open_chunks(self: _s3.S3Gateway, target: Any) -> _s3.ObjectChunks:
        stalled.open_gate.wait(10)
        chunks = original_open_chunks(self, target)
        with stalled._lock:
            stalled.opened += 1
        return chunks

    def next_chunk(self: _s3.ObjectChunks) -> bytes:
        stalled.read_gate.wait(10)
        return original_next(self)

    def close(self: _s3.ObjectChunks) -> None:
        # Cerrar el cuerpo real interrumpe cualquier `next_chunk` bloqueado en
        # `read_gate`, igual que cerrar un socket real despierta el `recv` en
        # curso (con el error que dé la lectura ya cerrada).
        stalled.read_gate.set()
        original_close(self)
        with stalled._lock:
            if id(self) not in stalled._closed_ids:
                stalled._closed_ids.add(id(self))
                stalled.closed += 1

    def run(self: _s3.ObjectFetch) -> bytes:
        try:
            return original_run(self)
        finally:
            stalled.fetch_finished.set()

    monkeypatch.setattr(_s3.S3Gateway, "upload", stalled.upload)
    monkeypatch.setattr(_s3.S3Gateway, "open_chunks", open_chunks)
    monkeypatch.setattr(_s3.ObjectChunks, "__next__", next_chunk)
    monkeypatch.setattr(_s3.ObjectChunks, "close", close)
    monkeypatch.setattr(_s3.ObjectFetch, "run", run)
    try:
        yield stalled
    finally:
        stalled.released.set()
        stalled.open_gate.set()
        stalled.read_gate.set()
