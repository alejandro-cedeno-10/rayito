"""Tabla de mapeo de errores: botocore por nombre, gRPC por status, StreamError por código."""

from __future__ import annotations

from typing import Any

import grpc
import pytest
from botocore.exceptions import ClientError

from rayito._aws import translate_client_error
from rayito._transport import (
    PROXY_FORBIDDEN_MARKER,
    is_not_yet_reachable,
    is_proxy_forbidden,
    translate_rpc_error,
    translate_stream_error,
)
from rayito.exceptions import (
    AuthenticationException,
    CapacityException,
    FileNotFoundException,
    InvalidArgumentException,
    NotFoundException,
    QuotaExceededException,
    RateLimitException,
    SandboxException,
    SandboxNotFoundException,
    SandboxStateException,
    TimeoutException,
)


def client_error(code: str, status: int, **fields: Any) -> ClientError:
    response: dict[str, Any] = {
        "Error": {"Code": code, "Message": f"{code} happened"},
        "ResponseMetadata": {"HTTPStatusCode": status},
        **fields,
    }
    return ClientError(response, "RunMicrovm")


@pytest.mark.parametrize(
    ("code", "status", "expected"),
    [
        ("ResourceNotFoundException", 404, SandboxNotFoundException),
        ("ValidationException", 400, InvalidArgumentException),
        ("AccessDeniedException", 403, AuthenticationException),
        ("ThrottlingException", 429, RateLimitException),
        ("ConflictException", 409, SandboxStateException),
        ("ServiceQuotaExceededException", 402, QuotaExceededException),
        ("InsufficientCapacityException", 500, CapacityException),
        ("InternalServerException", 500, SandboxException),
        ("SomethingNew", 418, SandboxException),
    ],
)
def test_botocore_errors_map_by_name(code: str, status: int, expected: type[Exception]) -> None:
    mapped = translate_client_error(client_error(code, status))
    assert type(mapped) is expected
    assert f"{code} happened" in str(mapped)
    if isinstance(mapped, SandboxException):
        assert mapped.aws_code == code
        assert mapped.status_code == status


def test_quota_exceeded_is_mapped_by_name_not_by_402() -> None:
    mapped = translate_client_error(
        client_error("ServiceQuotaExceededException", 402, quotaCode="L-1")
    )
    assert isinstance(mapped, QuotaExceededException)
    assert mapped.quota_code == "L-1"
    assert not isinstance(mapped, SandboxException)


def test_throttling_carries_retry_after_header_field() -> None:
    mapped = translate_client_error(client_error("ThrottlingException", 429, retryAfterSeconds=3))
    assert isinstance(mapped, RateLimitException)
    assert mapped.retry_after == 3.0


class FakeRpcError(grpc.RpcError):
    def __init__(self, code: grpc.StatusCode, details: str = "boom", debug: str = "") -> None:
        super().__init__(details)
        self._code = code
        self._details = details
        self._debug = debug

    def code(self) -> grpc.StatusCode:
        return self._code

    def details(self) -> str:
        return self._details

    def debug_error_string(self) -> str:
        return self._debug


@pytest.mark.parametrize(
    ("code", "filesystem", "expected"),
    [
        (grpc.StatusCode.INVALID_ARGUMENT, False, InvalidArgumentException),
        (grpc.StatusCode.UNIMPLEMENTED, False, InvalidArgumentException),
        (grpc.StatusCode.UNAUTHENTICATED, False, AuthenticationException),
        (grpc.StatusCode.PERMISSION_DENIED, False, AuthenticationException),
        (grpc.StatusCode.NOT_FOUND, False, NotFoundException),
        (grpc.StatusCode.NOT_FOUND, True, FileNotFoundException),
        (grpc.StatusCode.OUT_OF_RANGE, False, NotFoundException),
        (grpc.StatusCode.FAILED_PRECONDITION, False, InvalidArgumentException),
        (grpc.StatusCode.RESOURCE_EXHAUSTED, False, RateLimitException),
        (grpc.StatusCode.DEADLINE_EXCEEDED, False, TimeoutException),
        (grpc.StatusCode.CANCELLED, False, SandboxException),
        (grpc.StatusCode.UNAVAILABLE, False, SandboxException),
        (grpc.StatusCode.INTERNAL, False, SandboxException),
    ],
)
def test_grpc_errors_map_by_status(
    code: grpc.StatusCode, filesystem: bool, expected: type[Exception]
) -> None:
    mapped = translate_rpc_error(FakeRpcError(code), filesystem=filesystem)
    assert type(mapped) is expected
    assert isinstance(mapped, SandboxException | AuthenticationException)
    assert mapped.grpc_code is code


def test_proxy_403_is_distinguished_from_rayd_permission_denied() -> None:
    proxy = FakeRpcError(
        grpc.StatusCode.PERMISSION_DENIED,
        debug=f"UNKNOWN:Error received from peer {{grpc_message:'', {PROXY_FORBIDDEN_MARKER}}}",
    )
    assert is_proxy_forbidden(proxy) is True
    mapped = translate_rpc_error(proxy)
    assert isinstance(mapped, AuthenticationException) and mapped.proxy_rejected is True

    genuine = FakeRpcError(grpc.StatusCode.PERMISSION_DENIED, details="EACCES /root/x")
    assert is_proxy_forbidden(genuine) is False
    mapped = translate_rpc_error(genuine)
    assert isinstance(mapped, AuthenticationException) and mapped.proxy_rejected is False


def test_not_yet_reachable_covers_unavailable_and_deadline() -> None:
    assert is_not_yet_reachable(FakeRpcError(grpc.StatusCode.UNAVAILABLE))
    assert is_not_yet_reachable(FakeRpcError(grpc.StatusCode.DEADLINE_EXCEEDED))
    assert not is_not_yet_reachable(FakeRpcError(grpc.StatusCode.UNAUTHENTICATED))


@pytest.mark.parametrize(
    ("code", "filesystem", "expected"),
    [
        ("not_found", False, NotFoundException),
        ("not_found", True, FileNotFoundException),
        ("permission_denied", False, AuthenticationException),
        ("deadline_exceeded", False, TimeoutException),
        ("unimplemented", False, InvalidArgumentException),
        ("invalid_argument", False, InvalidArgumentException),
        ("suspending", False, SandboxStateException),
        ("output_truncated", False, SandboxException),
        ("kernel_died", False, SandboxException),
        ("something_unknown", False, SandboxException),
    ],
)
def test_stream_error_codes(code: str, filesystem: bool, expected: type[Exception]) -> None:
    assert type(translate_stream_error(code, "m", filesystem=filesystem)) is expected


def test_hierarchy_matches_e2b_shape() -> None:
    assert issubclass(FileNotFoundException, NotFoundException)
    assert issubclass(SandboxNotFoundException, NotFoundException)
    assert issubclass(NotFoundException, SandboxException)
    assert issubclass(TimeoutException, SandboxException)
    assert issubclass(RateLimitException, SandboxException)
    assert not issubclass(AuthenticationException, SandboxException)
    assert not issubclass(QuotaExceededException, SandboxException)
    assert not issubclass(CapacityException, SandboxException)
