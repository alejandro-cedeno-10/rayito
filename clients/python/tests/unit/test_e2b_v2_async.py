"""La superficie E2B 2.51 del shim asíncrono: los mismos casos que
`test_e2b_v2_sync.py` con `await`, más los órdenes asíncronos de E2B para
`watch_dir`, `pty.create`/`pty.connect` y `commands.connect`."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from rayito import AsyncCommandHandle, FilesystemEvent, WriteEntry
from rayito import AsyncSandbox as NativeAsyncSandbox
from rayito._models import SandboxListItem
from rayito._sandbox_base import ReadinessPoll
from rayito.e2b import (
    E2B,
    AsyncSandbox,
    AsyncSandboxPaginator,
    ConnectionConfig,
    PtySize,
    SandboxException,
    UnimplementedError,
)
from rayito.e2b._async import AsyncCommands
from rayito.e2b._compat import METRICS_HISTORY_IMAGE_REASON
from rayito.e2b._unimplemented import UNIMPLEMENTED_REASONS
from rayito.exceptions import InvalidArgumentException
from rayito.sandbox_async.commands import AsyncCommands as NativeAsyncCommands
from rayito.v1 import health_pb2, lifecycle_pb2

from .conftest import (
    ACCESS_TOKEN,
    IMAGE_ARN,
    JWE,
    SANDBOX_ID,
    RaydEndpoint,
    StubbedControlPlane,
    TrackingTransport,
    auth_token_response,
    microvm_response,
    stub_metadata_probe,
)
from .fake_control_plane import FakeControlPlane
from .log_capture import capture_logs
from .test_e2b_compat_sync import capture_launch
from .test_e2b_v2_sync import EVIDENCE, CreateCaptured, history_sample

HOME = "/home/user"
WAIT_SECONDS = 5.0


@pytest.fixture(autouse=True)
def no_integration() -> Iterator[None]:
    ConnectionConfig.set_integration(None)
    yield
    ConnectionConfig.set_integration(None)


async def create_on(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint, **kwargs: Any
) -> AsyncSandbox:
    capture_launch(control_plane, fake_rayd)
    return await AsyncSandbox.create(
        IMAGE_ARN,
        access_token=ACCESS_TOKEN,
        control_plane=control_plane.plane,
        transport=fake_rayd.transport,
        **kwargs,
    )


@pytest.fixture
async def sbx(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> AsyncIterator[AsyncSandbox]:
    fake_rayd.servicer.metadata = {"a": "1"}
    sandbox = await create_on(control_plane, fake_rayd, metadata={"a": "1"})
    try:
        yield sandbox
    finally:
        await sandbox.native.close()


def unimplemented_triggers(sandbox: AsyncSandbox) -> dict[str, Callable[[], Awaitable[object]]]:
    async def call(function: Callable[[], object]) -> object:
        result = function()
        if asyncio.iscoroutine(result):
            return await result
        return result

    return {
        "fork": lambda: call(sandbox.fork),
        "AsyncSandbox.fork": lambda: call(lambda: AsyncSandbox.fork(SANDBOX_ID)),
        "create_snapshot": lambda: call(sandbox.create_snapshot),
        "list_snapshots": lambda: call(sandbox.list_snapshots),
        "AsyncSandbox.delete_snapshot": lambda: call(lambda: AsyncSandbox.delete_snapshot("s")),
        "connect(on_resume='reboot')": lambda: sandbox.connect(on_resume="reboot"),
        "AsyncSandbox.connect(on_resume='reboot')": lambda: AsyncSandbox.connect(
            SANDBOX_ID, on_resume="reboot"
        ),
        "pause(keep_memory=False)": lambda: sandbox.pause(keep_memory=False),
        "AsyncSandbox.pause(keep_memory=False)": lambda: AsyncSandbox.pause(
            SANDBOX_ID, keep_memory=False
        ),
        "lifecycle keep_memory": lambda: AsyncSandbox.create(
            lifecycle={"on_timeout": {"action": "pause", "keep_memory": False}}
        ),
        "network.rules": lambda: AsyncSandbox.create(network={"rules": {}}),
        "network.mask_request_host": lambda: AsyncSandbox.create(
            network={"mask_request_host": "h"}
        ),
        "network.allow_public_traffic": lambda: AsyncSandbox.create(
            network={"allow_public_traffic": True}
        ),
        "iam": lambda: AsyncSandbox.create(iam={"aws": {}}),
        "mcp": lambda: AsyncSandbox.create(mcp={"x": {}}),
        "beta_create(mcp=)": lambda: AsyncSandbox.beta_create(mcp={"x": {}}),
        "get_mcp_url": lambda: call(sandbox.get_mcp_url),
        "get_mcp_token": lambda: call(sandbox.get_mcp_token),
        "volume_mounts": lambda: AsyncSandbox.create(volume_mounts={"/data": "vol"}),
        "E2B().AsyncTemplate": lambda: call(lambda: E2B().AsyncTemplate),
    }


def trigger_names() -> list[str]:
    return list(unimplemented_triggers(AsyncSandbox.__new__(AsyncSandbox)))


@pytest.mark.parametrize("trigger", trigger_names())
async def test_async_every_d14_trigger_raises_before_any_request(
    sbx: AsyncSandbox, fake_rayd: RaydEndpoint, trigger: str
) -> None:
    health_calls = len(fake_rayd.servicer.health_calls)
    with pytest.raises(UnimplementedError) as excinfo:
        await unimplemented_triggers(sbx)[trigger]()
    error = excinfo.value
    assert not isinstance(error, SandboxException)
    assert error.reason == UNIMPLEMENTED_REASONS[error.feature]
    assert any(evidence in error.reason for evidence in EVIDENCE)
    assert len(fake_rayd.servicer.health_calls) == health_calls
    assert fake_rayd.lifecycle.requests == []


async def test_async_positional_commands_and_wait_callbacks(
    sbx: AsyncSandbox, fake_rayd: RaydEndpoint
) -> None:
    handle = await sbx.commands.run("echo hi", True)
    assert isinstance(handle, AsyncCommandHandle)
    chunks: list[str] = []

    async def collect(chunk: str) -> None:
        chunks.append(chunk)

    result = await handle.wait(on_stdout=collect)
    assert chunks == ["hi\n"] and result.exit_code == 0
    await sbx.commands.run("env", False, {"K": "1"})
    assert dict(fake_rayd.process.start_requests[-1].process.envs)["K"] == "1"
    background = await sbx.commands.run("sleep 0.2", True)
    assert isinstance(background, AsyncCommandHandle)
    attached = await sbx.commands.connect(background.pid, 5, None, None, None)
    assert attached.pid == background.pid
    assert (await background.wait()).exit_code == 0


async def test_async_files_and_watch_order(sbx: AsyncSandbox, fake_rayd: RaydEndpoint) -> None:
    written = await sbx.files.write_files(
        [WriteEntry(f"{HOME}/c.txt", "z"), WriteEntry(f"{HOME}/d.txt", "w")]
    )
    assert len(written) == 2
    await sbx.files.make_dir(f"{HOME}/w")
    seen: list[FilesystemEvent] = []
    watch = await sbx.files.watch_dir(f"{HOME}/w", seen.append, None, None, None, 0, False, True)
    assert fake_rayd.filesystem.watch_requests[-1].include_entry is True
    await watch.stop()


async def test_async_pty_create_and_connect_take_on_data(
    sbx: AsyncSandbox, fake_rayd: RaydEndpoint
) -> None:
    terminal = await sbx.pty.create(PtySize(24, 80), None, timeout=None)
    started = fake_rayd.pty.ptys[terminal.pid]
    assert (started.cols, started.rows) == (80, 24)
    again = await sbx.pty.connect(terminal.pid, None, None)
    assert again.pid == terminal.pid
    assert fake_rayd.pty.connect_requests[-1].pid == terminal.pid
    assert await sbx.pty.kill(terminal.pid) is True


async def test_async_code_contexts(sbx: AsyncSandbox, fake_rayd: RaydEndpoint) -> None:
    context = await sbx.create_code_context()
    assert context.id in {item.id for item in await sbx.list_code_contexts()}
    await sbx.restart_code_context(context)
    await sbx.remove_code_context(context.id)
    assert fake_rayd.code.destroy_requests[-1] == context.id


async def test_async_pause_bool_and_instance_connect(
    sbx: AsyncSandbox, control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    control_plane.microvms.add_response(
        "get_microvm", microvm_response(endpoint=fake_rayd.host, state="RUNNING")
    )
    control_plane.microvms.add_response("suspend_microvm", {}, {"microvmIdentifier": SANDBOX_ID})
    control_plane.microvms.add_response(
        "get_microvm", microvm_response(endpoint=fake_rayd.host, state="SUSPENDED")
    )
    assert await sbx.pause() is True
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
    assert await sbx.connect() is sbx
    control_plane.microvms.add_response(
        "get_microvm", microvm_response(endpoint=fake_rayd.host, state="RUNNING")
    )
    assert await sbx.connect(timeout=120) is sbx
    request = fake_rayd.lifecycle.requests[-1]
    assert (request.timeout_ms, request.mode) == (120_000, lifecycle_pb2.TIMEOUT_MODE_AT_LEAST)


async def test_async_is_running_bounds_the_health_deadline(
    sbx: AsyncSandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El plazo se verifica en el stub y no en `time_remaining()` del
    servidor: gRPC redondea el deadline al milisegundo hacia arriba y un
    `Health` real de 0.5 s puede caducar con el GIL ocupado."""
    timeouts: list[float | None] = []

    async def health(
        request: health_pb2.HealthRequest, timeout: float | None = None
    ) -> health_pb2.HealthResponse:
        timeouts.append(timeout)
        return health_pb2.HealthResponse(agent_ready=True)

    monkeypatch.setattr(sbx.native._health, "Health", health)
    assert await sbx.is_running(request_timeout=0.5) is True
    assert await sbx.is_running() is True
    assert timeouts == [0.5, ReadinessPoll.MAX_RPC_TIMEOUT]


