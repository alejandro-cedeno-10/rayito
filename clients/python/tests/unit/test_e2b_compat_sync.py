"""Snippets al estilo del cookbook de E2B, escritos como los escribiría un
usuario tras cambiar la línea de import, contra el `rayd` falso y el plano
de control con Stubber (design D8, items 1-10)."""

from __future__ import annotations

import json
import time
import warnings
from collections.abc import Callable, Iterator
from datetime import timedelta
from typing import Any

import pytest

from rayito import Sandbox as NativeSandbox
from rayito.e2b import (
    AsyncSandbox,
    CommandExitException,
    CommandHandle,
    Execution,
    FilesystemEventType,
    NotEnoughSpaceException,
    NotFoundException,
    PtySize,
    RayitoCompatWarning,
    Sandbox,
    SandboxException,
    SandboxInfo,
    SandboxMetrics,
    SandboxPaginator,
    SandboxQuery,
    SandboxState,
    TemplateException,
    UnimplementedError,
    WriteEntry,
    WriteInfo,
)
from rayito.e2b import exceptions as e2b_exceptions
from rayito.v1 import filesystem_pb2

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

ALL_INGRESS_ARN = "arn:aws:lambda:us-east-1:aws:network-connector:aws-network-connector:ALL_INGRESS"
INTERNET_EGRESS_ARN = (
    "arn:aws:lambda:us-east-1:aws:network-connector:aws-network-connector:INTERNET_EGRESS"
)
HOME = "/home/user"
READ_BUDGET_SECONDS = 5.0


def capture_launch(control_plane: StubbedControlPlane, endpoint: RaydEndpoint) -> dict[str, Any]:
    captured: dict[str, Any] = {}
    control_plane.microvms.add_response("run_microvm", microvm_response(endpoint=endpoint.host))
    control_plane.microvms.add_response("create_microvm_auth_token", auth_token_response())
    original = control_plane.plane.run_microvm

    def spy(request: Any) -> Any:
        captured.update(request.to_api())
        return original(request)

    control_plane.plane.run_microvm = spy  # type: ignore[method-assign]
    return captured


