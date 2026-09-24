"""`AsyncSandbox`: misma superficie que `Sandbox` sobre `grpc.aio`."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import grpc
import pytest

from rayito import AsyncSandbox
from rayito._limits import DEFAULT_PORT
from rayito._sandbox_base import ReadinessPoll
from rayito._transport import (
    ACCESS_TOKEN_KEY,
    PROXY_AUTH_KEY,
    PROXY_FORBIDDEN_MARKER,
    PROXY_FORCE_H2_KEY,
    PROXY_PORT_KEY,
)
from rayito.exceptions import AuthenticationException, SandboxNotReadyException
from rayito.sandbox_async.pty import AsyncPty
from rayito.v1 import health_pb2

from .conftest import (
    ACCESS_TOKEN,
    IMAGE_ARN,
    JWE,
    SANDBOX_ID,
    FakeRpcError,
    RaydEndpoint,
    StubbedControlPlane,
    auth_token_response,
    list_item,
    microvm_response,
)
from .log_capture import capture_logs


def proxy_forbidden() -> FakeRpcError:
    return FakeRpcError(
        grpc.StatusCode.PERMISSION_DENIED,
        details="Received http2 header with status: 403",
        debug=f'{{"grpc_status":7,"description":"{PROXY_FORBIDDEN_MARKER}"}}',
    )


def failing_first(real: Callable[..., Any], failures: list[FakeRpcError]) -> Callable[..., Any]:
    async def health(request: Any, timeout: float | None = None) -> Any:
        if failures:
            raise failures.pop(0)
        return await real(request, timeout=timeout)

    return health


def stub_launch(control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint) -> None:
    control_plane.microvms.add_response("run_microvm", microvm_response(endpoint=fake_rayd.host))
    control_plane.microvms.add_response(
        "create_microvm_auth_token",
        auth_token_response(),
        expected_params={
            "microvmIdentifier": SANDBOX_ID,
            "expirationInMinutes": 60,
            "allowedPorts": [{"port": DEFAULT_PORT}],
        },
    )


async def test_async_create_polls_health_and_kills(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.servicer.unavailable_calls = 1
    fake_rayd.servicer.not_ready_calls = 1
    stub_launch(control_plane, fake_rayd)
    control_plane.microvms.add_response(
        "terminate_microvm", {}, expected_params={"microvmIdentifier": SANDBOX_ID}
    )
    async with await AsyncSandbox.create(
        IMAGE_ARN,
        timeout=900,
        idle=None,
        access_token=ACCESS_TOKEN,
        control_plane=control_plane.plane,
        transport=fake_rayd.transport,
        ready_timeout=20,
    ) as sandbox:
        assert sandbox.sandbox_id == SANDBOX_ID
        assert await sandbox.is_running() is True
        host = await sandbox.get_host(DEFAULT_PORT)
        assert host.headers == {PROXY_AUTH_KEY: JWE, PROXY_PORT_KEY: "8080"}
        assert isinstance(sandbox.pty, AsyncPty)
        assert sandbox.pty is sandbox.pty
        assert (await sandbox.run_code("1+1")).text == "2"
    seen = fake_rayd.servicer.health_calls[0]
    assert seen[PROXY_FORCE_H2_KEY] == "true"
    assert seen[ACCESS_TOKEN_KEY] == ACCESS_TOKEN
    assert len(fake_rayd.servicer.health_calls) == 4


async def test_async_ready_timeout_terminates(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.servicer.not_ready_calls = 10_000
    stub_launch(control_plane, fake_rayd)
    control_plane.microvms.add_response(
        "get_microvm",
        microvm_response(
            endpoint=fake_rayd.host, state="TERMINATING", state_reason="run hook failed"
        ),
    )
    with pytest.raises(SandboxNotReadyException) as excinfo:
        await AsyncSandbox.create(
            IMAGE_ARN,
            idle=None,
            access_token=ACCESS_TOKEN,
            control_plane=control_plane.plane,
            transport=fake_rayd.transport,
            ready_timeout=0.4,
        )
    assert excinfo.value.state == "TERMINATING"
    assert excinfo.value.state_reason == "run hook failed"


async def test_async_class_variants_and_list(control_plane: StubbedControlPlane) -> None:
    control_plane.microvms.add_response(
        "terminate_microvm", {}, expected_params={"microvmIdentifier": SANDBOX_ID}
    )
    assert await AsyncSandbox.kill(SANDBOX_ID, control_plane=control_plane.plane) is True
    control_plane.microvms.add_response("get_microvm", microvm_response(state="RUNNING"))
    info = await AsyncSandbox.get_info(
        SANDBOX_ID, read_metadata=False, control_plane=control_plane.plane
    )
    assert info.state == "RUNNING"
    control_plane.microvms.add_response(
        "list_microvms", {"items": [list_item("x", "RUNNING"), list_item("y", "TERMINATING")]}
    )
    listed = await AsyncSandbox.list(control_plane=control_plane.plane)
    assert [item.sandbox_id for item in listed] == ["x"]


async def test_async_pause_resume(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    stub_launch(control_plane, fake_rayd)
    control_plane.microvms.add_response(
        "get_microvm", microvm_response(endpoint=fake_rayd.host, state="RUNNING")
    )
    control_plane.microvms.add_response("suspend_microvm", {}, {"microvmIdentifier": SANDBOX_ID})
    control_plane.microvms.add_response(
        "get_microvm", microvm_response(endpoint=fake_rayd.host, state="SUSPENDED")
    )
    control_plane.microvms.add_response("resume_microvm", {}, {"microvmIdentifier": SANDBOX_ID})
    control_plane.microvms.add_response("create_microvm_auth_token", auth_token_response("jwe-2"))
    control_plane.microvms.add_response("terminate_microvm", {})
    async with await AsyncSandbox.create(
        IMAGE_ARN,
        idle=None,
        access_token=ACCESS_TOKEN,
        control_plane=control_plane.plane,
        transport=fake_rayd.transport,
    ) as sandbox:
        assert await sandbox.pause() is True
        await sandbox.resume()
        assert fake_rayd.servicer.health_calls[-1][PROXY_AUTH_KEY] == "jwe-2"


async def test_async_create_terminates_the_vm_when_token_minting_fails(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    control_plane.microvms.add_response("run_microvm", microvm_response(endpoint=fake_rayd.host))
    control_plane.microvms.add_client_error(
        "create_microvm_auth_token",
        service_error_code="AccessDeniedException",
        http_status_code=403,
    )
    control_plane.microvms.add_response(
        "terminate_microvm", {}, expected_params={"microvmIdentifier": SANDBOX_ID}
    )
    with pytest.raises(AuthenticationException):
        await AsyncSandbox.create(
            IMAGE_ARN,
            idle=None,
            access_token=ACCESS_TOKEN,
            control_plane=control_plane.plane,
            transport=fake_rayd.transport,
        )
    control_plane.microvms.assert_no_pending_responses()
    assert fake_rayd.servicer.health_calls == []


async def test_async_proxy_403_remints_once_and_retries(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_launch(control_plane, fake_rayd)
    control_plane.microvms.add_response(
        "create_microvm_auth_token",
        auth_token_response("jwe-2"),
        expected_params={
            "microvmIdentifier": SANDBOX_ID,
            "expirationInMinutes": 60,
            "allowedPorts": [{"port": DEFAULT_PORT}],
        },
    )
    control_plane.microvms.add_response("terminate_microvm", {})
    async with await AsyncSandbox.create(
        IMAGE_ARN,
        idle=None,
        access_token=ACCESS_TOKEN,
        control_plane=control_plane.plane,
        transport=fake_rayd.transport,
    ) as sandbox:
        monkeypatch.setattr(
            sandbox._health, "Health", failing_first(sandbox._health.Health, [proxy_forbidden()])
        )
        assert await sandbox.is_running() is True
    control_plane.microvms.assert_no_pending_responses()
    assert fake_rayd.servicer.health_calls[-1][PROXY_AUTH_KEY] == "jwe-2"


async def test_async_genuine_permission_denied_is_not_retried(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_launch(control_plane, fake_rayd)
    control_plane.microvms.add_response("terminate_microvm", {})
    denied = FakeRpcError(grpc.StatusCode.PERMISSION_DENIED, details="EACCES", debug="EACCES")
    async with await AsyncSandbox.create(
        IMAGE_ARN,
        idle=None,
        access_token=ACCESS_TOKEN,
        control_plane=control_plane.plane,
        transport=fake_rayd.transport,
    ) as sandbox:
        monkeypatch.setattr(
            sandbox._health, "Health", failing_first(sandbox._health.Health, [denied])
        )
        with pytest.raises(AuthenticationException, match="EACCES") as excinfo:
            await sandbox.is_running()
        assert excinfo.value.proxy_rejected is False
    control_plane.microvms.assert_no_pending_responses()


async def test_async_is_running_request_timeout_bounds_the_health_deadline(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El plazo se verifica en el stub y no en `time_remaining()` del
    servidor: gRPC redondea el deadline al milisegundo hacia arriba y un
    `Health` real de 0.5 s puede caducar con el GIL ocupado."""
    stub_launch(control_plane, fake_rayd)
    control_plane.microvms.add_response("terminate_microvm", {})
    timeouts: list[float | None] = []

    async def health(
        request: health_pb2.HealthRequest, timeout: float | None = None
    ) -> health_pb2.HealthResponse:
        timeouts.append(timeout)
        return health_pb2.HealthResponse(agent_ready=True)

    async with await AsyncSandbox.create(
        IMAGE_ARN,
        idle=None,
        access_token=ACCESS_TOKEN,
        control_plane=control_plane.plane,
        transport=fake_rayd.transport,
    ) as sandbox:
        monkeypatch.setattr(sandbox._health, "Health", health)
        assert await sandbox.is_running(request_timeout=0.5) is True
        assert await sandbox.is_running() is True
        assert timeouts == [0.5, ReadinessPoll.MAX_RPC_TIMEOUT]


async def test_async_logger_receives_the_create_and_readiness_records(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.servicer.unavailable_calls = 1
    stub_launch(control_plane, fake_rayd)
    control_plane.microvms.add_response("terminate_microvm", {})
    custom = logging.getLogger("tests.custom.async")
    with capture_logs("tests.custom.async") as mine, capture_logs("rayito.sandbox") as default:
        sandbox = await AsyncSandbox.create(
            IMAGE_ARN,
            idle=None,
            access_token=ACCESS_TOKEN,
            control_plane=control_plane.plane,
            transport=fake_rayd.transport,
            logger=custom,
        )
        await sandbox.kill()
    messages = mine.messages()
    assert f"run-microvm aceptado: {SANDBOX_ID} (PENDING)" in messages
    assert any("Health aún no alcanzable" in message for message in messages)
    assert default.messages() == []
    assert sandbox._logger_or(logging.getLogger("rayito.commands")) is custom
