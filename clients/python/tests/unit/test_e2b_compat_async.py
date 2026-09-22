"""Los snippets del corpus síncrono con `await`: `rayito.e2b.AsyncSandbox`
contra el mismo `rayd` falso (hello world, comandos, ficheros, PTY, ciclo
de vida, listado, métricas, `UnimplementedError` y avisos)."""

from __future__ import annotations

import asyncio
import time
import warnings
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import timedelta

import pytest

from rayito import AsyncSandbox as NativeAsyncSandbox
from rayito.e2b import (
    AsyncSandbox,
    AsyncSandboxPaginator,
    CommandExitException,
    NotFoundException,
    PtySize,
    RayitoCompatWarning,
    SandboxException,
    SandboxMetrics,
    SandboxQuery,
    SandboxState,
    UnimplementedError,
    WriteEntry,
)
from rayito.sandbox_async.pty import AsyncPtyHandle

from .conftest import (
    ACCESS_TOKEN,
    IMAGE_ARN,
    IMAGE_NAME,
    SANDBOX_ID,
    FakeRaydFactory,
    RaydEndpoint,
    StubbedControlPlane,
    TrackingTransport,
    auth_token_response,
    list_item,
    microvm_response,
    stub_metadata_probe,
)
from .test_e2b_compat_sync import ALL_INGRESS_ARN, INTERNET_EGRESS_ARN, capture_launch

HOME = "/home/user"
READ_BUDGET_SECONDS = 5.0


