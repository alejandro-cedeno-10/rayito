"""La superficie E2B 2.51 del shim síncrono (`m9-e2b-v2-surface` D5-D12,
D14, D21 y los mapeos de los cambios hermanos) contra el `rayd` falso y el
plano de control con Stubber: cada `UnimplementedError` sin tocar nada, los
wrappers posicionales, la pausa booleana, `connect()` de instancia, las
opciones de conexión, el cliente `E2B`, la higiene de logs, el historial de
métricas, la paginación reanudable y las URLs de transferencia."""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import grpc
import pytest

from rayito import DownloadLink, FilesystemEvent, S3Staging, UploadTicket, WriteEntry
from rayito import Sandbox as NativeSandbox
from rayito._models import SandboxListItem
from rayito._sandbox_base import ReadinessPoll
from rayito._transport import translate_rpc_error
from rayito.e2b import (
    E2B,
    CommandHandle,
    ConnectionConfig,
    NotEnoughSpaceException,
    PtySize,
    Sandbox,
    SandboxException,
    SandboxPaginator,
    Secret,
    ServiceBusyException,
    Template,
    UnimplementedError,
    Volume,
    get_signature,
)
from rayito.e2b._compat import METRICS_HISTORY_IMAGE_REASON
from rayito.e2b._sync import Commands
from rayito.e2b._unimplemented import UNIMPLEMENTED_REASONS
from rayito.exceptions import InvalidArgumentException
from rayito.sandbox_sync.commands import Commands as NativeCommands
from rayito.v1 import health_pb2, lifecycle_pb2

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
    microvm_response,
    stub_metadata_probe,
)
from .fake_control_plane import FakeControlPlane
from .fake_s3 import BUCKET
from .log_capture import capture_logs
from .test_e2b_compat_sync import capture_launch
from .transfer_support import STAGING, start_transfer_rayd, stub_launch, transfer_files

HOME = "/home/user"
WAIT_SECONDS = 5.0
EVIDENCE = ("AWS_API_NOTES.md §", "SPEC.md §4", "RAYITO_ACCESS_TOKEN", "rayito-base-poly")
TICKET_TIMEOUT = 5.0


@pytest.fixture(autouse=True)
def no_integration() -> Iterator[None]:
    ConnectionConfig.set_integration(None)
    yield
    ConnectionConfig.set_integration(None)


def create_on(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint, **kwargs: Any
) -> Sandbox:
    capture_launch(control_plane, fake_rayd)
    return Sandbox.create(
        IMAGE_ARN,
        access_token=ACCESS_TOKEN,
        control_plane=control_plane.plane,
        transport=fake_rayd.transport,
        **kwargs,
    )


@pytest.fixture
def sbx(control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint) -> Iterator[Sandbox]:
    fake_rayd.servicer.metadata = {"a": "1"}
    sandbox = create_on(control_plane, fake_rayd, metadata={"a": "1"})
    try:
        yield sandbox
    finally:
        sandbox.native.close()


def wait_until(predicate: Callable[[], bool]) -> None:
    deadline = time.monotonic() + WAIT_SECONDS
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(0.02)


# --------------------------------------------------------- unimplemented


