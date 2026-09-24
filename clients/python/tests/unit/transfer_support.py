"""Piezas comunes de `test_transfer_sync.py` y `test_transfer_async.py`: el
`rayd` falso con transferencias, el `S3Staging` de los tests y un espía de
stub que registra los kwargs de cada llamada gRPC (compresión, metadata)."""

from __future__ import annotations

import io
import time
import urllib.parse
from collections.abc import Callable
from typing import Any, cast

import grpc

from rayito import S3Staging
from rayito._limits import DEFAULT_PORT

from .conftest import (
    SANDBOX_ID,
    RaydEndpoint,
    StubbedControlPlane,
    auth_token_response,
    microvm_response,
    start_fake_rayd,
    token_sha256_for_tests,
)
from .fake_s3 import BUCKET, REGION
from .fake_transfer import FakeTransferFilesystemService

MIB = 1_048_576
WAIT_BUDGET_SECONDS = 5.0
POLL_SECONDS = 0.02
STAGING = S3Staging(bucket=BUCKET, threshold_bytes=MIB, multipart_threshold_bytes=16 * MIB)
REGIONAL_HOST = f"{BUCKET}.s3.{REGION}.amazonaws.com"


def start_transfer_rayd() -> tuple[grpc.Server, RaydEndpoint]:
    return start_fake_rayd(
        filesystem=FakeTransferFilesystemService(token_sha256=token_sha256_for_tests())
    )


def transfer_files(endpoint: RaydEndpoint) -> FakeTransferFilesystemService:
    return cast("FakeTransferFilesystemService", endpoint.filesystem)


def stub_launch(control_plane: StubbedControlPlane, endpoint: RaydEndpoint) -> None:
    control_plane.microvms.add_response("run_microvm", microvm_response(endpoint=endpoint.host))
    control_plane.microvms.add_response(
        "create_microvm_auth_token",
        auth_token_response(),
        expected_params={
            "microvmIdentifier": SANDBOX_ID,
            "expirationInMinutes": 60,
            "allowedPorts": [{"port": DEFAULT_PORT}],
        },
    )


def stub_terminate(control_plane: StubbedControlPlane) -> None:
    control_plane.microvms.add_response(
        "terminate_microvm", {}, expected_params={"microvmIdentifier": SANDBOX_ID}
    )


def wait_until(predicate: Callable[[], bool], timeout: float = WAIT_BUDGET_SECONDS) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "la condición no se cumplió a tiempo"
        time.sleep(POLL_SECONDS)


def url_host(url: str) -> str | None:
    return urllib.parse.urlsplit(url).hostname


def url_query(url: str) -> dict[str, str]:
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))


class NonSeekableRaw(io.RawIOBase):
    """El extremo crudo de una tubería: se lee, no se puede buscar."""

    def __init__(self, data: bytes) -> None:
        self._inner = io.BytesIO(data)

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False

    def readinto(self, buffer: Any) -> int:
        chunk = self._inner.read(len(buffer))
        buffer[: len(chunk)] = chunk
        return len(chunk)


def non_seekable(data: bytes) -> io.BufferedReader:
    """Un stream binario de tamaño desconocido (una tubería)."""
    return io.BufferedReader(NonSeekableRaw(data))


class StubSpy:
    """Envuelve un stub gRPC y registra `(rpc, kwargs)` de cada llamada."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __getattr__(self, name: str) -> Any:
        method = getattr(self._inner, name)

        def call(*args: Any, **kwargs: Any) -> Any:
            self.calls.append((name, dict(kwargs)))
            return method(*args, **kwargs)

        return call

    def kwargs_of(self, rpc: str) -> list[dict[str, Any]]:
        return [kwargs for name, kwargs in self.calls if name == rpc]


def spy_on_files(sandbox: Any, stub_type: Any) -> StubSpy:
    """Sustituye el stub de `FilesystemService` del canal de unarios (lo usan
    `_files_call` y `_open_stream(stream=False)`) por un espía."""
    spy = StubSpy(sandbox._files)
    sandbox._files = spy
    sandbox._unary_stubs[stub_type] = spy
    return spy
