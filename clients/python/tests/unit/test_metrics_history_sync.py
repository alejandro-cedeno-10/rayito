"""`Sandbox.get_metrics_history` (instancia y variante de clase) contra el
`rayd` falso: el request que llega, el mapeo ascendente con
`mem_cache_bytes`, la validación previa al RPC, un agente anterior a M9 y la
variante de clase con el access token por un canal dedicado."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import grpc
import pytest

from rayito import Sandbox
from rayito._aws import PortSpec
from rayito._limits import DEFAULT_PORT
from rayito._metrics_base import (
    CLASS_HISTORY_FEATURE,
    HISTORY_FEATURE,
    HISTORY_UNIMPLEMENTED_REASON,
)
from rayito._payload import generate_access_token
from rayito._sandbox_base import ACCESS_TOKEN_ENV_VAR
from rayito._transport import PROXY_FORBIDDEN_MARKER, TokenRefresher, TokenStore
from rayito.exceptions import (
    AuthenticationException,
    InvalidArgumentException,
    SandboxException,
    SandboxNotFoundException,
    SandboxStateException,
    UnimplementedError,
)
from rayito.sandbox_sync.main import invoke_reminting
from rayito.v1 import health_pb2

from .conftest import (
    ACCESS_TOKEN,
    IMAGE_ARN,
    JWE,
    SANDBOX_ID,
    FakeRpcError,
    RaydEndpoint,
    StubbedControlPlane,
    TrackingTransport,
    auth_token_response,
    endpoint_of,
    microvm_response,
)

WINDOW_START = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
WINDOW_START_MS = 1_790_078_400_000


def history_sample(offset_s: int, *, cache: int) -> health_pb2.MetricsResponse:
    return health_pb2.MetricsResponse(
        cpu_used_pct=float(offset_s),
        mem_used_bytes=100,
        mem_total_bytes=1_000,
        disk_used_bytes=10,
        disk_total_bytes=20,
        cpu_count=2,
        timestamp_unix_ms=WINDOW_START_MS + offset_s * 1000,
        mem_cache_bytes=cache,
    )


SEEDED_HISTORY = [
    history_sample(5, cache=11),
    history_sample(10, cache=22),
    history_sample(15, cache=33),
]


def stub_launch(control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint) -> None:
    control_plane.microvms.add_response("run_microvm", microvm_response(endpoint=fake_rayd.host))
    control_plane.microvms.add_response("create_microvm_auth_token", auth_token_response())


@pytest.fixture
def sandbox(control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint) -> Iterator[Sandbox]:
    stub_launch(control_plane, fake_rayd)
    created = Sandbox.create(
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
        created.kill()


def stub_class_call(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint, *, state: str
) -> None:
    control_plane.microvms.add_response(
        "get_microvm",
        microvm_response(state=state, endpoint=endpoint_of(fake_rayd)),
        expected_params={"microvmIdentifier": SANDBOX_ID},
    )
    if state != "RUNNING":
        return
    control_plane.microvms.add_response(
        "create_microvm_auth_token",
        auth_token_response(),
        expected_params={
            "microvmIdentifier": SANDBOX_ID,
            "expirationInMinutes": 60,
            "allowedPorts": [{"port": DEFAULT_PORT}],
        },
    )


def test_history_request_comes_from_datetimes_and_maps_ascending(
    sandbox: Sandbox, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.servicer.history = list(SEEDED_HISTORY)
    samples = sandbox.get_metrics_history(
        start=WINDOW_START, end=WINDOW_START + timedelta(minutes=5), max_points=2
    )
    request = fake_rayd.servicer.history_requests[-1]
    assert request.start_unix_ms == WINDOW_START_MS
    assert request.end_unix_ms == WINDOW_START_MS + 300_000
    assert request.max_points == 2
    assert [sample.mem_cache_bytes for sample in samples] == [11, 22, 33]
    assert [sample.timestamp for sample in samples] == [
        WINDOW_START + timedelta(seconds=offset) for offset in (5, 10, 15)
    ]
    assert samples[0].cpu_used_pct == 5.0 and samples[0].cpu_count == 2
    assert fake_rayd.servicer.history_calls[-1]["x-access-token"] == ACCESS_TOKEN


def test_history_without_arguments_sends_zeros(sandbox: Sandbox, fake_rayd: RaydEndpoint) -> None:
    assert sandbox.get_metrics_history() == []
    request = fake_rayd.servicer.history_requests[-1]
    assert (request.start_unix_ms, request.end_unix_ms, request.max_points) == (0, 0, 0)


def test_history_validates_before_any_rpc(sandbox: Sandbox, fake_rayd: RaydEndpoint) -> None:
    with pytest.raises(InvalidArgumentException, match="posterior"):
        sandbox.get_metrics_history(start=WINDOW_START, end=WINDOW_START - timedelta(seconds=1))
    with pytest.raises(InvalidArgumentException, match="max_points"):
        sandbox.get_metrics_history(max_points=0)
    with pytest.raises(InvalidArgumentException, match="start"):
        sandbox.get_metrics_history(start=datetime(1960, 1, 1, tzinfo=UTC))
    assert fake_rayd.servicer.history_requests == []


def test_history_on_a_pre_m9_agent_says_so(sandbox: Sandbox, fake_rayd: RaydEndpoint) -> None:
    fake_rayd.servicer.history_unimplemented = True
    with pytest.raises(UnimplementedError) as excinfo:
        sandbox.get_metrics_history()
    assert excinfo.value.feature == HISTORY_FEATURE
    assert excinfo.value.reason == HISTORY_UNIMPLEMENTED_REASON and "M9" in str(excinfo.value)
    assert not isinstance(excinfo.value, SandboxException)
    cause = excinfo.value.__cause__
    assert isinstance(cause, SandboxException)
    assert cause.grpc_code is grpc.StatusCode.UNIMPLEMENTED


def test_snapshot_get_metrics_is_unchanged_and_maps_mem_cache(
    sandbox: Sandbox, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.servicer.mem_cache_bytes = 4096
    snapshot = sandbox.get_metrics()
    assert snapshot.mem_cache_bytes == 4096
    assert fake_rayd.servicer.history_requests == []


def test_class_history_without_a_token_never_calls_aws(
    control_plane: StubbedControlPlane, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ACCESS_TOKEN_ENV_VAR, raising=False)
    with pytest.raises(AuthenticationException, match="access_token"):
        Sandbox.get_metrics_history(SANDBOX_ID, control_plane=control_plane.plane)


def test_class_history_validates_the_range_before_any_aws_call(
    control_plane: StubbedControlPlane,
) -> None:
    with pytest.raises(InvalidArgumentException, match="max_points"):
        Sandbox.get_metrics_history(
            SANDBOX_ID, access_token=ACCESS_TOKEN, max_points=-1, control_plane=control_plane.plane
        )
    with pytest.raises(InvalidArgumentException, match="sandbox_id"):
        Sandbox.get_metrics_history(
            "", access_token=ACCESS_TOKEN, control_plane=control_plane.plane
        )


@pytest.mark.parametrize("state", ["SUSPENDED", "SUSPENDING", "PENDING"])
def test_class_history_on_a_sleeping_sandbox_never_mints(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint, state: str
) -> None:
    transport = TrackingTransport.for_loopback()
    stub_class_call(control_plane, fake_rayd, state=state)
    with pytest.raises(SandboxStateException, match="connect"):
        Sandbox.get_metrics_history(
            SANDBOX_ID,
            access_token=ACCESS_TOKEN,
            control_plane=control_plane.plane,
            transport=transport,
        )
    assert transport.open_count == 0
    assert fake_rayd.servicer.history_requests == []


def test_class_history_on_a_terminated_sandbox_is_not_found(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    stub_class_call(control_plane, fake_rayd, state="TERMINATED")
    with pytest.raises(SandboxNotFoundException):
        Sandbox.get_metrics_history(
            SANDBOX_ID, access_token=ACCESS_TOKEN, control_plane=control_plane.plane
        )


def test_class_history_on_a_running_sandbox_uses_one_dedicated_channel(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.servicer.history = list(SEEDED_HISTORY)
    transport = TrackingTransport.for_loopback()
    stub_class_call(control_plane, fake_rayd, state="RUNNING")
    samples = Sandbox.get_metrics_history(
        SANDBOX_ID,
        access_token=ACCESS_TOKEN,
        start=WINDOW_START,
        max_points=3,
        control_plane=control_plane.plane,
        transport=transport,
    )
    assert [sample.mem_cache_bytes for sample in samples] == [11, 22, 33]
    assert len(fake_rayd.servicer.history_requests) == 1
    request = fake_rayd.servicer.history_requests[0]
    assert (request.start_unix_ms, request.end_unix_ms, request.max_points) == (
        WINDOW_START_MS,
        0,
        3,
    )
    assert fake_rayd.servicer.history_calls[0]["x-access-token"] == ACCESS_TOKEN
    assert fake_rayd.servicer.health_calls == []
    assert transport.open_count == 1 and transport.all_closed


def test_class_history_reads_the_token_from_the_environment(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ACCESS_TOKEN_ENV_VAR, ACCESS_TOKEN)
    transport = TrackingTransport.for_loopback()
    stub_class_call(control_plane, fake_rayd, state="RUNNING")
    assert (
        Sandbox.get_metrics_history(
            SANDBOX_ID, control_plane=control_plane.plane, transport=transport
        )
        == []
    )
    assert fake_rayd.servicer.history_calls[0]["x-access-token"] == ACCESS_TOKEN
    assert transport.all_closed


def test_class_history_with_a_wrong_token_is_unauthenticated(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    transport = TrackingTransport.for_loopback()
    stub_class_call(control_plane, fake_rayd, state="RUNNING")
    with pytest.raises(AuthenticationException):
        Sandbox.get_metrics_history(
            SANDBOX_ID,
            access_token=generate_access_token(),
            control_plane=control_plane.plane,
            transport=transport,
        )
    assert transport.all_closed


def test_class_history_on_a_pre_m9_agent_says_so(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.servicer.history_unimplemented = True
    stub_class_call(control_plane, fake_rayd, state="RUNNING")
    with pytest.raises(UnimplementedError) as excinfo:
        Sandbox.get_metrics_history(
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


def proxy_forbidden() -> FakeRpcError:
    return FakeRpcError(
        grpc.StatusCode.PERMISSION_DENIED,
        details=PROXY_FORBIDDEN_MARKER,
        debug=f'{{"grpc_status":7,"description":"{PROXY_FORBIDDEN_MARKER}"}}',
    )


def counting_refresher(minted: list[int]) -> TokenRefresher:
    def mint(_ports: object) -> str:
        minted.append(len(minted))
        return JWE

    refresher = TokenRefresher(TokenStore(), mint)
    refresher.mint((PortSpec.single(DEFAULT_PORT),))
    return refresher


def test_dedicated_call_remints_once_after_a_proxy_403() -> None:
    minted: list[int] = []
    outcomes: list[BaseException | str] = [proxy_forbidden(), "history"]

    def invoke(_stub: object) -> str:
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    assert invoke_reminting(invoke, object(), counting_refresher(minted)) == "history"
    assert len(minted) == 2 and outcomes == []


def test_dedicated_call_does_not_remint_on_other_errors() -> None:
    minted: list[int] = []

    def invoke(_stub: object) -> str:
        raise FakeRpcError(grpc.StatusCode.UNAUTHENTICATED, details="x-access-token")

    with pytest.raises(grpc.RpcError):
        invoke_reminting(invoke, object(), counting_refresher(minted))
    assert len(minted) == 1