def unimplemented_triggers(sandbox: Sandbox) -> dict[str, Callable[[], object]]:
    return {
        "fork": lambda: sandbox.fork(),
        "Sandbox.fork": lambda: Sandbox.fork(SANDBOX_ID),
        "create_snapshot": lambda: sandbox.create_snapshot(),
        "list_snapshots": lambda: sandbox.list_snapshots(),
        "Sandbox.list_snapshots": lambda: Sandbox.list_snapshots(sandbox_id=SANDBOX_ID),
        "Sandbox.delete_snapshot": lambda: Sandbox.delete_snapshot("snap-1"),
        "connect(on_resume='reboot')": lambda: sandbox.connect(on_resume="reboot"),
        "Sandbox.connect(on_resume='reboot')": lambda: Sandbox.connect(
            SANDBOX_ID, on_resume="reboot"
        ),
        "pause(keep_memory=False)": lambda: sandbox.pause(keep_memory=False),
        "Sandbox.pause(keep_memory=False)": lambda: Sandbox.pause(SANDBOX_ID, keep_memory=False),
        "lifecycle keep_memory": lambda: Sandbox.create(
            lifecycle={"on_timeout": {"action": "pause", "keep_memory": False}}
        ),
        "network.rules": lambda: Sandbox.create(network={"rules": {}}),
        "network.mask_request_host": lambda: Sandbox.create(network={"mask_request_host": "h"}),
        "network.allow_public_traffic": lambda: Sandbox.create(
            network={"allow_public_traffic": True}
        ),
        "iam": lambda: Sandbox.create(iam={"aws": {}}),
        "mcp": lambda: Sandbox.create(mcp={"x": {}}),
        "beta_create(mcp=)": lambda: Sandbox.beta_create(mcp={"x": {}}),
        "get_mcp_url": lambda: sandbox.get_mcp_url(),
        "get_mcp_token": lambda: sandbox.get_mcp_token(),
        "volume_mounts": lambda: Sandbox.create(volume_mounts={"/data": "vol"}),
        "get_signature": lambda: get_signature("/home/user/a", "write"),
        "Template.build": lambda: Template.build("tpl", alias="x"),
        "Volume.create": lambda: Volume.create("vol"),
        "Secret.list": lambda: Secret.list(),
        "E2B().Template": lambda: E2B().Template,
        "E2B().AsyncTemplate": lambda: E2B().AsyncTemplate,
        "E2B().Volume": lambda: E2B().Volume,
        "E2B().AsyncVolume": lambda: E2B().AsyncVolume,
        "E2B().Secret": lambda: E2B().Secret,
        "E2B().AsyncSecret": lambda: E2B().AsyncSecret,
    }


def trigger_names() -> list[str]:
    return list(unimplemented_triggers(Sandbox.__new__(Sandbox)))


@pytest.mark.parametrize("trigger", trigger_names())
def test_every_d14_trigger_raises_before_any_request(
    sbx: Sandbox, fake_rayd: RaydEndpoint, trigger: str
) -> None:
    health_calls = len(fake_rayd.servicer.health_calls)
    executions = len(fake_rayd.code.executions)
    with pytest.raises(UnimplementedError) as excinfo:
        unimplemented_triggers(sbx)[trigger]()
    error = excinfo.value
    assert isinstance(error, NotImplementedError)
    assert not isinstance(error, SandboxException)
    assert error.reason == UNIMPLEMENTED_REASONS[error.feature]
    assert any(evidence in error.reason for evidence in EVIDENCE)
    assert len(fake_rayd.servicer.health_calls) == health_calls
    assert len(fake_rayd.code.executions) == executions
    assert fake_rayd.lifecycle.requests == []


