"""`AsyncSandbox.get_metrics_history`: la misma superficie que la versión
síncrona sobre `grpc.aio`, contra el mismo `rayd` falso."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta

import grpc
import pytest

from rayito import AsyncSandbox
from rayito._metrics_base import (
    CLASS_HISTORY_FEATURE,
    HISTORY_FEATURE,
    HISTORY_UNIMPLEMENTED_REASON,
)
from rayito._payload import generate_access_token
from rayito._sandbox_base import ACCESS_TOKEN_ENV_VAR
from rayito.exceptions import (
    AuthenticationException,
    InvalidArgumentException,
    SandboxException,
    SandboxNotFoundException,
    SandboxStateException,
    UnimplementedError,
)
from rayito.sandbox_async.main import invoke_reminting_async

from .conftest import (
    ACCESS_TOKEN,
    IMAGE_ARN,
    SANDBOX_ID,
    FakeRpcError,
    RaydEndpoint,
    StubbedControlPlane,
    TrackingTransport,
)
from .test_metrics_history_sync import (
    SEEDED_HISTORY,
    WINDOW_START,
    WINDOW_START_MS,
    counting_refresher,
    proxy_forbidden,
    stub_class_call,
    stub_launch,
)


@pytest.fixture
async def sandbox(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> AsyncIterator[AsyncSandbox]:
    stub_launch(control_plane, fake_rayd)
    created = await AsyncSandbox.create(
        IMAGE_ARN,
        idle=None,
        access_token=ACCESS_TOKEN,
        control_plane=control_plane.plane,
        transport=fake_rayd.transport,
    )
    try:
        yield created
    finally:
        control_plane.microvms.add_response("terminate_microvm", {})
        await created.kill()


async def test_history_request_comes_from_datetimes_and_maps_ascending(
    sandbox: AsyncSandbox, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.servicer.history = list(SEEDED_HISTORY)
    samples = await sandbox.get_metrics_history(
        start=WINDOW_START, end=WINDOW_START + timedelta(minutes=5), max_points=2
    )
    request = fake_rayd.servicer.history_requests[-1]
    assert (request.start_unix_ms, request.end_unix_ms, request.max_points) == (
        WINDOW_START_MS,
        WINDOW_START_MS + 300_000,
        2,
    )
    assert [sample.mem_cache_bytes for sample in samples] == [11, 22, 33]
    assert samples[-1].timestamp == WINDOW_START + timedelta(seconds=15)
    assert fake_rayd.servicer.history_calls[-1]["x-access-token"] == ACCESS_TOKEN


async def test_history_validates_before_any_rpc(
    sandbox: AsyncSandbox, fake_rayd: RaydEndpoint
) -> None:
    with pytest.raises(InvalidArgumentException, match="posterior"):
        await sandbox.get_metrics_history(
            start=WINDOW_START, end=WINDOW_START - timedelta(seconds=1)
        )
    with pytest.raises(InvalidArgumentException, match="max_points"):
        await sandbox.get_metrics_history(max_points=0)
    assert fake_rayd.servicer.history_requests == []


async def test_history_without_arguments_sends_zeros(
    sandbox: AsyncSandbox, fake_rayd: RaydEndpoint
) -> None:
    assert await sandbox.get_metrics_history() == []
    request = fake_rayd.servicer.history_requests[-1]
    assert (request.start_unix_ms, request.end_unix_ms, request.max_points) == (0, 0, 0)


async def test_history_on_a_pre_m9_agent_says_so(
    sandbox: AsyncSandbox, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.servicer.history_unimplemented = True
    with pytest.raises(UnimplementedError) as excinfo:
        await sandbox.get_metrics_history()
    assert excinfo.value.feature == HISTORY_FEATURE
    assert excinfo.value.reason == HISTORY_UNIMPLEMENTED_REASON and "M9" in str(excinfo.value)
    assert not isinstance(excinfo.value, SandboxException)
    cause = excinfo.value.__cause__
    assert isinstance(cause, SandboxException)
    assert cause.grpc_code is grpc.StatusCode.UNIMPLEMENTED


async def test_snapshot_get_metrics_maps_mem_cache(
    sandbox: AsyncSandbox, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.servicer.mem_cache_bytes = 4096
    assert (await sandbox.get_metrics()).mem_cache_bytes == 4096


async def test_class_history_without_a_token_never_calls_aws(
    control_plane: StubbedControlPlane, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ACCESS_TOKEN_ENV_VAR, raising=False)
    with pytest.raises(AuthenticationException, match="access_token"):
        await AsyncSandbox.get_metrics_history(SANDBOX_ID, control_plane=control_plane.plane)


async def test_class_history_validates_the_range_before_any_aws_call(
    control_plane: StubbedControlPlane,
) -> None:
    with pytest.raises(InvalidArgumentException, match="max_points"):
        await AsyncSandbox.get_metrics_history(
            SANDBOX_ID, access_token=ACCESS_TOKEN, max_points=-1, control_plane=control_plane.plane
        )
    with pytest.raises(InvalidArgumentException, match="posterior"):
        await AsyncSandbox.get_metrics_history(
            SANDBOX_ID,
            access_token=ACCESS_TOKEN,
            start=WINDOW_START,
            end=WINDOW_START - timedelta(seconds=1),
            control_plane=control_plane.plane,
        )
    with pytest.raises(InvalidArgumentException, match="sandbox_id"):
        await AsyncSandbox.get_metrics_history(
            "", access_token=ACCESS_TOKEN, control_plane=control_plane.plane
        )
    control_plane.microvms.assert_no_pending_responses()


@pytest.mark.parametrize("state", ["SUSPENDED", "PENDING"])
async def test_class_history_on_a_sleeping_sandbox_never_mints(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint, state: str
) -> None:
    transport = TrackingTransport.for_loopback()
    stub_class_call(control_plane, fake_rayd, state=state)
    with pytest.raises(SandboxStateException, match="connect"):
        await AsyncSandbox.get_metrics_history(
            SANDBOX_ID,
            access_token=ACCESS_TOKEN,
            control_plane=control_plane.plane,
            transport=transport,
        )
    assert transport.open_count == 0


async def test_class_history_on_a_terminated_sandbox_is_not_found(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    stub_class_call(control_plane, fake_rayd, state="TERMINATING")
    with pytest.raises(SandboxNotFoundException):
        await AsyncSandbox.get_metrics_history(
            SANDBOX_ID, access_token=ACCESS_TOKEN, control_plane=control_plane.plane
        )


async def test_class_history_on_a_running_sandbox_uses_one_dedicated_channel(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.servicer.history = list(SEEDED_HISTORY)
    transport = TrackingTransport.for_loopback()
    stub_class_call(control_plane, fake_rayd, state="RUNNING")
    samples = await AsyncSandbox.get_metrics_history(
        SANDBOX_ID,
        access_token=ACCESS_TOKEN,
        end=WINDOW_START + timedelta(seconds=30),
        control_plane=control_plane.plane,
        transport=transport,
    )
    assert [sample.mem_cache_bytes for sample in samples] == [11, 22, 33]
    assert len(fake_rayd.servicer.history_requests) == 1
    assert fake_rayd.servicer.history_requests[0].end_unix_ms == WINDOW_START_MS + 30_000
    assert fake_rayd.servicer.history_calls[0]["x-access-token"] == ACCESS_TOKEN
    assert transport.open_count == 1 and transport.all_closed


async def test_class_history_reads_the_token_from_the_environment(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ACCESS_TOKEN_ENV_VAR, ACCESS_TOKEN)
    transport = TrackingTransport.for_loopback()
    stub_class_call(control_plane, fake_rayd, state="RUNNING")
    assert (
        await AsyncSandbox.get_metrics_history(
            SANDBOX_ID, control_plane=control_plane.plane, transport=transport
        )
        == []
    )
    assert fake_rayd.servicer.history_calls[0]["x-access-token"] == ACCESS_TOKEN
    assert transport.all_closed


async def test_class_history_with_a_wrong_token_is_unauthenticated(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    transport = TrackingTransport.for_loopback()
    stub_class_call(control_plane, fake_rayd, state="RUNNING")
    with pytest.raises(AuthenticationException):
        await AsyncSandbox.get_metrics_history(
            SANDBOX_ID,
            access_token=generate_access_token(),
            control_plane=control_plane.plane,
            transport=transport,
        )
    assert transport.all_closed


async def test_class_history_on_a_pre_m9_agent_says_so(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.servicer.history_unimplemented = True
    stub_class_call(control_plane, fake_rayd, state="RUNNING")
    with pytest.raises(UnimplementedError) as excinfo:
        await AsyncSandbox.get_metrics_history(
            SANDBOX_ID,
            access_token=ACCESS_TOKEN,
            control_plane=control_plane.plane,
            transport=TrackingTransport.for_loopback(),
        )
    assert excinfo.value.feature == CLASS_HISTORY_FEATURE
    assert excinfo.value.reason == HISTORY_UNIMPLEMENTED_REASON
    cause = excinfo.value.__cause__
    assert isinstance(cause, SandboxException)
    assert cause.grpc_code is grpc.StatusCode.UNIMPLEMENTED


async def test_dedicated_async_call_remints_once_after_a_proxy_403() -> None:
    minted: list[int] = []
    calls: list[int] = []

    async def invoke(_stub: object) -> str:
        calls.append(len(calls))
        if len(calls) == 1:
            raise proxy_forbidden()
        return "history"

    refresher = counting_refresher(minted)
    assert await invoke_reminting_async(invoke, object(), refresher) == "history"
    assert len(minted) == 2 and len(calls) == 2


async def test_dedicated_async_call_does_not_remint_on_other_errors() -> None:
    minted: list[int] = []

    async def invoke(_stub: object) -> str:
        raise FakeRpcError(grpc.StatusCode.UNAUTHENTICATED, details="x-access-token")

    with pytest.raises(grpc.RpcError):
        await invoke_reminting_async(invoke, object(), counting_refresher(minted))
    assert len(minted) == 1
