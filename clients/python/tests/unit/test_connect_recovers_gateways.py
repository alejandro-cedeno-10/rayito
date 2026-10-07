"""`connect()` (sync and async) rebuilds `sbx.gateways` from a read-only
`ConfigureStatus` when this handle did not apply `gateways=` itself, so a
second process can drive `sbx.agent` without recreating the sandbox: only
names, ports and the last error class travel, never a header value."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, cast

import pytest

import rayito.sandbox_async.main as async_main
import rayito.sandbox_sync.main as sync_main
from rayito import AsyncSandbox, Sandbox
from rayito._configure_base import AgentFeatures
from rayito._secret_gateway import (
    EMPTY_GATEWAYS,
    GatewayHandle,
    GatewayStatus,
    gateways_recoverable,
    owns_gateways,
    recovered_gateways,
)
from rayito._secret_gateway import SECTION_NAME as GATEWAY_SECTION
from rayito.exceptions import SandboxException
from rayito.v1 import configure_pb2, features_pb2, secret_gateway_pb2

from .conftest import (
    ACCESS_TOKEN,
    SANDBOX_ID,
    RaydEndpoint,
    StubbedControlPlane,
    auth_token_response,
    microvm_response,
)

FIRST_PORT = 40123
SECOND_PORT = 40999
REQUEST_TIMEOUT_SECONDS = 5.0


def status_with(port: int, error_class: str = "") -> configure_pb2.ConfigureStatusResponse:
    status = configure_pb2.ConfigureStatusResponse()
    status.secret_gateway.routes.append(
        secret_gateway_pb2.SecretGatewayRouteStatus(
            name="bedrock",
            port=port,
            state=secret_gateway_pb2.SecretGatewayRouteState.SECRET_GATEWAY_ROUTE_STATE_LISTENING,
            last_error_class=error_class,
        )
    )
    return status


def bare(cls: type[Sandbox] | type[AsyncSandbox], features: AgentFeatures | None) -> Any:
    sandbox = cls.__new__(cls)
    sandbox._agent_features = features
    sandbox._configure = object()
    sandbox._request_timeout = REQUEST_TIMEOUT_SECONDS
    sandbox._logger = logging.getLogger("test.connect_recovers_gateways")
    sandbox._info = cast(Any, SimpleNamespace(sandbox_id="mvm-test-connect-gateways"))
    sandbox._section_handles = {}
    return sandbox


FULL = AgentFeatures(configure=True, secret_gateway=True)


# ------------------------------------------------------------------ domain


@pytest.mark.parametrize(
    ("features", "expected"),
    [
        (None, False),
        (AgentFeatures(configure=True), False),
        (AgentFeatures(secret_gateway=True), False),
        (FULL, True),
    ],
)
def test_gateways_recoverable_needs_configure_and_the_flag(
    features: AgentFeatures | None, expected: bool
) -> None:
    assert gateways_recoverable(features) is expected


def test_recovered_gateways_is_empty_without_routes() -> None:
    assert recovered_gateways(configure_pb2.ConfigureStatusResponse()) is EMPTY_GATEWAYS


def test_recovered_gateways_maps_names_ports_and_errors() -> None:
    handle = recovered_gateways(status_with(FIRST_PORT, "upstream_timeout"))
    assert dict(handle) == {
        "bedrock": GatewayStatus(port=FIRST_PORT, last_error_class="upstream_timeout")
    }
    assert handle["bedrock"].url == f"http://127.0.0.1:{FIRST_PORT}"


def test_recovered_refresh_only_rereads_the_status() -> None:
    reads = iter([status_with(SECOND_PORT)])
    handle = recovered_gateways(status_with(FIRST_PORT), reader=lambda: next(reads))
    handle.refresh()
    assert handle["bedrock"].port == SECOND_PORT


# -------------------------------------------------------------------- sync


def test_sync_recover_reads_the_status_once(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[float] = []

    def fake_status(stub: object, *, timeout: float) -> configure_pb2.ConfigureStatusResponse:
        calls.append(timeout)
        return status_with(FIRST_PORT)

    monkeypatch.setattr(sync_main, "call_configure_status", fake_status)
    sandbox = bare(Sandbox, FULL)
    sandbox._recover_gateways()
    assert sandbox.gateways["bedrock"].port == FIRST_PORT
    assert calls == [REQUEST_TIMEOUT_SECONDS]


def test_sync_recover_keeps_the_handle_that_applied_gateways(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sync_main, "call_configure_status", pytest.fail)
    sandbox = bare(Sandbox, FULL)
    own = GatewayHandle({"bedrock": GatewayStatus(port=FIRST_PORT)})
    sandbox._section_handles[GATEWAY_SECTION] = own
    sandbox._recover_gateways()
    assert sandbox.gateways is own


@pytest.mark.parametrize("features", [None, AgentFeatures(configure=True)])
def test_sync_recover_makes_no_call_without_the_feature(
    monkeypatch: pytest.MonkeyPatch, features: AgentFeatures | None
) -> None:
    monkeypatch.setattr(sync_main, "call_configure_status", pytest.fail)
    sandbox = bare(Sandbox, features)
    sandbox._recover_gateways()
    assert sandbox.gateways is EMPTY_GATEWAYS


# ------------------------------------------------------------------- async


async def test_async_recover_reads_the_status_and_refreshes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reads = iter([status_with(FIRST_PORT), status_with(SECOND_PORT)])

    async def fake_status(stub: object, *, timeout: float) -> configure_pb2.ConfigureStatusResponse:
        return next(reads)

    monkeypatch.setattr(async_main, "call_configure_status", fake_status)
    sandbox = bare(AsyncSandbox, FULL)
    await sandbox._recover_gateways()
    assert sandbox.gateways["bedrock"].port == FIRST_PORT
    await sandbox.gateways.arefresh()
    assert sandbox.gateways["bedrock"].port == SECOND_PORT


async def test_async_recover_makes_no_call_without_the_feature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(async_main, "call_configure_status", pytest.fail)
    sandbox = bare(AsyncSandbox, None)
    await sandbox._recover_gateways()
    assert sandbox.gateways is EMPTY_GATEWAYS


# ------------------------------------------- through the real connect() path
#
# `Sandbox.connect(...)` / `sbx.connect()` against the fake `rayd` (as
# `test_reconnect_sync`/`_async` do): the agent advertises the feature over
# `Health`, and `call_configure_status`/`call_configure` (the
# `ConfigureService` port) are recorded, so deleting the wiring in
# `connect()` makes these fail.

CONNECT_REQUEST_TIMEOUT_SECONDS = 7.0


@dataclass
class ConfigureRecorder:
    """Records each `ConfigureStatus` timeout and fails on any `Configure`."""

    statuses: list[configure_pb2.ConfigureStatusResponse]
    timeouts: list[float] = field(default_factory=list)
    fail_with: BaseException | None = None

    def status(self, stub: object, *, timeout: float) -> configure_pb2.ConfigureStatusResponse:
        self.timeouts.append(timeout)
        if self.fail_with is not None:
            raise self.fail_with
        return self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]

    async def astatus(
        self, stub: object, *, timeout: float
    ) -> configure_pb2.ConfigureStatusResponse:
        return self.status(stub, timeout=timeout)


def advertise_gateways(fake_rayd: RaydEndpoint) -> None:
    fake_rayd.servicer.features = features_pb2.AgentFeatures(configure=True, secret_gateway=True)


def stub_static_connect(control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint) -> None:
    control_plane.microvms.add_response(
        "get_microvm", microvm_response(endpoint=fake_rayd.host, state="RUNNING")
    )
    control_plane.microvms.add_response("create_microvm_auth_token", auth_token_response())


def stub_instance_connect(control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint) -> None:
    control_plane.microvms.add_response(
        "get_microvm", microvm_response(endpoint=fake_rayd.host, state="RUNNING")
    )


def record(
    monkeypatch: pytest.MonkeyPatch, module: Any, recorder: ConfigureRecorder, *, is_async: bool
) -> None:
    monkeypatch.setattr(
        module, "call_configure_status", recorder.astatus if is_async else recorder.status
    )
    monkeypatch.setattr(module, "call_configure", pytest.fail)


def test_sync_static_connect_recovers_without_any_configure(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    advertise_gateways(fake_rayd)
    recorder = ConfigureRecorder([status_with(FIRST_PORT)])
    record(monkeypatch, sync_main, recorder, is_async=False)
    stub_static_connect(control_plane, fake_rayd)
    sbx = Sandbox.connect(
        SANDBOX_ID,
        access_token=ACCESS_TOKEN,
        control_plane=control_plane.plane,
        transport=fake_rayd.transport,
        request_timeout=CONNECT_REQUEST_TIMEOUT_SECONDS,
    )
    try:
        assert sbx.gateways["bedrock"].port == FIRST_PORT
        assert sbx.gateways.recovered
        assert recorder.timeouts == [CONNECT_REQUEST_TIMEOUT_SECONDS]
    finally:
        sbx.close()


def test_sync_static_connect_closes_the_handle_when_the_read_fails(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    advertise_gateways(fake_rayd)
    recorder = ConfigureRecorder([], fail_with=SandboxException("configure status failed"))
    record(monkeypatch, sync_main, recorder, is_async=False)
    closed: list[Sandbox] = []
    original_close = Sandbox.close

    def spy_close(self: Sandbox) -> None:
        closed.append(self)
        original_close(self)

    monkeypatch.setattr(Sandbox, "close", spy_close)
    stub_static_connect(control_plane, fake_rayd)
    with pytest.raises(SandboxException, match="configure status failed"):
        Sandbox.connect(
            SANDBOX_ID,
            access_token=ACCESS_TOKEN,
            control_plane=control_plane.plane,
            transport=fake_rayd.transport,
        )
    assert len(closed) == 1


def test_sync_instance_connect_uses_its_request_timeout_and_rereads_a_recovered_handle(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    advertise_gateways(fake_rayd)
    recorder = ConfigureRecorder([status_with(FIRST_PORT), status_with(SECOND_PORT)])
    record(monkeypatch, sync_main, recorder, is_async=False)
    stub_static_connect(control_plane, fake_rayd)
    sbx = Sandbox.connect(
        SANDBOX_ID,
        access_token=ACCESS_TOKEN,
        control_plane=control_plane.plane,
        transport=fake_rayd.transport,
    )
    try:
        assert sbx.gateways["bedrock"].port == FIRST_PORT
        stub_instance_connect(control_plane, fake_rayd)
        sbx.connect(request_timeout=CONNECT_REQUEST_TIMEOUT_SECONDS)
        assert sbx.gateways["bedrock"].port == SECOND_PORT
        assert recorder.timeouts[-1] == CONNECT_REQUEST_TIMEOUT_SECONDS
    finally:
        sbx.close()


def test_sync_instance_connect_keeps_the_handle_that_applied_gateways(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    advertise_gateways(fake_rayd)
    recorder = ConfigureRecorder([configure_pb2.ConfigureStatusResponse()])
    record(monkeypatch, sync_main, recorder, is_async=False)
    stub_static_connect(control_plane, fake_rayd)
    sbx = Sandbox.connect(
        SANDBOX_ID,
        access_token=ACCESS_TOKEN,
        control_plane=control_plane.plane,
        transport=fake_rayd.transport,
    )
    try:
        own = GatewayHandle({"bedrock": GatewayStatus(port=FIRST_PORT)})
        sbx._section_handles[GATEWAY_SECTION] = own
        reads_before = len(recorder.timeouts)
        stub_instance_connect(control_plane, fake_rayd)
        sbx.connect()
        assert sbx.gateways is own
        assert len(recorder.timeouts) == reads_before
    finally:
        sbx.close()


async def test_async_static_connect_recovers_without_any_configure(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    advertise_gateways(fake_rayd)
    recorder = ConfigureRecorder([status_with(FIRST_PORT)])
    record(monkeypatch, async_main, recorder, is_async=True)
    stub_static_connect(control_plane, fake_rayd)
    sbx = await AsyncSandbox.connect(
        SANDBOX_ID,
        access_token=ACCESS_TOKEN,
        control_plane=control_plane.plane,
        transport=fake_rayd.transport,
        request_timeout=CONNECT_REQUEST_TIMEOUT_SECONDS,
    )
    try:
        assert sbx.gateways["bedrock"].port == FIRST_PORT
        assert sbx.gateways.recovered
        assert recorder.timeouts == [CONNECT_REQUEST_TIMEOUT_SECONDS]
    finally:
        await sbx.close()


async def test_async_static_connect_closes_the_handle_when_the_read_fails(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    advertise_gateways(fake_rayd)
    recorder = ConfigureRecorder([], fail_with=SandboxException("configure status failed"))
    record(monkeypatch, async_main, recorder, is_async=True)
    closed: list[AsyncSandbox] = []
    original_close = AsyncSandbox.close

    async def spy_close(self: AsyncSandbox) -> None:
        closed.append(self)
        await original_close(self)

    monkeypatch.setattr(AsyncSandbox, "close", spy_close)
    stub_static_connect(control_plane, fake_rayd)
    with pytest.raises(SandboxException, match="configure status failed"):
        await AsyncSandbox.connect(
            SANDBOX_ID,
            access_token=ACCESS_TOKEN,
            control_plane=control_plane.plane,
            transport=fake_rayd.transport,
        )
    assert len(closed) == 1


async def test_async_instance_connect_uses_its_request_timeout_and_rereads_a_recovered_handle(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    advertise_gateways(fake_rayd)
    recorder = ConfigureRecorder([status_with(FIRST_PORT), status_with(SECOND_PORT)])
    record(monkeypatch, async_main, recorder, is_async=True)
    stub_static_connect(control_plane, fake_rayd)
    sbx = await AsyncSandbox.connect(
        SANDBOX_ID,
        access_token=ACCESS_TOKEN,
        control_plane=control_plane.plane,
        transport=fake_rayd.transport,
    )
    try:
        assert sbx.gateways["bedrock"].port == FIRST_PORT
        stub_instance_connect(control_plane, fake_rayd)
        await sbx.connect(request_timeout=CONNECT_REQUEST_TIMEOUT_SECONDS)
        assert sbx.gateways["bedrock"].port == SECOND_PORT
        assert recorder.timeouts[-1] == CONNECT_REQUEST_TIMEOUT_SECONDS
    finally:
        await sbx.close()


@pytest.mark.parametrize(
    ("handle", "expected"),
    [
        (None, False),
        (GatewayHandle({}), True),
        (recovered_gateways(status_with(FIRST_PORT)), False),
    ],
)
def test_owns_gateways_only_for_the_handle_that_applied_them(
    handle: object | None, expected: bool
) -> None:
    assert owns_gateways(handle) is expected