def test_neutral_values_are_accepted(
    sbx: Sandbox, control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    control_plane.microvms.add_response(
        "get_microvm", microvm_response(endpoint=fake_rayd.host, state="SUSPENDED")
    )
    assert sbx.pause(keep_memory=True) is False
    control_plane.microvms.add_response(
        "get_microvm", microvm_response(endpoint=fake_rayd.host, state="RUNNING")
    )
    assert sbx.connect(on_resume="restore") is sbx


# ---------------------------------------------------------------- commands


def test_positional_background_run_with_wait_callbacks(sbx: Sandbox) -> None:
    handle = sbx.commands.run("echo hi", True)
    assert isinstance(handle, CommandHandle)
    chunks: list[str] = []
    result = handle.wait(on_stdout=chunks.append)
    assert chunks == ["hi\n"] and result.exit_code == 0


def test_positional_envs_reach_the_wire(sbx: Sandbox, fake_rayd: RaydEndpoint) -> None:
    result = sbx.commands.run("env", False, {"K": "1"})
    assert not isinstance(result, CommandHandle)
    assert dict(fake_rayd.process.start_requests[-1].process.envs)["K"] == "1"
    assert sbx.commands.native is sbx.native.commands
    assert isinstance(sbx.commands.list(), list)


# ------------------------------------------------------------------ files


def test_write_files_returns_one_write_info_per_file(sbx: Sandbox) -> None:
    written = sbx.files.write_files(
        [WriteEntry(f"{HOME}/c.txt", "z"), WriteEntry(f"{HOME}/d.txt", "w")]
    )
    assert [entry.path for entry in written] == [f"{HOME}/c.txt", f"{HOME}/d.txt"]
    assert len(sbx.files.write([WriteEntry(f"{HOME}/b.txt", "y")])) == 1
    assert sbx.files.read(f"{HOME}/d.txt") == "w"


def test_sync_watch_dir_2x_signature(sbx: Sandbox, fake_rayd: RaydEndpoint) -> None:
    sbx.files.make_dir(f"{HOME}/w")
    handle = sbx.files.watch_dir(f"{HOME}/w", include_entry=True, allow_network_mounts=True)
    assert fake_rayd.filesystem.watch_requests[-1].include_entry is True
    handle.stop()
    with pytest.raises(InvalidArgumentException, match="on_event"):
        sbx.files.watch_dir(f"{HOME}/w", lambda event: None)  # type: ignore[arg-type]
    seen: list[FilesystemEvent] = []
    keyword = sbx.files.watch_dir(f"{HOME}/w", on_event=seen.append)
    keyword.stop()


# -------------------------------------------------------------------- pty


def test_pty_create_then_connect_reattaches(sbx: Sandbox, fake_rayd: RaydEndpoint) -> None:
    terminal = sbx.pty.create(PtySize(24, 80), timeout=None)
    started = fake_rayd.pty.ptys[terminal.pid]
    assert (started.cols, started.rows) == (80, 24)
    sbx.pty.resize(terminal.pid, PtySize(rows=40, cols=120))
    again = sbx.pty.connect(terminal.pid, timeout=None)
    assert again.pid == terminal.pid
    assert fake_rayd.pty.connect_requests[-1].pid == terminal.pid
    with pytest.raises(InvalidArgumentException, match="on_data"):
        sbx.pty.create(PtySize(), print)  # type: ignore[arg-type]
    assert sbx.pty.kill(terminal.pid) is True


# ------------------------------------------------------------------ code


def test_code_context_lifecycle_through_the_shim(sbx: Sandbox, fake_rayd: RaydEndpoint) -> None:
    context = sbx.create_code_context()
    assert context.id in {item.id for item in sbx.list_code_contexts()}
    sbx.restart_code_context(context)
    sbx.remove_code_context(context.id)
    assert fake_rayd.code.restart_requests[-1] == context.id
    assert fake_rayd.code.destroy_requests[-1] == context.id


# -------------------------------------------------------------- lifecycle


def test_pause_returns_a_bool(
    sbx: Sandbox, control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    control_plane.microvms.add_response(
        "get_microvm", microvm_response(endpoint=fake_rayd.host, state="RUNNING")
    )
    control_plane.microvms.add_response("suspend_microvm", {}, {"microvmIdentifier": SANDBOX_ID})
    control_plane.microvms.add_response(
        "get_microvm", microvm_response(endpoint=fake_rayd.host, state="SUSPENDED")
    )
    assert sbx.pause() is True
    control_plane.microvms.add_response(
        "get_microvm", microvm_response(endpoint=fake_rayd.host, state="SUSPENDED")
    )
    assert sbx.beta_pause() is False


def test_class_pause_returns_a_bool(control_plane: StubbedControlPlane) -> None:
    control_plane.microvms.add_response("get_microvm", microvm_response(state="SUSPENDED"))
    assert Sandbox.pause(SANDBOX_ID, control_plane=control_plane.plane) is False


def test_instance_connect_resumes_and_extends(
    sbx: Sandbox, control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    control_plane.microvms.add_response(
        "get_microvm",
        microvm_response(
            endpoint=fake_rayd.host,
            state="SUSPENDED",
            idle={
                "maxIdleDurationSeconds": 60,
                "suspendedDurationSeconds": 0,
                "autoResumeEnabled": False,
            },
        ),
    )
    control_plane.microvms.add_response("resume_microvm", {}, {"microvmIdentifier": SANDBOX_ID})
    control_plane.microvms.add_response("create_microvm_auth_token", auth_token_response())
    native = sbx.native
    assert sbx.connect() is sbx
    assert sbx.native is native
    control_plane.microvms.add_response(
        "get_microvm", microvm_response(endpoint=fake_rayd.host, state="RUNNING")
    )
    assert sbx.connect(timeout=120) is sbx
    request = fake_rayd.lifecycle.requests[-1]
    assert (request.timeout_ms, request.mode) == (120_000, lifecycle_pb2.TIMEOUT_MODE_AT_LEAST)


def test_is_running_bounds_the_health_deadline(
    sbx: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El plazo se verifica en el stub y no en `time_remaining()` del
    servidor: gRPC redondea el deadline al milisegundo hacia arriba y un
    `Health` real de 0.5 s puede caducar con el GIL ocupado."""
    timeouts: list[float | None] = []

    def health(
        request: health_pb2.HealthRequest, timeout: float | None = None
    ) -> health_pb2.HealthResponse:
        timeouts.append(timeout)
        return health_pb2.HealthResponse(agent_ready=True)

    monkeypatch.setattr(sbx.native._health, "Health", health)
    assert sbx.is_running(request_timeout=0.5) is True
    assert sbx.is_running() is True
    assert timeouts == [0.5, ReadinessPoll.MAX_RPC_TIMEOUT]


def test_endpoint_accessors_and_git(sbx: Sandbox, fake_rayd: RaydEndpoint) -> None:
    assert sbx.envd_api_url == f"https://{fake_rayd.host}"
    assert sbx.envd_direct_url == sbx.envd_api_url
    assert sbx.traffic_access_token == JWE
    assert sbx.git is sbx.native.git


def test_info_shapes(
    sbx: Sandbox, control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.servicer.cpu_count = 2
    fake_rayd.servicer.memory_total_bytes = 2048 * 1024 * 1024
    control_plane.microvms.add_response(
        "get_microvm", microvm_response(endpoint=fake_rayd.host, state="RUNNING")
    )
    info = sbx.get_info()
    deadline = fake_rayd.servicer.lifecycle.deadline_unix_ms
    assert info.end_at == datetime.fromtimestamp(deadline / 1000, tz=UTC)
    assert info.sandbox_domain == fake_rayd.host
    assert info.envd_version == "test"
    assert info.metadata == {"a": "1"}
    assert info.lifecycle == {"on_timeout": "kill", "auto_resume": False}
    assert info.network == {"allow_out": [], "deny_out": []}
    assert info.allow_internet_access is True
    assert info.volume_mounts == []
    assert info.name == "rayito-base-2gb"
    metrics = sbx.get_metrics()
    assert len(metrics) == 1
    assert metrics[0].mem_total == 2 * 1024**3 and metrics[0].cpu_count == 1


def test_terminated_sandbox_is_not_found(control_plane: StubbedControlPlane) -> None:
    control_plane.microvms.add_response("get_microvm", microvm_response(state="TERMINATED"))
    with pytest.raises(SandboxException):
        Sandbox.get_info(SANDBOX_ID, control_plane=control_plane.plane)


# ---------------------------------------------------------------- metrics


def history_sample(offset_s: int, mem_cache: int) -> health_pb2.MetricsResponse:
    base = int(datetime(2026, 9, 23, 12, 0, tzinfo=UTC).timestamp() * 1000)
    return health_pb2.MetricsResponse(
        cpu_used_pct=1.0,
        mem_used_bytes=10,
        mem_total_bytes=20,
        disk_used_bytes=30,
        disk_total_bytes=40,
        cpu_count=1,
        timestamp_unix_ms=base + offset_s * 1000,
        mem_cache_bytes=mem_cache,
    )


def test_metrics_series_through_the_shim(
    sbx: Sandbox, control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.servicer.history = [history_sample(0, 7), history_sample(5, 8), history_sample(10, 9)]
    assert [m.mem_cache for m in sbx.get_metrics()] == [7, 8, 9]
    start = datetime(2026, 9, 23, 11, 0, tzinfo=UTC)
    end = start + timedelta(hours=2)
    ranged = sbx.get_metrics(start=start, end=end)
    assert [m.mem_cache for m in ranged] == [7, 8, 9]
    request = fake_rayd.servicer.history_requests[-1]
    assert request.start_unix_ms == int(start.timestamp() * 1000)
    assert request.end_unix_ms == int(end.timestamp() * 1000)
    transport = TrackingTransport.for_loopback()
    stub_metadata_probe(control_plane, SANDBOX_ID, fake_rayd)
    calls = len(fake_rayd.servicer.history_calls)
    classed = Sandbox.get_metrics(
        sbx.sandbox_id,
        access_token=ACCESS_TOKEN,
        control_plane=control_plane.plane,
        transport=transport,
    )
    assert [m.mem_cache for m in classed] == [7, 8, 9]
    assert len(fake_rayd.servicer.history_calls) == calls + 1
    assert "x-access-token" in fake_rayd.servicer.history_calls[-1]
    assert transport.all_closed


def test_metrics_on_an_old_agent(sbx: Sandbox, fake_rayd: RaydEndpoint) -> None:
    fake_rayd.servicer.history_unimplemented = True
    assert len(sbx.get_metrics()) == 1
    with pytest.raises(UnimplementedError) as excinfo:
        sbx.get_metrics(start=datetime(2026, 9, 23, tzinfo=UTC))
    assert excinfo.value.feature == "get_metrics(start=, end=)"
    assert "M9" in excinfo.value.reason


def test_class_metrics_on_an_old_agent(
    sbx: Sandbox, control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.servicer.history_unimplemented = True
    stub_metadata_probe(control_plane, SANDBOX_ID, fake_rayd)
    with pytest.raises(UnimplementedError) as excinfo:
        Sandbox.get_metrics(
            sbx.sandbox_id,
            access_token=ACCESS_TOKEN,
            control_plane=control_plane.plane,
            transport=TrackingTransport.for_loopback(),
        )
    assert excinfo.value.feature == "Sandbox.get_metrics(sandbox_id)"
    assert excinfo.value.reason == METRICS_HISTORY_IMAGE_REASON
    assert isinstance(excinfo.value.__cause__, UnimplementedError)


def test_class_metrics_without_a_token_makes_no_call(
    control_plane: StubbedControlPlane, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("RAYITO_ACCESS_TOKEN", raising=False)
    with pytest.raises(UnimplementedError) as excinfo:
        Sandbox.get_metrics(SANDBOX_ID, control_plane=control_plane.plane)
    assert "access_token" in excinfo.value.reason
    assert "RAYITO_ACCESS_TOKEN" in excinfo.value.reason


# ---------------------------------------------------------------- listing


def listed(sandbox_id: str) -> SandboxListItem:
    return SandboxListItem(
        sandbox_id=sandbox_id,
        state="RUNNING",
        template=IMAGE_ARN,
        template_version="1.0",
        started_at=datetime(2026, 9, 22, 12, 0, tzinfo=UTC),
    )


def test_list_resumes_with_next_token_and_orders() -> None:
    plane = FakeControlPlane()
    try:
        plane.script_pages([listed("a"), listed("b"), listed("c")])
        first = Sandbox.list(limit=1, control_plane=plane)
        assert isinstance(first, SandboxPaginator)
        assert [item.sandbox_id for item in first.next_items()] == ["a"]
        token = first.next_token
        assert token is not None and first.has_next
        resumed = Sandbox.list(None, 1, token, control_plane=plane)
        assert [item.sandbox_id for item in resumed.next_items()] == ["b"]
        ascending = Sandbox.list(order="asc", control_plane=plane)
        assert [item.sandbox_id for item in ascending.next_items()] == ["a", "b", "c"]
        descending = Sandbox.list(order="desc", control_plane=plane)
        assert [item.sandbox_id for item in descending.next_items()] == ["c", "b", "a"]
    finally:
        plane.close()


# -------------------------------------------------------- connection options


def test_headers_reach_health_unary_and_stream(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    sandbox = create_on(control_plane, fake_rayd, headers={"X-Trace": "1"})
    try:
        assert fake_rayd.servicer.health_calls[-1]["x-trace"] == "1"
        sandbox.files.make_dir(f"{HOME}/h")
        assert fake_rayd.filesystem.metadata["MakeDir"][-1]["x-trace"] == "1"
        sandbox.commands.run("echo hi")
        assert fake_rayd.process.start_metadata[-1]["x-trace"] == "1"
        assert dict(sandbox.connection_config.headers) == {"x-trace": "1"}
    finally:
        sandbox.native.close()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"headers": {"x-access-token": "x"}},
        {"headers": {"X-AWS-Proxy-Port": "1"}},
        {"proxy": "https://p:1"},
        {"retries": -1},
    ],
)
def test_bad_connection_kwargs_are_refused_before_run_microvm(
    control_plane: StubbedControlPlane, kwargs: dict[str, Any]
) -> None:
    with pytest.raises(InvalidArgumentException) as excinfo:
        Sandbox.create(IMAGE_ARN, control_plane=control_plane.plane, **kwargs)
    assert "'x'" not in str(excinfo.value) and "p:1" not in str(excinfo.value)


class CreateCaptured(Exception):
    """Corta `rayito.Sandbox.create` tras capturar sus kwargs."""


def capture_native_create(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def fake_create(**kwargs: Any) -> Any:
        captured.update(kwargs)
        raise CreateCaptured

    monkeypatch.setattr(NativeSandbox, "create", fake_create)
    return captured


def test_proxy_and_retries_reach_the_transport_and_the_plane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = capture_native_create(monkeypatch)
    ConnectionConfig.set_integration("acme/1.0")
    with pytest.raises(CreateCaptured):
        Sandbox.create(
            IMAGE_ARN,
            region="us-east-1",
            headers={"x-trace": "1"},
            proxy="http://127.0.0.1:3128",
            retries=2,
        )
    transport = captured["transport"]
    assert transport.http_proxy == "http://127.0.0.1:3128"
    assert transport.extra_metadata == (("x-trace", "1"),)
    config = captured["control_plane"]._client.meta.config
    assert config.retries["total_max_attempts"] == 3
    assert config.proxies == {"http": "http://127.0.0.1:3128", "https": "http://127.0.0.1:3128"}
    assert config.user_agent_extra.endswith("acme/1.0")


def test_set_integration_reaches_the_connection_config(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    ConnectionConfig.set_integration("acme/1.0")
    with pytest.raises(InvalidArgumentException, match="control_plane"):
        Sandbox.create(IMAGE_ARN, control_plane=control_plane.plane)
    ConnectionConfig.set_integration(None)
    sandbox = create_on(control_plane, fake_rayd, retries=None)
    try:
        assert sandbox.connection_config.integration is None
        assert sandbox.connection_config.region == "us-east-1"
        assert sandbox.connection_config.request_timeout == 60.0
    finally:
        sandbox.native.close()


def test_capacity_and_disk_errors_use_the_e2b_names(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.servicer.lifecycle = None
    control_plane.microvms.add_client_error(
        "run_microvm",
        service_error_code="InsufficientCapacityException",
        http_status_code=500,
    )
    with pytest.raises(ServiceBusyException):
        Sandbox.create(IMAGE_ARN, access_token=ACCESS_TOKEN, control_plane=control_plane.plane)
    disk = translate_rpc_error(
        FakeRpcError(grpc.StatusCode.RESOURCE_EXHAUSTED, details="disk_reserve"), filesystem=True
    )
    assert isinstance(disk, NotEnoughSpaceException)


# ------------------------------------------------------------------ logger


def test_logger_receives_the_sdk_records(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.servicer.unavailable_calls = 1
    app = logging.getLogger("tests.e2b.app")
    with capture_logs("tests.e2b.app") as mine, capture_logs("rayito.sandbox") as default:
        sandbox = create_on(control_plane, fake_rayd, logger=app)
        sandbox.native.close()
    messages = mine.messages()
    assert f"run-microvm aceptado: {SANDBOX_ID} (PENDING)" in messages
    assert any("Health aún no alcanzable" in message for message in messages)
    assert default.messages() == []
    assert sandbox.connection_config.logger is app


def test_secrets_never_reach_the_logs(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = logging.getLogger("tests.e2b.hygiene")
    with capture_logs("rayito") as sdk, capture_logs("tests.e2b.hygiene") as mine:
        sandbox = create_on(
            control_plane, fake_rayd, headers={"x-trace": "valor-secreto"}, logger=app
        )
        with contextlib.suppress(SandboxException):
            sandbox.git.clone("https://u:clave@example.com/org/repo.git", path=f"{HOME}/repo")
        sandbox.native.close()
        captured = capture_native_create(monkeypatch)
        with pytest.raises(CreateCaptured):
            Sandbox.create(
                IMAGE_ARN,
                region="us-east-1",
                proxy="http://u:clave@127.0.0.1:3128",
                logger=app,
            )
        assert captured["transport"].http_proxy == "http://u:clave@127.0.0.1:3128"
    text = sdk.text() + "\n" + mine.text()
    for secret in ("valor-secreto", "clave", "u:clave"):
        assert secret not in text


# -------------------------------------------------------------- E2B client


def test_bound_params_are_used(control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint) -> None:
    client = E2B(region="us-east-1", control_plane=control_plane.plane)
    capture_launch(control_plane, fake_rayd)
    sandbox = client.Sandbox.create(
        IMAGE_ARN, access_token=ACCESS_TOKEN, transport=fake_rayd.transport
    )
    assert isinstance(sandbox, client.Sandbox) and isinstance(sandbox, Sandbox)
    sandbox.native.close()
    transport = TrackingTransport.for_loopback()
    stub_metadata_probe(control_plane, SANDBOX_ID, fake_rayd)
    info = client.Sandbox.get_info(SANDBOX_ID, region="us-east-1", transport=transport)
    assert info.sandbox_id == SANDBOX_ID
    assert Sandbox._bound_params == {}


def test_two_clients_are_isolated(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    first = E2B(control_plane=control_plane.plane, headers={"x-client": "uno"})
    second = E2B(control_plane=control_plane.plane, headers={"x-client": "dos"})
    capture_launch(control_plane, fake_rayd)
    one = first.Sandbox.create(IMAGE_ARN, access_token=ACCESS_TOKEN, transport=fake_rayd.transport)
    assert fake_rayd.servicer.health_calls[-1]["x-client"] == "uno"
    one.native.close()
    capture_launch(control_plane, fake_rayd)
    two = second.Sandbox.create(IMAGE_ARN, access_token=ACCESS_TOKEN, transport=fake_rayd.transport)
    assert fake_rayd.servicer.health_calls[-1]["x-client"] == "dos"
    two.native.close()
    assert dict(Sandbox._bound_params) == {}
    assert first.Sandbox is not second.Sandbox


def test_client_warns_once_for_ignored_params() -> None:
    with pytest.warns(Warning) as caught:
        client = E2B(api_key="e2b_secreto")
    assert [str(w.message).split(" ")[0] for w in caught] == ["api_key"]
    assert "api_key" not in client.Sandbox._bound_params


# ---------------------------------------------------------------- transfer


def test_upload_and_download_urls_through_the_shim(control_plane: StubbedControlPlane) -> None:
    server, endpoint = start_transfer_rayd()
    try:
        stub_launch(control_plane, endpoint)
        fake = transfer_files(endpoint)
        native = NativeSandbox.create(
            IMAGE_ARN,
            idle=None,
            access_token=ACCESS_TOKEN,
            control_plane=control_plane.plane,
            transport=endpoint.transport,
            session=fake.s3.session(),
            transfer=STAGING,
        )
        sandbox = Sandbox(_native=native)
        ticket = sandbox.upload_url("e2b.bin", use_signature=True)
        assert isinstance(ticket, UploadTicket) and isinstance(ticket, str)
        assert BUCKET in ticket.url or ticket.startswith("https://")
        fake.s3.put_url(ticket, b"hola")
        assert ticket.wait(timeout=TICKET_TIMEOUT).size == 4
        link = sandbox.download_url("e2b.bin", use_signature_expiration=600)
        assert isinstance(link, DownloadLink)
        for method in (sandbox.upload_url, sandbox.download_url):
            with pytest.raises(InvalidArgumentException, match="use_signature_expiration"):
                method("e2b.bin", use_signature_expiration=-1)
        with pytest.raises(InvalidArgumentException, match="ruta de destino"):
            sandbox.upload_url()
        native.close()
    finally:
        server.stop(grace=None)


def test_upload_url_without_staging_is_the_native_unimplemented(sbx: Sandbox) -> None:
    assert isinstance(sbx.native.transfer, S3Staging | type(None))
    with pytest.raises(UnimplementedError, match="RAYITO_TRANSFER_BUCKET"):
        sbx.upload_url("/x")


def test_files_forward_the_transfer_keywords(sbx: Sandbox, monkeypatch: pytest.MonkeyPatch) -> None:
    """`files.write`/`write_files`/`read` del shim pasan `gzip`, `metadata`,
    `use_octet_stream` y `stream_idle_timeout` al nativo (m9-file-transfer D14)."""
    calls: list[tuple[str, dict[str, Any]]] = []

    def spy(name: str) -> Callable[..., object]:
        def record(*args: object, **kwargs: Any) -> object:
            calls.append((name, kwargs))
            return [] if name == "write_files" else "hola"

        return record

    for name in ("write", "write_files", "read"):
        monkeypatch.setattr(sbx.files._native, name, spy(name))
    sbx.files.write(f"{HOME}/a", "x", gzip=True, metadata={"k": "v"}, use_octet_stream=True)
    sbx.files.write([WriteEntry(f"{HOME}/b", "y")], gzip=True, metadata={"k": "w"})
    sbx.files.read(f"{HOME}/a", "bytes", gzip=True, stream_idle_timeout=7)
    write, many, read = (kwargs for _, kwargs in calls)
    assert (write["gzip"], write["metadata"], write["use_octet_stream"]) == (True, {"k": "v"}, True)
    assert (many["gzip"], many["metadata"], many["use_octet_stream"]) == (True, {"k": "w"}, False)
    assert (read["format"], read["gzip"], read["stream_idle_timeout"]) == ("bytes", True, 7)


class RecordingCommands:
    """Un `Commands` nativo que sólo anota lo que el shim le pasa."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def run(self, *args: Any, **kwargs: Any) -> str:
        self.calls.append(("run", args, kwargs))
        return "ran"

    def connect(self, *args: Any, **kwargs: Any) -> str:
        self.calls.append(("connect", args, kwargs))
        return "attached"


def test_commands_forward_callbacks_and_the_connect_timeout() -> None:
    native = RecordingCommands()
    commands = Commands(cast(NativeCommands, native))

    def on_stdout(chunk: str) -> None:
        del chunk

    ran: object = commands.run("echo hi", on_stdout=on_stdout)
    attached: object = commands.connect(42, 7, 3)
    assert (ran, attached) == ("ran", "attached")
    (_, run_args, run_kwargs), (_, connect_args, connect_kwargs) = native.calls
    assert run_args == ("echo hi",) and run_kwargs["on_stdout"] is on_stdout
    assert connect_args == (42,)
    assert (connect_kwargs["timeout"], connect_kwargs["request_timeout"]) == (7, 3)
