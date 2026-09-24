"""Los snippets del corpus síncrono con `await`: `rayito.e2b.AsyncSandbox`
contra el mismo `rayd` falso (hello world, comandos, ficheros, PTY, ciclo
de vida, listado, métricas, `UnimplementedError` y avisos)."""

from __future__ import annotations

import asyncio
import json
import time
import warnings
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import timedelta

import grpc
import pytest

from rayito import ALL_TRAFFIC, FilesystemEvent
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
from rayito.exceptions import InvalidArgumentException, LifecycleUnsupportedException
from rayito.sandbox_async.pty import AsyncPtyHandle
from rayito.v1 import lifecycle_pb2, network_pb2

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
from .test_e2b_compat_sync import (
    INTERNET_EGRESS_ARN,
    assert_e2b_defaults,
    capture_launch,
    managed_lifecycle,
)

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
    assert_e2b_defaults(captured)


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
    seen: list[FilesystemEvent] = []
    watch = await sbx.files.watch_dir(f"{HOME}/watched", seen.append)
    await watch.stop()
    await sbx.files.remove(f"{HOME}/a.txt")
    assert await sbx.files.exists(f"{HOME}/a.txt") is False


async def test_async_pty_with_e2b_size_order(sbx: AsyncSandbox, fake_rayd: RaydEndpoint) -> None:
    terminal = await sbx.pty.create(PtySize(rows=24, cols=80), None, timeout=None)
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
    assert info.end_at is not None and info.end_at > info.started_at + timedelta(seconds=3600)
    assert info.lifecycle == {"on_timeout": "kill", "auto_resume": False}
    assert info.allow_internet_access is True
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
    assert await sandbox.beta_pause() is True
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
    return {
        "upload_url": lambda: sandbox.upload_url("/x"),
        "download_url": lambda: sandbox.download_url("/x"),
        "AsyncSandbox.get_metrics": lambda: AsyncSandbox.get_metrics(sandbox.sandbox_id),
        "run_code(language=r)": lambda: sandbox.run_code("1", language="r"),
        "create_code_context(language=java)": lambda: sandbox.create_code_context(language="java"),
        "list(state=PAUSED, query.metadata)": lambda: AsyncSandbox.list(
            query=SandboxQuery(metadata={"a": "1"}), state=[SandboxState.PAUSED]
        ),
    }


UNIMPLEMENTED_FEATURES = [
    "upload_url",
    "download_url",
    "AsyncSandbox.get_metrics",
    "run_code(language=r)",
    "create_code_context(language=java)",
    "list(state=PAUSED, query.metadata)",
]