@pytest.fixture
async def sbx(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[AsyncSandbox]:
    monkeypatch.setenv("RAYITO_TEMPLATE", IMAGE_ARN)
    fake_rayd.servicer.metadata = {"a": "1"}
    capture_launch(control_plane, fake_rayd)
    async with await AsyncSandbox.create(
        timeout=900,
        metadata={"a": "1"},
        access_token=ACCESS_TOKEN,
        control_plane=control_plane.plane,
        transport=fake_rayd.transport,
    ) as sandbox:
        yield sandbox
        control_plane.microvms.add_response(
            "terminate_microvm", {}, expected_params={"microvmIdentifier": SANDBOX_ID}
        )


async def read_until(
    handle: AsyncPtyHandle, needle: bytes, timeout: float = READ_BUDGET_SECONDS
) -> bytes:
    buffer = b""
    deadline = time.monotonic() + timeout
    iterator = handle.__aiter__()
    while needle not in buffer:
        assert time.monotonic() < deadline, f"{needle!r} no llegó; buffer={buffer!r}"
        _, _, pty_bytes = await iterator.__anext__()
        assert pty_bytes is not None
        buffer += pty_bytes
    return buffer


def has_next(paginator: AsyncSandboxPaginator) -> bool:
    """Lectura fresca: `next_items()` cambia `has_next` y mypy no lo sabe."""
    return paginator.has_next


async def test_async_hello_world(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RAYITO_TEMPLATE", IMAGE_ARN)
    captured = capture_launch(control_plane, fake_rayd)
    control_plane.microvms.add_response(
        "terminate_microvm", {}, expected_params={"microvmIdentifier": SANDBOX_ID}
    )
    async with await AsyncSandbox.create(
        access_token=ACCESS_TOKEN, control_plane=control_plane.plane, transport=fake_rayd.transport
    ) as sandbox:
        execution = await sandbox.run_code("1+1")
        assert execution.text == "2"
        await sandbox.run_code("x = 42")
        assert (await sandbox.run_code("print(x)")).logs.stdout == ["42\n"]
        failed = await sandbox.run_code("1/0")
        assert failed.error is not None and failed.error.name == "ZeroDivisionError"
        assert isinstance(sandbox.native, NativeAsyncSandbox)
        assert sandbox.sandbox_domain == fake_rayd.host
        assert await sandbox.is_running() is True
    assert captured["maximumDurationInSeconds"] == 300
    assert "idlePolicy" not in captured
    assert captured["ingressNetworkConnectors"] == [ALL_INGRESS_ARN]
    assert captured["egressNetworkConnectors"] == [INTERNET_EGRESS_ARN]


async def test_async_commands_and_files(sbx: AsyncSandbox) -> None:
    assert (await sbx.commands.run("echo hi")).stdout == "hi\n"
    with pytest.raises(CommandExitException) as excinfo:
        await sbx.commands.run("exit 3")
    assert excinfo.value.exit_code == 3
    handle = await sbx.commands.run("sleep 0.2", background=True)
    assert (await handle.wait()).exit_code == 0

    info = await sbx.files.write(f"{HOME}/a.txt", "x")
    assert info.path == f"{HOME}/a.txt"
    written = await sbx.files.write(
        [WriteEntry(f"{HOME}/b.txt", "y"), WriteEntry(f"{HOME}/c.txt", "z")]
    )
    assert [entry.path for entry in written] == [f"{HOME}/b.txt", f"{HOME}/c.txt"]
    assert await sbx.files.read(f"{HOME}/a.txt") == "x"
    assert await sbx.files.read(f"{HOME}/b.txt", format="bytes") == b"y"
    assert await sbx.files.exists(f"{HOME}/c.txt") is True
    assert {entry.name for entry in await sbx.files.list(HOME)} >= {"a.txt", "b.txt", "c.txt"}
    await sbx.files.make_dir(f"{HOME}/watched")
    watch = await sbx.files.watch_dir(f"{HOME}/watched")
    await watch.stop()
    await sbx.files.remove(f"{HOME}/a.txt")
    assert await sbx.files.exists(f"{HOME}/a.txt") is False


async def test_async_pty_with_e2b_size_order(sbx: AsyncSandbox, fake_rayd: RaydEndpoint) -> None:
    terminal = await sbx.pty.create(PtySize(rows=24, cols=80), timeout=None)
    started = fake_rayd.pty.ptys[terminal.pid]
    assert (started.cols, started.rows) == (80, 24)
    await sbx.pty.send_stdin(terminal.pid, b"echo hola\n")
    await read_until(terminal, b"\r\nhola\r\n")
    await sbx.pty.resize(terminal.pid, PtySize(rows=40, cols=120))
    resize = fake_rayd.pty.resize_requests[-1]
    assert (resize.size.cols, resize.size.rows) == (120, 40)
    assert await sbx.pty.kill(terminal.pid) is True


async def test_async_get_info_metrics_and_class_variants(
    sbx: AsyncSandbox, control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    control_plane.microvms.add_response(
        "get_microvm", microvm_response(endpoint=fake_rayd.host, state="RUNNING")
    )
    info = await sbx.get_info()
    assert info.state is SandboxState.RUNNING
    assert info.metadata == {"a": "1"}
    assert info.name == IMAGE_NAME
    assert info.end_at == info.started_at + timedelta(seconds=3600)
    metrics = await sbx.get_metrics()
    assert len(metrics) == 1 and isinstance(metrics[0], SandboxMetrics)
    assert metrics[0].mem_total == 2 * 1024**3

    transport = TrackingTransport.for_loopback()
    stub_metadata_probe(control_plane, SANDBOX_ID, fake_rayd)
    probed = await AsyncSandbox.get_info(
        SANDBOX_ID, control_plane=control_plane.plane, transport=transport
    )
    assert probed.metadata == {"a": "1"} and transport.all_closed
    control_plane.microvms.add_response("get_microvm", microvm_response(state="TERMINATED"))
    with pytest.raises(NotFoundException):
        await AsyncSandbox.get_info(SANDBOX_ID, control_plane=control_plane.plane)


async def test_async_list_paginator(
    control_plane: StubbedControlPlane, fake_rayd_factory: FakeRaydFactory
) -> None:
    control_plane.microvms.add_response(
        "list_microvms",
        {
            "items": [
                list_item("a", "RUNNING"),
                list_item("b", "RUNNING"),
                list_item("c", "SUSPENDED"),
            ]
        },
    )
    paginator = await AsyncSandbox.list(limit=2, control_plane=control_plane.plane)
    assert isinstance(paginator, AsyncSandboxPaginator)
    assert paginator.has_next is True and paginator.next_token is None
    first = await paginator.next_items()
    assert [item.sandbox_id for item in first] == ["a", "b"] and paginator.has_next is True
    second = await paginator.next_items()
    assert [item.sandbox_id for item in second] == ["c"] and has_next(paginator) is False
    assert second[0].state is SandboxState.PAUSED

    ci = fake_rayd_factory({"env": "ci"})
    dev = fake_rayd_factory({"env": "dev"})
    control_plane.microvms.add_response(
        "list_microvms", {"items": [list_item("ci", "RUNNING"), list_item("dev", "RUNNING")]}
    )
    stub_metadata_probe(control_plane, "ci", ci)
    stub_metadata_probe(control_plane, "dev", dev)
    filtered = await AsyncSandbox.list(
        query=SandboxQuery(metadata={"env": "ci"}),
        control_plane=control_plane.plane,
        transport=TrackingTransport.for_loopback(),
    )
    matched = await filtered.next_items()
    assert [item.sandbox_id for item in matched] == ["ci"]
    assert matched[0].metadata == {"env": "ci"}
    assert filtered.has_next is False


async def test_async_beta_pause_then_connect(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    capture_launch(control_plane, fake_rayd)
    control_plane.microvms.add_response(
        "get_microvm", microvm_response(endpoint=fake_rayd.host, state="RUNNING")
    )
    control_plane.microvms.add_response("suspend_microvm", {}, {"microvmIdentifier": SANDBOX_ID})
    control_plane.microvms.add_response(
        "get_microvm", microvm_response(endpoint=fake_rayd.host, state="SUSPENDED")
    )
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
    sandbox = await AsyncSandbox.create(
        IMAGE_ARN,
        access_token=ACCESS_TOKEN,
        control_plane=control_plane.plane,
        transport=fake_rayd.transport,
    )
    await sandbox.run_code("x = 40")
    assert await sandbox.beta_pause() == SANDBOX_ID
    await sandbox.native.close()
    again = await AsyncSandbox.connect(
        SANDBOX_ID,
        access_token=ACCESS_TOKEN,
        control_plane=control_plane.plane,
        transport=fake_rayd.transport,
    )
    assert (await again.run_code("x")).text == "40"
    await again.native.close()


def unimplemented_calls(sandbox: AsyncSandbox) -> dict[str, Callable[[], Awaitable[object]]]:
    async def connection_config() -> object:
        return sandbox.connection_config

    return {
        "set_timeout": lambda: sandbox.set_timeout(60),
        "AsyncSandbox.set_timeout": lambda: AsyncSandbox.set_timeout(sandbox.sandbox_id, 60),
        "upload_url": lambda: sandbox.upload_url("/x"),
        "download_url": lambda: sandbox.download_url("/x"),
        "get_metrics(start=)": lambda: sandbox.get_metrics(start=1),
        "AsyncSandbox.get_metrics": lambda: AsyncSandbox.get_metrics(sandbox.sandbox_id),
        "run_code(language=r)": lambda: sandbox.run_code("1", language="r"),
        "create_code_context(language=java)": lambda: sandbox.create_code_context(language="java"),
        "list(next_token=)": lambda: AsyncSandbox.list(next_token="x"),
        "list(state=PAUSED, query.metadata)": lambda: AsyncSandbox.list(
            query=SandboxQuery(metadata={"a": "1"}), state=[SandboxState.PAUSED]
        ),
        "beta_create(auto_pause=)": lambda: AsyncSandbox.beta_create(auto_pause=True),
        "connection_config": connection_config,
    }


UNIMPLEMENTED_FEATURES = [
    "set_timeout",
    "AsyncSandbox.set_timeout",
    "upload_url",
    "download_url",
    "get_metrics(start=)",
    "AsyncSandbox.get_metrics",
    "run_code(language=r)",
    "create_code_context(language=java)",
    "list(next_token=)",
    "list(state=PAUSED, query.metadata)",
    "beta_create(auto_pause=)",
    "connection_config",
]


@pytest.mark.parametrize("feature", UNIMPLEMENTED_FEATURES)
async def test_async_unimplemented_features_raise(
    sbx: AsyncSandbox, fake_rayd: RaydEndpoint, feature: str
) -> None:
    health_calls = len(fake_rayd.servicer.health_calls)
    with pytest.raises(UnimplementedError) as excinfo:
        await unimplemented_calls(sbx)[feature]()
    assert isinstance(excinfo.value, NotImplementedError)
    assert not isinstance(excinfo.value, SandboxException)
    if feature.endswith("set_timeout"):
        assert "UpdateMicrovm" in str(excinfo.value) and "reincarnate()" in str(excinfo.value)
    assert len(fake_rayd.servicer.health_calls) == health_calls


async def test_async_ignored_kwargs_warn(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    captured = capture_launch(control_plane, fake_rayd)
    control_plane.microvms.add_response("terminate_microvm", {})
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        sandbox = await AsyncSandbox.create(
            IMAGE_ARN,
            api_key="e2b_x",
            domain="e2b.dev",
            debug=True,
            proxy="http://p",
            secure=False,
            access_token=ACCESS_TOKEN,
            control_plane=control_plane.plane,
            transport=fake_rayd.transport,
        )
    await sandbox.kill()
    compat = [w for w in caught if issubclass(w.category, RayitoCompatWarning)]
    assert len(compat) == 5
    assert not any("e2b_x" in str(w.message) for w in compat)
    assert captured["egressNetworkConnectors"] == [INTERNET_EGRESS_ARN]
    assert captured["maximumDurationInSeconds"] == 300
    with pytest.raises(UnimplementedError, match="allow_internet_access=False"):
        await AsyncSandbox.create(IMAGE_ARN, allow_internet_access=False)


async def test_async_list_and_connect_warn_about_api_key(
    control_plane: StubbedControlPlane,
) -> None:
    control_plane.microvms.add_response("list_microvms", {"items": []})
    with pytest.warns(RayitoCompatWarning, match="api_key"):
        paginator = await AsyncSandbox.list(api_key="e2b_x", control_plane=control_plane.plane)
    assert await paginator.next_items() == []
    await asyncio.sleep(0)


async def test_async_shim_forwards_languages(sbx: AsyncSandbox, fake_rayd: RaydEndpoint) -> None:
    execution = await sbx.run_code("echo 1", language="bash")
    assert "".join(execution.logs.stdout) == "1\n"
    assert fake_rayd.code.execute_requests[-1].language == "bash"
    context = await sbx.create_code_context(language="javascript")
    assert context.language == "javascript"
    assert fake_rayd.code.create_requests[-1].language == "javascript"
    with pytest.raises(UnimplementedError):
        await sbx.run_code("1", language="r")