@pytest.fixture
def sbx(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Sandbox]:
    """`Sandbox()` tal cual lo escribe un usuario de E2B: `RAYITO_TEMPLATE`
    en el entorno, el plano de control y el transporte inyectados."""
    monkeypatch.setenv("RAYITO_TEMPLATE", IMAGE_ARN)
    fake_rayd.servicer.metadata = {"a": "1"}
    capture_launch(control_plane, fake_rayd)
    with Sandbox(
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


def read_until(handle: CommandHandle, needle: bytes, timeout: float = READ_BUDGET_SECONDS) -> bytes:
    buffer = b""
    deadline = time.monotonic() + timeout
    iterator = iter(handle)
    while needle not in buffer:
        assert time.monotonic() < deadline, f"{needle!r} no llegó; buffer={buffer!r}"
        _, _, pty_bytes = next(iterator)
        assert pty_bytes is not None
        buffer += pty_bytes
    return buffer


def has_next(paginator: SandboxPaginator) -> bool:
    """Lectura fresca: `next_items()` cambia `has_next` y mypy no lo sabe."""
    return paginator.has_next


def wait_until(predicate: Callable[[], bool], timeout: float = READ_BUDGET_SECONDS) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(0.02)


# 1. Hello world ------------------------------------------------------------


def test_hello_world_from_the_e2b_readme(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RAYITO_TEMPLATE", IMAGE_ARN)
    captured = capture_launch(control_plane, fake_rayd)
    control_plane.microvms.add_response(
        "terminate_microvm", {}, expected_params={"microvmIdentifier": SANDBOX_ID}
    )
    with Sandbox(
        access_token=ACCESS_TOKEN, control_plane=control_plane.plane, transport=fake_rayd.transport
    ) as sandbox:
        execution = sandbox.run_code("1+1")
        assert execution.text == "2"
        sandbox.run_code("x = 42")
        assert sandbox.run_code("print(x)").logs.stdout == ["42\n"]
        failed = sandbox.run_code("1/0")
        assert isinstance(failed, Execution)
        assert failed.error is not None and failed.error.name == "ZeroDivisionError"
        assert isinstance(sandbox.native, NativeSandbox)
        assert sandbox.sandbox_domain == fake_rayd.host
        assert repr(sandbox) == f"e2b.Sandbox(sandbox_id={SANDBOX_ID!r})"
    assert captured["maximumDurationInSeconds"] == 300
    assert "idlePolicy" not in captured
    assert captured["ingressNetworkConnectors"] == [ALL_INGRESS_ARN]
    assert captured["egressNetworkConnectors"] == [INTERNET_EGRESS_ARN]


# 2. Charts -----------------------------------------------------------------


def test_charts_from_a_plot_cell(sbx: Sandbox) -> None:
    execution = sbx.run_code("plot")
    assert execution.results[0].png
    assert execution.results[0].chart is not None
    assert execution.results[0].chart.type.value == "line"
    assert execution.results[0].formats() == ["png", "chart"]


# 3. Commands ---------------------------------------------------------------


def test_commands_foreground_background_callbacks_and_list(sbx: Sandbox) -> None:
    assert sbx.commands.run("echo hi").stdout == "hi\n"
    with pytest.raises(CommandExitException) as excinfo:
        sbx.commands.run("exit 3")
    assert excinfo.value.exit_code == 3
    lines: list[str] = []
    result = sbx.commands.run("echo streamed", on_stdout=lines.append)
    assert result.exit_code == 0 and lines == ["streamed\n"]
    handle = sbx.commands.run("sleep 0.2", background=True)
    assert isinstance(handle, CommandHandle)
    assert any(process.pid == handle.pid for process in sbx.commands.list())
    assert handle.wait().exit_code == 0


def test_e2b_exception_names_keep_compiling(sbx: Sandbox) -> None:
    try:
        sbx.commands.run("exit 3")
    except e2b_exceptions.CommandExitException as error:
        assert error.exit_code == 3
    assert issubclass(NotEnoughSpaceException, SandboxException)
    assert issubclass(TemplateException, SandboxException)
    assert e2b_exceptions.SandboxException is SandboxException


# 4. Files ------------------------------------------------------------------


def test_files_write_overloads_read_list_exists_and_watch(
    sbx: Sandbox, fake_rayd: RaydEndpoint
) -> None:
    info = sbx.files.write(f"{HOME}/a.txt", "x")
    assert isinstance(info, WriteInfo)
    assert info.path == f"{HOME}/a.txt"
    written = sbx.files.write([WriteEntry(f"{HOME}/b.txt", "y"), WriteEntry(f"{HOME}/c.txt", "z")])
    assert [entry.path for entry in written] == [f"{HOME}/b.txt", f"{HOME}/c.txt"]
    assert sbx.files.read(f"{HOME}/a.txt") == "x"
    assert sbx.files.read(f"{HOME}/b.txt", format="bytes") == b"y"
    assert sbx.files.read(f"{HOME}/c.txt", "text") == "z"
    assert {entry.name for entry in sbx.files.list(HOME)} >= {"a.txt", "b.txt", "c.txt"}
    assert sbx.files.exists(f"{HOME}/a.txt") is True
    assert sbx.files.exists(f"{HOME}/missing.txt") is False
    sbx.files.make_dir(f"{HOME}/watched")
    seen: list[Any] = []
    handle = sbx.files.watch_dir(f"{HOME}/watched", seen.append)
    fake_rayd.filesystem.push_event(
        f"{HOME}/watched", "new.txt", filesystem_pb2.FILESYSTEM_EVENT_TYPE_CREATE
    )
    wait_until(lambda: len(seen) == 1)
    assert seen[0].type is FilesystemEventType.CREATE and seen[0].name == "new.txt"
    handle.stop()
    sbx.files.remove(f"{HOME}/a.txt")
    assert sbx.files.exists(f"{HOME}/a.txt") is False
    renamed = sbx.files.rename(f"{HOME}/b.txt", f"{HOME}/d.txt")
    assert renamed.path == f"{HOME}/d.txt"
    assert sbx.files.get_info(f"{HOME}/d.txt").size == 1


def test_files_write_requires_data_for_a_path(sbx: Sandbox) -> None:
    with pytest.raises(TypeError):
        sbx.files.write(f"{HOME}/a.txt")  # type: ignore[arg-type]


# 5. PTY --------------------------------------------------------------------


def test_pty_with_e2b_size_order(sbx: Sandbox, fake_rayd: RaydEndpoint) -> None:
    chunks: list[bytes] = []
    terminal = sbx.pty.create(PtySize(rows=24, cols=80), on_data=chunks.append, timeout=None)
    started = fake_rayd.pty.ptys[terminal.pid]
    assert (started.cols, started.rows) == (80, 24)
    sbx.pty.send_stdin(terminal.pid, b"echo hola\n")
    read_until(terminal, b"\r\nhola\r\n")
    assert any(b"hola" in chunk for chunk in chunks)
    sbx.pty.resize(terminal.pid, PtySize(rows=40, cols=120))
    resize = fake_rayd.pty.resize_requests[-1]
    assert (resize.size.cols, resize.size.rows) == (120, 40)
    assert sbx.pty.kill(terminal.pid) is True


# 6. Lifecycle --------------------------------------------------------------


def test_connect_get_info_and_kill(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.servicer.metadata = {"a": "1"}
    control_plane.microvms.add_response(
        "get_microvm", microvm_response(endpoint=fake_rayd.host, state="RUNNING")
    )
    control_plane.microvms.add_response("create_microvm_auth_token", auth_token_response())
    control_plane.microvms.add_response(
        "get_microvm", microvm_response(endpoint=fake_rayd.host, state="RUNNING")
    )
    control_plane.microvms.add_response(
        "terminate_microvm", {}, expected_params={"microvmIdentifier": SANDBOX_ID}
    )
    sandbox = Sandbox.connect(
        SANDBOX_ID,
        access_token=ACCESS_TOKEN,
        control_plane=control_plane.plane,
        transport=fake_rayd.transport,
    )
    assert sandbox.is_running() is True
    info = sandbox.get_info()
    assert isinstance(info, SandboxInfo)
    assert info.state is SandboxState.RUNNING
    assert info.raw_state == "RUNNING"
    assert info.metadata == {"a": "1"}
    assert info.name == IMAGE_NAME
    assert info.template_id == IMAGE_ARN
    assert info.end_at == info.started_at + timedelta(seconds=3600)
    assert sandbox.kill() is True

    control_plane.microvms.add_response(
        "terminate_microvm", {}, expected_params={"microvmIdentifier": "other"}
    )
    assert Sandbox.kill("other", control_plane=control_plane.plane) is True


def test_constructor_with_sandbox_id_connects_and_warns_about_create_kwargs(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    control_plane.microvms.add_response(
        "get_microvm", microvm_response(endpoint=fake_rayd.host, state="RUNNING")
    )
    control_plane.microvms.add_response("create_microvm_auth_token", auth_token_response())
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        sandbox = Sandbox(
            sandbox_id=SANDBOX_ID,
            timeout=900,
            metadata={"a": "1"},
            access_token=ACCESS_TOKEN,
            control_plane=control_plane.plane,
            transport=fake_rayd.transport,
        )
    sandbox.native.close()
    assert sandbox.sandbox_id == SANDBOX_ID
    assert len(caught) == 1
    assert issubclass(caught[0].category, RayitoCompatWarning)
    assert "timeout" in str(caught[0].message) and "metadata" in str(caught[0].message)


def test_class_get_info_reads_metadata_and_maps_terminated_to_not_found(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.servicer.metadata = {"a": "1"}
    transport = TrackingTransport.for_loopback()
    stub_metadata_probe(control_plane, SANDBOX_ID, fake_rayd)
    info = Sandbox.get_info(SANDBOX_ID, control_plane=control_plane.plane, transport=transport)
    assert info.metadata == {"a": "1"} and info.state is SandboxState.RUNNING
    assert transport.all_closed

    control_plane.microvms.add_response("get_microvm", microvm_response(state="SUSPENDED"))
    paused = Sandbox.get_info(SANDBOX_ID, control_plane=control_plane.plane, transport=transport)
    assert paused.state is SandboxState.PAUSED and paused.metadata is None

    control_plane.microvms.add_response("get_microvm", microvm_response(state="TERMINATED"))
    with pytest.raises(NotFoundException):
        Sandbox.get_info(SANDBOX_ID, control_plane=control_plane.plane, transport=transport)


def test_list_paginator_pages_and_filters_by_metadata(
    control_plane: StubbedControlPlane, fake_rayd_factory: FakeRaydFactory
) -> None:
    control_plane.microvms.add_response(
        "list_microvms",
        {
            "items": [
                list_item("a", "RUNNING"),
                list_item("b", "SUSPENDED"),
                list_item("c", "PENDING"),
            ]
        },
    )
    paginator = Sandbox.list(control_plane=control_plane.plane)
    assert isinstance(paginator, SandboxPaginator)
    assert paginator.has_next is True and paginator.next_token is None
    items = paginator.next_items()
    assert [item.sandbox_id for item in items] == ["a", "b", "c"]
    assert [item.state for item in items] == [
        SandboxState.RUNNING,
        SandboxState.PAUSED,
        SandboxState.RUNNING,
    ]
    assert all(item.metadata is None and item.end_at is None for item in items)
    assert has_next(paginator) is False

    control_plane.microvms.add_response(
        "list_microvms",
        {
            "items": [
                list_item("a", "RUNNING"),
                list_item("b", "RUNNING"),
                list_item("c", "RUNNING"),
            ]
        },
    )
    paged = Sandbox.list(limit=2, state=[SandboxState.RUNNING], control_plane=control_plane.plane)
    first = paged.next_items()
    assert [item.sandbox_id for item in first] == ["a", "b"] and paged.has_next is True
    second = paged.next_items()
    assert [item.sandbox_id for item in second] == ["c"] and has_next(paged) is False

    ci = fake_rayd_factory({"env": "ci"})
    dev = fake_rayd_factory({"env": "dev"})
    control_plane.microvms.add_response(
        "list_microvms", {"items": [list_item("ci", "RUNNING"), list_item("dev", "RUNNING")]}
    )
    stub_metadata_probe(control_plane, "ci", ci)
    stub_metadata_probe(control_plane, "dev", dev)
    filtered = Sandbox.list(
        query=SandboxQuery(metadata={"env": "ci"}),
        control_plane=control_plane.plane,
        transport=TrackingTransport.for_loopback(),
    )
    matched = filtered.next_items()
    assert [item.sandbox_id for item in matched] == ["ci"]
    assert matched[0].metadata == {"env": "ci"}
    assert filtered.has_next is False


def test_beta_pause_then_connect(
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
    sandbox = Sandbox(
        IMAGE_ARN,
        access_token=ACCESS_TOKEN,
        control_plane=control_plane.plane,
        transport=fake_rayd.transport,
    )
    sandbox.run_code("x = 40")
    assert sandbox.beta_pause() == SANDBOX_ID
    sandbox.native.close()
    again = Sandbox.connect(
        SANDBOX_ID,
        access_token=ACCESS_TOKEN,
        control_plane=control_plane.plane,
        transport=fake_rayd.transport,
    )
    assert again.run_code("x").text == "40"
    again.native.close()


# 7. Metrics ----------------------------------------------------------------


def test_get_metrics_returns_a_one_element_list_in_bytes(sbx: Sandbox) -> None:
    metrics = sbx.get_metrics()
    assert len(metrics) == 1 and isinstance(metrics[0], SandboxMetrics)
    assert metrics[0].mem_total == 2 * 1024**3
    assert metrics[0].mem_used == 512 * 1024**2
    assert metrics[0].cpu_count == 1
    assert metrics[0].cpu_used_pct == 12.5


# 8. Unimplemented ----------------------------------------------------------


def unimplemented_calls(sandbox: Sandbox) -> dict[str, Callable[[], object]]:
    return {
        "set_timeout": lambda: sandbox.set_timeout(60),
        "Sandbox.set_timeout": lambda: Sandbox.set_timeout(sandbox.sandbox_id, 60),
        "upload_url": lambda: sandbox.upload_url("/x"),
        "download_url": lambda: sandbox.download_url("/x"),
        "get_metrics(start=)": lambda: sandbox.get_metrics(start=1),
        "Sandbox.get_metrics": lambda: Sandbox.get_metrics(sandbox.sandbox_id),
        "run_code(language=r)": lambda: sandbox.run_code("1", language="r"),
        "create_code_context(language=java)": lambda: sandbox.create_code_context(language="java"),
        "list(next_token=)": lambda: Sandbox.list(next_token="x"),
        "list(state=PAUSED, query.metadata)": lambda: Sandbox.list(
            query=SandboxQuery(metadata={"a": "1"}), state=[SandboxState.PAUSED]
        ),
        "beta_create(auto_pause=)": lambda: Sandbox.beta_create(auto_pause=True),
        "connection_config": lambda: sandbox.connection_config,
    }


UNIMPLEMENTED_FEATURES = [
    "set_timeout",
    "Sandbox.set_timeout",
    "upload_url",
    "download_url",
    "get_metrics(start=)",
    "Sandbox.get_metrics",
    "run_code(language=r)",
    "create_code_context(language=java)",
    "list(next_token=)",
    "list(state=PAUSED, query.metadata)",
    "beta_create(auto_pause=)",
    "connection_config",
]


@pytest.mark.parametrize("feature", UNIMPLEMENTED_FEATURES)
def test_unimplemented_features_raise_before_any_call(
    sbx: Sandbox, fake_rayd: RaydEndpoint, feature: str
) -> None:
    health_calls = len(fake_rayd.servicer.health_calls)
    executions = len(fake_rayd.code.executions)
    with pytest.raises(UnimplementedError) as excinfo:
        unimplemented_calls(sbx)[feature]()
    error = excinfo.value
    assert isinstance(error, NotImplementedError)
    assert not isinstance(error, SandboxException)
    assert error.feature.split("(")[0].split(".")[-1] in str(error)
    if feature.endswith("set_timeout"):
        assert "UpdateMicrovm" in str(error) and "reincarnate()" in str(error)
    assert len(fake_rayd.servicer.health_calls) == health_calls
    assert len(fake_rayd.code.executions) == executions


def test_set_timeout_names_update_microvm(sbx: Sandbox) -> None:
    with pytest.raises(UnimplementedError) as excinfo:
        sbx.set_timeout(60)
    assert excinfo.value.feature == "set_timeout"
    assert "UpdateMicrovm" in str(excinfo.value)


def test_bash_and_javascript_are_forwarded_other_kernels_are_not(
    sbx: Sandbox, fake_rayd: RaydEndpoint
) -> None:
    assert "".join(sbx.run_code("echo 1", language="Bash").logs.stdout) == "1\n"
    assert fake_rayd.code.execute_requests[-1].language == "bash"
    assert fake_rayd.code.execute_requests[-1].context_id == ""
    assert sbx.run_code("1 + 1", language="js").text == "2"
    assert fake_rayd.code.execute_requests[-1].language == "javascript"
    assert sbx.run_code("1+1", language="Python").text == "2"
    assert not fake_rayd.code.execute_requests[-1].HasField("language")
    assert sbx.run_code("1+1", "python").text == "2"
    executions = len(fake_rayd.code.executions)
    with pytest.raises(UnimplementedError) as excinfo:
        sbx.run_code("1", language="r")
    assert excinfo.value.feature == "run_code(language='r')"
    assert "rayito-base-poly" in excinfo.value.reason
    assert len(fake_rayd.code.executions) == executions
    listed = {item.id: item.language for item in sbx.native.list_code_contexts()}
    assert listed["default-bash"] == "bash"
    assert listed["default-javascript"] == "javascript"
    context = sbx.create_code_context(language="bash")
    assert context.language == "bash"
    assert fake_rayd.code.create_requests[-1].language == "bash"


# 9. Warnings ---------------------------------------------------------------


def test_ignored_kwargs_warn_and_defaults_reach_the_wire(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    captured = capture_launch(control_plane, fake_rayd)
    control_plane.microvms.add_response("terminate_microvm", {})
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        sandbox = Sandbox(
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
    sandbox.kill()
    compat = [w for w in caught if issubclass(w.category, RayitoCompatWarning)]
    assert len(compat) == 5
    assert not any("e2b_x" in str(w.message) for w in compat)
    assert captured["maximumDurationInSeconds"] == 300
    assert "idlePolicy" not in captured
    assert captured["ingressNetworkConnectors"] == [ALL_INGRESS_ARN]
    assert captured["egressNetworkConnectors"] == [INTERNET_EGRESS_ARN]
    payload = json.loads(str(captured["runHookPayload"]))
    assert "metadata" not in payload


def test_allow_internet_access_false_is_unimplemented_before_any_call(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    """Q44: sin conector de egress el MicroVM sigue saliendo a internet, así
    que el shim no finge; el Stubber no recibe ningún `run-microvm`."""
    with pytest.raises(UnimplementedError, match="allow_internet_access=False"):
        Sandbox(
            IMAGE_ARN,
            allow_internet_access=False,
            access_token=ACCESS_TOKEN,
            control_plane=control_plane.plane,
            transport=fake_rayd.transport,
        )
    assert fake_rayd.servicer.health_calls == []


def test_idle_and_egress_are_not_accepted() -> None:
    with pytest.raises(TypeError):
        Sandbox(idle=None)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        Sandbox(egress=[])  # type: ignore[call-arg]


# 10. Names -----------------------------------------------------------------


def test_async_sandbox_is_exported() -> None:
    assert AsyncSandbox.__name__ == "AsyncSandbox"