@pytest.mark.parametrize("feature", UNIMPLEMENTED_FEATURES)
async def test_async_unimplemented_features_raise(
    sbx: AsyncSandbox, fake_rayd: RaydEndpoint, feature: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("RAYITO_ACCESS_TOKEN", raising=False)
    health_calls = len(fake_rayd.servicer.health_calls)
    with pytest.raises(UnimplementedError) as excinfo:
        await unimplemented_calls(sbx)[feature]()
    assert isinstance(excinfo.value, NotImplementedError)
    assert not isinstance(excinfo.value, SandboxException)
    assert len(fake_rayd.servicer.health_calls) == health_calls


async def test_async_set_timeout_maps_to_native_exact(
    sbx: AsyncSandbox, control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    await sbx.set_timeout(60)
    control_plane.microvms.add_response(
        "get_microvm", microvm_response(endpoint=fake_rayd.host, state="RUNNING")
    )
    control_plane.microvms.add_response("create_microvm_auth_token", auth_token_response())
    await AsyncSandbox.set_timeout(
        sbx.sandbox_id,
        60,
        access_token=ACCESS_TOKEN,
        control_plane=control_plane.plane,
        transport=fake_rayd.transport,
    )
    assert [(r.timeout_ms, r.mode) for r in fake_rayd.lifecycle.requests] == [
        (60_000, lifecycle_pb2.TIMEOUT_MODE_EXACT),
        (60_000, lifecycle_pb2.TIMEOUT_MODE_EXACT),
    ]


async def test_async_connect_timeout_is_at_least(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.servicer.lifecycle = managed_lifecycle()
    control_plane.microvms.add_response(
        "get_microvm", microvm_response(endpoint=fake_rayd.host, state="RUNNING")
    )
    control_plane.microvms.add_response("create_microvm_auth_token", auth_token_response())
    sandbox = await AsyncSandbox.connect(
        SANDBOX_ID,
        120,
        access_token=ACCESS_TOKEN,
        control_plane=control_plane.plane,
        transport=fake_rayd.transport,
    )
    await sandbox.native.close()
    request = fake_rayd.lifecycle.requests[-1]
    assert (request.timeout_ms, request.mode) == (120_000, lifecycle_pb2.TIMEOUT_MODE_AT_LEAST)


async def test_async_beta_create_auto_pause_is_lifecycle_pause(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    captured = capture_launch(control_plane, fake_rayd)
    sandbox = await AsyncSandbox.beta_create(
        IMAGE_ARN,
        timeout=60,
        auto_pause=True,
        access_token=ACCESS_TOKEN,
        control_plane=control_plane.plane,
        transport=fake_rayd.transport,
    )
    await sandbox.native.close()
    assert captured["idlePolicy"]["maxIdleDurationSeconds"] == 300
    assert json.loads(str(captured["runHookPayload"]))["lifecycle"]["on_timeout"] == "pause"


async def test_async_older_image_raises_unimplemented_and_terminates(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.servicer.lifecycle = None
    control_plane.microvms.add_response("run_microvm", microvm_response(endpoint=fake_rayd.host))
    control_plane.microvms.add_response("create_microvm_auth_token", auth_token_response())
    control_plane.microvms.add_response(
        "terminate_microvm", {}, expected_params={"microvmIdentifier": SANDBOX_ID}
    )
    with pytest.raises(UnimplementedError) as excinfo:
        await AsyncSandbox.create(
            IMAGE_ARN,
            access_token=ACCESS_TOKEN,
            control_plane=control_plane.plane,
            transport=fake_rayd.transport,
        )
    assert excinfo.value.feature == "lifecycle"
    assert isinstance(excinfo.value.__cause__, LifecycleUnsupportedException)


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
            api_url="https://a",
            sandbox_url="https://s",
            validate_api_key=True,
            api_headers={"k": "v"},
            secure=False,
            access_token=ACCESS_TOKEN,
            control_plane=control_plane.plane,
            transport=fake_rayd.transport,
        )
    await sandbox.kill()
    compat = [w for w in caught if issubclass(w.category, RayitoCompatWarning)]
    assert len(compat) == 8
    assert not any("e2b_x" in str(w.message) for w in compat)
    assert_e2b_defaults(captured)


async def test_async_internet_access_off_is_the_guest_policy(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.servicer.egress_enforcement = network_pb2.EGRESS_ENFORCEMENT_GUEST_ROUTES
    captured = capture_launch(control_plane, fake_rayd)
    sandbox = await AsyncSandbox.create(
        IMAGE_ARN,
        allow_internet_access=False,
        access_token=ACCESS_TOKEN,
        control_plane=control_plane.plane,
        transport=fake_rayd.transport,
    )
    assert captured["egressNetworkConnectors"] == [INTERNET_EGRESS_ARN]
    assert list(fake_rayd.network.last_policy.deny_out) == [ALL_TRAFFIC]
    updated = await sandbox.update_network({"allow_out": ["1.1.1.1"]})  # type: ignore[func-returns-value]
    assert updated is None
    assert list(fake_rayd.network.last_policy.allow_out) == ["1.1.1.1"]
    control_plane.microvms.add_response(
        "get_microvm", microvm_response(endpoint=fake_rayd.host, state="RUNNING")
    )
    control_plane.microvms.add_response("create_microvm_auth_token", auth_token_response())
    result = await AsyncSandbox.update_network(  # type: ignore[func-returns-value]
        sandbox.sandbox_id,
        {"deny_out": ["10.0.0.0/8"]},
        access_token=ACCESS_TOKEN,
        control_plane=control_plane.plane,
        transport=fake_rayd.transport,
    )
    assert result is None
    assert list(fake_rayd.network.last_policy.deny_out) == ["10.0.0.0/8"]
    await sandbox.native.close()


async def test_async_internet_access_off_without_enforcement_terminates(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.servicer.egress_enforcement = network_pb2.EGRESS_ENFORCEMENT_NONE
    capture_launch(control_plane, fake_rayd)
    control_plane.microvms.add_response(
        "terminate_microvm", {}, expected_params={"microvmIdentifier": SANDBOX_ID}
    )
    with pytest.raises(UnimplementedError, match="rayito-base-caps"):
        await AsyncSandbox.create(
            IMAGE_ARN,
            allow_internet_access=False,
            access_token=ACCESS_TOKEN,
            control_plane=control_plane.plane,
            transport=fake_rayd.transport,
        )


async def test_async_network_keys_without_primitive_and_beta_create_mcp(
    control_plane: StubbedControlPlane,
) -> None:
    with pytest.raises(UnimplementedError, match=r"network\.rules"):
        await AsyncSandbox.create(IMAGE_ARN, network={"rules": {}})
    with pytest.raises(UnimplementedError) as excinfo:
        await AsyncSandbox.beta_create(IMAGE_ARN, mcp={"x": {}})
    assert excinfo.value.feature == "mcp"


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


async def test_async_typescript_alias_reaches_the_agent(
    sbx: AsyncSandbox, fake_rayd: RaydEndpoint
) -> None:
    assert (await sbx.run_code("1 + 1", language="ts")).text == "2"
    assert fake_rayd.code.execute_requests[-1].language == "typescript"
    context = await sbx.create_code_context(language="TypeScript")
    assert context.language == "typescript"
    assert fake_rayd.code.create_requests[-1].language == "typescript"


async def test_async_kernel_not_shipped_is_unimplemented_from_the_native_error(
    sbx: AsyncSandbox, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.code.languages = frozenset({"python"})
    with pytest.raises(UnimplementedError) as ran:
        await sbx.run_code("1 + 1", language="javascript")
    assert ran.value.feature == "run_code(language='javascript')"
    assert "rayito-base-poly" in ran.value.reason
    assert isinstance(ran.value.__cause__, InvalidArgumentException)
    assert ran.value.__cause__.grpc_code is grpc.StatusCode.UNIMPLEMENTED
    with pytest.raises(UnimplementedError) as created:
        await sbx.create_code_context(language="ts")
    assert created.value.feature == "create_code_context(language='ts')"
    assert isinstance(created.value.__cause__, InvalidArgumentException)
    executions = len(fake_rayd.code.executions)
    with pytest.raises(UnimplementedError) as refused:
        await sbx.run_code("1", language="r")
    assert refused.value.feature == "run_code(language='r')"
    assert len(fake_rayd.code.executions) == executions


async def test_async_other_native_errors_are_not_remapped(sbx: AsyncSandbox) -> None:
    with pytest.raises(InvalidArgumentException) as excinfo:
        await sbx.run_code("echo 1", language="bash", envs={"A": "1"})
    assert not isinstance(excinfo.value, UnimplementedError)