async def test_async_accessors_and_running(sbx: AsyncSandbox, fake_rayd: RaydEndpoint) -> None:
    assert await sbx.is_running() is True
    assert sbx.envd_api_url == f"https://{fake_rayd.host}" == sbx.envd_direct_url
    assert sbx.traffic_access_token == JWE
    assert sbx.git is sbx.native.git
    assert sbx.connection_config.region == "us-east-1"


async def test_async_metrics_series_and_class_variant(
    sbx: AsyncSandbox, control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.servicer.history = [history_sample(0, 7), history_sample(5, 8), history_sample(10, 9)]
    assert [m.mem_cache for m in await sbx.get_metrics()] == [7, 8, 9]
    start = datetime(2026, 9, 23, 11, 0, tzinfo=UTC)
    ranged = await sbx.get_metrics(start, start + timedelta(hours=2))
    assert len(ranged) == 3
    transport = TrackingTransport.for_loopback()
    stub_metadata_probe(control_plane, SANDBOX_ID, fake_rayd)
    classed = await AsyncSandbox.get_metrics(
        sbx.sandbox_id,
        access_token=ACCESS_TOKEN,
        control_plane=control_plane.plane,
        transport=transport,
    )
    assert [m.mem_cache for m in classed] == [7, 8, 9]
    fake_rayd.servicer.history = []
    assert len(await sbx.get_metrics()) == 1
    fake_rayd.servicer.history_unimplemented = True
    with pytest.raises(UnimplementedError, match="M9"):
        await sbx.get_metrics(start=start)


async def test_async_class_metrics_on_an_old_agent(
    sbx: AsyncSandbox, control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.servicer.history_unimplemented = True
    stub_metadata_probe(control_plane, SANDBOX_ID, fake_rayd)
    with pytest.raises(UnimplementedError) as excinfo:
        await AsyncSandbox.get_metrics(
            sbx.sandbox_id,
            access_token=ACCESS_TOKEN,
            control_plane=control_plane.plane,
            transport=TrackingTransport.for_loopback(),
        )
    assert excinfo.value.feature == "Sandbox.get_metrics(sandbox_id)"
    assert excinfo.value.reason == METRICS_HISTORY_IMAGE_REASON
    assert isinstance(excinfo.value.__cause__, UnimplementedError)


async def test_async_list_resumes_with_next_token() -> None:
    plane = FakeControlPlane()
    started = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
    try:
        plane.script_pages(
            [
                SandboxListItem(item, "RUNNING", IMAGE_ARN, "1.0", started)
                for item in ("a", "b", "c")
            ]
        )
        first = await AsyncSandbox.list(limit=1, control_plane=plane)
        assert isinstance(first, AsyncSandboxPaginator)
        assert [item.sandbox_id for item in await first.next_items()] == ["a"]
        resumed = await AsyncSandbox.list(None, 1, first.next_token, control_plane=plane)
        assert [item.sandbox_id for item in await resumed.next_items()] == ["b"]
    finally:
        plane.close()


async def test_async_headers_and_bound_client(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    client = E2B(control_plane=control_plane.plane, headers={"x-client": "uno"})
    capture_launch(control_plane, fake_rayd)
    sandbox = await client.AsyncSandbox.create(
        IMAGE_ARN, access_token=ACCESS_TOKEN, transport=fake_rayd.transport
    )
    assert fake_rayd.servicer.health_calls[-1]["x-client"] == "uno"
    await sandbox.commands.run("echo hi")
    assert fake_rayd.process.start_metadata[-1]["x-client"] == "uno"
    assert dict(sandbox.connection_config.headers) == {"x-client": "uno"}
    await sandbox.native.close()
    assert dict(AsyncSandbox._bound_params) == {}
    with pytest.raises(InvalidArgumentException, match="x-access-token"):
        await AsyncSandbox.create(IMAGE_ARN, headers={"x-access-token": "x"})


async def test_async_proxy_and_retries_reach_the_plane(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_create(**kwargs: Any) -> Any:
        captured.update(kwargs)
        raise CreateCaptured

    monkeypatch.setattr(NativeAsyncSandbox, "create", fake_create)
    with pytest.raises(CreateCaptured):
        await AsyncSandbox.create(
            IMAGE_ARN, region="us-east-1", proxy="http://127.0.0.1:3128", retries=2
        )
    assert captured["transport"].http_proxy == "http://127.0.0.1:3128"
    assert captured["control_plane"]._client.meta.config.retries["total_max_attempts"] == 3


async def test_async_logger_routing(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    app = logging.getLogger("tests.e2b.async.app")
    with capture_logs("tests.e2b.async.app") as mine, capture_logs("rayito.sandbox") as default:
        sandbox = await create_on(control_plane, fake_rayd, logger=app)
        await sandbox.native.close()
    assert f"run-microvm aceptado: {SANDBOX_ID} (PENDING)" in mine.messages()
    assert default.messages() == []


async def test_async_files_forward_the_transfer_keywords(
    sbx: AsyncSandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def spy(name: str) -> Callable[..., Awaitable[object]]:
        async def record(*args: object, **kwargs: Any) -> object:
            calls.append((name, kwargs))
            return [] if name == "write_files" else "hola"

        return record

    for name in ("write", "write_files", "read"):
        monkeypatch.setattr(sbx.files._native, name, spy(name))
    await sbx.files.write(f"{HOME}/a", "x", gzip=True, metadata={"k": "v"}, use_octet_stream=True)
    await sbx.files.write([WriteEntry(f"{HOME}/b", "y")], gzip=True, metadata={"k": "w"})
    await sbx.files.read(f"{HOME}/a", "bytes", gzip=True, stream_idle_timeout=7)
    write, many, read = (kwargs for _, kwargs in calls)
    assert (write["gzip"], write["metadata"], write["use_octet_stream"]) == (True, {"k": "v"}, True)
    assert (many["gzip"], many["metadata"], many["use_octet_stream"]) == (True, {"k": "w"}, False)
    assert (read["format"], read["gzip"], read["stream_idle_timeout"]) == ("bytes", True, 7)


async def test_async_upload_and_download_urls_delegate_to_the_native(
    sbx: AsyncSandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`upload_url`/`download_url` asíncronos devuelven el ticket y el enlace
    nativos con `use_signature_expiration` como plazo; sin ruta no hay dónde
    importar y sin staging es el `UnimplementedError` nativo."""
    with pytest.raises(UnimplementedError, match="RAYITO_TRANSFER_BUCKET"):
        await sbx.upload_url("/x")
    calls: list[tuple[str, tuple[object, ...], dict[str, Any]]] = []

    def spy(name: str) -> Callable[..., Awaitable[str]]:
        async def record(*args: object, **kwargs: Any) -> str:
            calls.append((name, args, kwargs))
            return name

        return record

    monkeypatch.setattr(sbx.native, "upload_url", spy("upload_url"))
    monkeypatch.setattr(sbx.native, "download_url", spy("download_url"))
    assert await sbx.upload_url("e2b.bin", "user", True, 600) == "upload_url"
    assert await sbx.download_url("e2b.bin", use_signature_expiration=60) == "download_url"
    assert calls == [
        ("upload_url", ("e2b.bin",), {"user": "user", "use_signature_expiration": 600}),
        ("download_url", ("e2b.bin",), {"user": None, "use_signature_expiration": 60}),
    ]
    with pytest.raises(InvalidArgumentException, match="ruta de destino"):
        await sbx.upload_url()


class RecordingAsyncCommands:
    """Un `AsyncCommands` nativo que sólo anota lo que el shim le pasa."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    async def run(self, *args: Any, **kwargs: Any) -> str:
        self.calls.append(("run", args, kwargs))
        return "ran"

    async def connect(self, *args: Any, **kwargs: Any) -> str:
        self.calls.append(("connect", args, kwargs))
        return "attached"


async def test_async_commands_forward_callbacks_and_the_connect_arguments() -> None:
    native = RecordingAsyncCommands()
    commands = AsyncCommands(cast(NativeAsyncCommands, native))

    def on_stdout(chunk: str) -> None:
        del chunk

    def on_stderr(chunk: str) -> None:
        del chunk

    ran: object = await commands.run("echo hi", on_stdout=on_stdout, on_stderr=on_stderr)
    attached: object = await commands.connect(42, 5, 2, on_stdout, on_stderr)
    assert (ran, attached) == ("ran", "attached")
    (_, run_args, run_kwargs), (_, connect_args, connect_kwargs) = native.calls
    assert run_args == ("echo hi",)
    assert (run_kwargs["on_stdout"], run_kwargs["on_stderr"]) == (on_stdout, on_stderr)
    assert connect_args == (42,)
    assert connect_kwargs == {
        "on_stdout": on_stdout,
        "on_stderr": on_stderr,
        "timeout": 5,
        "request_timeout": 2,
    }


async def test_async_connection_config_keeps_the_logger_of_create_and_connect(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    app = logging.getLogger("tests.e2b.async.config")
    created = await create_on(control_plane, fake_rayd, logger=app)
    assert created.connection_config.logger is app
    await created.native.close()
    control_plane.microvms.add_response(
        "get_microvm", microvm_response(endpoint=fake_rayd.host, state="RUNNING")
    )
    control_plane.microvms.add_response("create_microvm_auth_token", auth_token_response())
    connected = await AsyncSandbox.connect(
        SANDBOX_ID,
        access_token=ACCESS_TOKEN,
        control_plane=control_plane.plane,
        transport=fake_rayd.transport,
        logger=app,
    )
    assert connected.connection_config.logger is app
    await connected.native.close()
