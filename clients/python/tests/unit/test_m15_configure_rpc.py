"""`sandbox_sync/configure.py` y `sandbox_async/configure.py`: llaman al
stub generado y traducen `grpc.RpcError` con la tabla compartida. Sin
servidor real: un doble mínimo del stub basta, porque el adaptador sólo
llama a sus dos métodos."""

from __future__ import annotations

import grpc
import pytest

from rayito.exceptions import InvalidArgumentException
from rayito.sandbox_async import configure as async_configure
from rayito.sandbox_sync import configure as sync_configure
from rayito.v1 import configure_pb2


class FakeRpcError(grpc.RpcError):
    def __init__(self, code: grpc.StatusCode, details: str) -> None:
        super().__init__(details)
        self._code = code
        self._details = details

    def code(self) -> grpc.StatusCode:
        return self._code

    def details(self) -> str:
        return self._details

    def debug_error_string(self) -> str:
        return ""


class FakeSyncStub:
    def __init__(self, response: object | None = None, error: grpc.RpcError | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[tuple[str, object, float]] = []

    def Configure(self, request: object, *, timeout: float) -> object:
        self.calls.append(("Configure", request, timeout))
        if self.error is not None:
            raise self.error
        return self.response

    def ConfigureStatus(self, request: object, *, timeout: float) -> object:
        self.calls.append(("ConfigureStatus", request, timeout))
        if self.error is not None:
            raise self.error
        return self.response


class FakeAsyncStub:
    def __init__(self, response: object | None = None, error: grpc.RpcError | None = None) -> None:
        self.response = response
        self.error = error

    async def Configure(self, request: object, *, timeout: float) -> object:
        if self.error is not None:
            raise self.error
        return self.response

    async def ConfigureStatus(self, request: object, *, timeout: float) -> object:
        if self.error is not None:
            raise self.error
        return self.response


def test_call_configure_forwards_the_request_and_timeout() -> None:
    response = configure_pb2.ConfigureResponse(config_generation=1)
    stub = FakeSyncStub(response=response)
    request = configure_pb2.ConfigureRequest(request_id="r1")
    result = sync_configure.call_configure(stub, request, timeout=5.0)
    assert result is response
    assert stub.calls == [("Configure", request, 5.0)]


def test_call_configure_translates_grpc_errors() -> None:
    stub = FakeSyncStub(error=FakeRpcError(grpc.StatusCode.INVALID_ARGUMENT, "bad section"))
    with pytest.raises(InvalidArgumentException, match="bad section"):
        sync_configure.call_configure(stub, configure_pb2.ConfigureRequest(), timeout=5.0)


def test_call_configure_status_sends_an_empty_request() -> None:
    response = configure_pb2.ConfigureStatusResponse()
    stub = FakeSyncStub(response=response)
    result = sync_configure.call_configure_status(stub, timeout=5.0)
    assert result is response
    assert stub.calls[0][1] == configure_pb2.ConfigureStatusRequest()


@pytest.mark.asyncio
async def test_async_call_configure_forwards_the_request() -> None:
    response = configure_pb2.ConfigureResponse(config_generation=2)
    stub = FakeAsyncStub(response=response)
    result = await async_configure.call_configure(
        stub, configure_pb2.ConfigureRequest(), timeout=5.0
    )
    assert result is response


@pytest.mark.asyncio
async def test_async_call_configure_translates_grpc_errors() -> None:
    stub = FakeAsyncStub(error=FakeRpcError(grpc.StatusCode.FAILED_PRECONDITION, "not_running"))
    with pytest.raises(Exception, match="not_running"):
        await async_configure.call_configure(stub, configure_pb2.ConfigureRequest(), timeout=5.0)
