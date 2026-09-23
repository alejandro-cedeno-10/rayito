"""`Sandbox.commands` y `CommandHandle` contra el `rayd` falso (gRPC real en
loopback) y el plano de control con Stubber."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

import grpc
import pytest

from rayito import CommandHandle, CommandResult, Sandbox
from rayito._limits import DEFAULT_PORT
from rayito._payload import generate_access_token
from rayito._sandbox_base import ReconnectPoll
from rayito._transport import PROXY_AUTH_KEY, PROXY_FORBIDDEN_MARKER
from rayito.exceptions import (
    AuthenticationException,
    CommandExitException,
    InvalidArgumentException,
    NotFoundException,
    SandboxException,
    SandboxNotFoundException,
    SandboxStateException,
    TimeoutException,
)
from rayito.v1 import process_pb2, process_pb2_grpc

from .conftest import (
    ACCESS_TOKEN,
    IMAGE_ARN,
    JWE,
    METRICS_TIMESTAMP_UNIX_MS,
    SANDBOX_ID,
    FakeRpcError,
    RaydEndpoint,
    StubbedControlPlane,
    always_running,
    auth_token_response,
    microvm_response,
)
from .fake_process import CHUNK_SIZE, CannedReply

TIMEOUT_LATENCY_BUDGET_SECONDS = 2.0
DISCONNECT_DELAY_SECONDS = 0.2


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


def stub_remint(control_plane: StubbedControlPlane, jwe: str) -> None:
    control_plane.microvms.add_response(
        "create_microvm_auth_token",
        auth_token_response(jwe),
        expected_params={
            "microvmIdentifier": SANDBOX_ID,
            "expirationInMinutes": 60,
            "allowedPorts": [{"port": DEFAULT_PORT}],
        },
    )


@pytest.fixture
def sandbox(control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint) -> Iterator[Sandbox]:
    """El `terminate_microvm` se encola en el teardown: el Stubber es FIFO y
    los tests añaden sus propias respuestas (reacuñado, `get_microvm`) antes."""
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
        control_plane.microvms.add_response(
            "terminate_microvm", {}, expected_params={"microvmIdentifier": SANDBOX_ID}
        )
        created.kill()


def proxy_forbidden() -> FakeRpcError:
    return FakeRpcError(
        grpc.StatusCode.PERMISSION_DENIED,
        details="Received http2 header with status: 403",
        debug=f'{{"grpc_status":7,"description":"{PROXY_FORBIDDEN_MARKER}"}}',
    )


def failing_stream(error: grpc.RpcError) -> Iterator[Any]:
    """Un stream que falla en su primer `next()`, como el proxy con un JWE caducado."""
    yield from ()
    raise error


def resetting_stream(error: grpc.RpcError) -> Iterator[Any]:
    """Un stream que entrega el `StartEvent` y muere después."""
    yield process_pb2.ProcessEvent(start=process_pb2.StartEvent(pid=1))
    raise error


def start_failing_first(
    real: Callable[..., Any], failures: list[Callable[[], Iterator[Any]]]
) -> Callable[..., Any]:
    def start(request: Any, timeout: float | None = None) -> Any:
        if failures:
            return failures.pop(0)()
        return real(request, timeout=timeout)

    return start


def live_pids(sandbox: Sandbox) -> set[int]:
    return {info.pid for info in sandbox.commands.list()}


def test_foreground_echo_and_request_shape(sandbox: Sandbox, fake_rayd: RaydEndpoint) -> None:
    result = sandbox.commands.run("echo hola")
    assert result == CommandResult(stdout="hola\n", stderr="", exit_code=0, error=None)
    request = fake_rayd.process.start_requests[-1]
    assert request.process.cmd == "/bin/bash"
    assert list(request.process.args) == ["-l", "-c", "echo hola"]
    assert request.timeout_ms == 60_000
    assert request.stdin is False
    assert not request.HasField("user")
    assert fake_rayd.process.start_metadata[-1][PROXY_AUTH_KEY] == JWE
    assert sandbox.commands.list() == []


def test_options_reach_the_agent(sandbox: Sandbox, fake_rayd: RaydEndpoint) -> None:
    assert sandbox.commands.run("whoami", user="user").stdout == "user\n"
    assert sandbox.commands.run("pwd", cwd="/tmp").stdout == "/tmp\n"
    assert sandbox.commands.run("env", envs={"FOO": "bar"}).stdout == "FOO=bar\n"
    handle = sandbox.commands.run("sleep 5", background=True, timeout=None, tag="m2", stdin=True)
    request = fake_rayd.process.start_requests[-1]
    assert (request.timeout_ms, request.stdin, request.tag) == (0, True, "m2")
    assert handle.kill() is True


def exit_code_of(handle: CommandHandle) -> int | None:
    """Lectura fresca del exit code: `wait()` lo cambia y mypy no lo sabe."""
    return handle.exit_code


def test_background_and_foreground_are_equivalent(sandbox: Sandbox) -> None:
    handle = sandbox.commands.run("echo hola", background=True)
    assert isinstance(handle, CommandHandle)
    assert exit_code_of(handle) is None
    result = handle.wait()
    assert result == sandbox.commands.run("echo hola")
    assert exit_code_of(handle) == 0
    assert handle.error is None
    assert handle.last_seq == 1
    assert handle.wait() is result


def test_callbacks_receive_decoded_text(sandbox: Sandbox) -> None:
    out: list[str] = []
    err: list[str] = []
    sandbox.commands.run("echo a", on_stdout=out.append, on_stderr=err.append)
    sandbox.commands.run("err b", on_stdout=out.append, on_stderr=err.append)
    assert "".join(out) == "a\n"
    assert "".join(err) == "b\n"


def test_wait_callbacks_receive_the_chunks_after_the_run_callbacks(
    sandbox: Sandbox, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.process.reply_when("echo out", CannedReply(stdout="out\n", stderr="err\n"))
    order: list[str] = []
    out: list[str] = []
    err: list[str] = []
    handle = sandbox.commands.run(
        "echo out; echo err >&2", background=True, on_stdout=lambda _: order.append("run")
    )

    def on_stdout(text: str) -> None:
        order.append("wait")
        out.append(text)

    result = handle.wait(on_stdout=on_stdout, on_stderr=err.append)
    assert (out, err) == (["out\n"], ["err\n"])
    assert order == ["run", "wait"]
    assert (result.stdout, result.exit_code) == ("out\n", 0)


def test_wait_callbacks_never_replay_consumed_chunks(sandbox: Sandbox) -> None:
    handle = sandbox.commands.run("seq 3", background=True)
    iterator = iter(handle)
    first, _, _ = next(iterator)
    seen: list[str] = []
    result = handle.wait(on_stdout=seen.append)
    assert first == "1\n"
    assert "".join(seen) == "2\n3\n"
    assert result.stdout == "1\n2\n3\n"


def test_non_zero_exit_is_command_exit_exception(sandbox: Sandbox) -> None:
    with pytest.raises(CommandExitException) as excinfo:
        sandbox.commands.run("exit 3")
    assert excinfo.value.exit_code == 3
    assert excinfo.value.error == "exited"
    with pytest.raises(CommandExitException) as unknown:
        sandbox.commands.run("frobnicate")
    assert unknown.value.exit_code == 127
    assert "command not found" in unknown.value.stderr
    handle = sandbox.commands.run("exit 4", background=True)
    with pytest.raises(CommandExitException):
        handle.wait()
    assert (handle.exit_code, handle.error) == (4, "exited")
    with pytest.raises(CommandExitException):
        handle.wait()


def test_server_timeout_is_timeout_exception_and_process_is_gone(sandbox: Sandbox) -> None:
    started = time.perf_counter()
    with pytest.raises(TimeoutException):
        sandbox.commands.run("sleep 10", timeout=0.2)
    assert time.perf_counter() - started < TIMEOUT_LATENCY_BUDGET_SECONDS
    handle = sandbox.commands.run("sleep 10", background=True, timeout=0.2)
    with pytest.raises(TimeoutException):
        handle.wait()
    assert handle.error == "deadline_exceeded"
    assert handle.exit_code == 143
    assert handle.pid not in live_pids(sandbox)


def test_list_kill_and_signaled_exit(sandbox: Sandbox) -> None:
    handle = sandbox.commands.run("sleep 30", background=True, tag="m2")
    listed = [info for info in sandbox.commands.list() if info.pid == handle.pid]
    assert len(listed) == 1
    assert listed[0].kind == "process"
    assert listed[0].tag == "m2"
    assert listed[0].args == ("-l", "-c", "sleep 30")
    assert sandbox.commands.kill(handle.pid) is True
    with pytest.raises(CommandExitException) as excinfo:
        handle.wait()
    assert excinfo.value.exit_code == 137
    assert excinfo.value.error == "signaled"
    assert sandbox.commands.kill(handle.pid) is False
    assert handle.kill() is False
    assert handle.pid not in live_pids(sandbox)


def test_stdin_round_trip_and_precondition(sandbox: Sandbox) -> None:
    handle = sandbox.commands.run("cat", background=True, stdin=True)
    handle.send_stdin("hola\n")
    handle.send_stdin(b"chau\n")
    handle.close_stdin()
    assert handle.wait().stdout == "hola\nchau\n"
    without_stdin = sandbox.commands.run("sleep 30", background=True)
    with pytest.raises(InvalidArgumentException):
        without_stdin.send_stdin("x")
    with pytest.raises(InvalidArgumentException):
        sandbox.commands.close_stdin(without_stdin.pid)
    assert without_stdin.kill() is True
    with pytest.raises(CommandExitException):
        without_stdin.wait()
    with pytest.raises(NotFoundException):
        sandbox.commands.send_stdin(without_stdin.pid, "x")
    with pytest.raises(NotFoundException):
        sandbox.commands.close_stdin(without_stdin.pid)


def test_connect_replays_from_seq_and_maps_missing_pids(
    sandbox: Sandbox, fake_rayd: RaydEndpoint
) -> None:
    handle = sandbox.commands.run("seq 3", background=True)
    full = sandbox.commands.connect(handle.pid, from_seq=1)
    assert full.wait().stdout == "1\n2\n3\n"
    assert handle.wait().stdout == "1\n2\n3\n"
    assert full.last_seq == handle.last_seq == 3
    retained = sandbox.commands.connect(handle.pid)
    assert retained.wait() == CommandResult(stdout="", stderr="", exit_code=0)
    replayed = sandbox.commands.connect(handle.pid, from_seq=2)
    assert replayed.wait().stdout == "2\n3\n"
    with pytest.raises(NotFoundException):
        sandbox.commands.connect(999_999)
    with pytest.raises(NotFoundException):
        sandbox.commands.connect(handle.pid, from_seq=999)
    fake_rayd.process.retention_seconds = 0
    with pytest.raises(NotFoundException):
        sandbox.commands.connect(handle.pid)
    assert [request.from_seq for request in fake_rayd.process.connect_requests[:2]] == [1, 0]


def test_iteration_yields_chunks_and_skips_keepalives(sandbox: Sandbox) -> None:
    handle = sandbox.commands.run("seq 3", background=True)
    chunks = list(handle)
    assert chunks == [("1\n", None, None), ("2\n", None, None), ("3\n", None, None)]
    assert handle.exit_code == 0
    assert list(handle) == []
    assert handle.wait().stdout == "1\n2\n3\n"


def test_large_output_and_split_multibyte_character(sandbox: Sandbox) -> None:
    assert len(sandbox.commands.run("big 3000000").stdout) == 3_000_000
    assert sandbox.commands.run("split").stdout == "a" * (CHUNK_SIZE - 1) + "é\n"


def test_output_truncated_keeps_the_process_alive(sandbox: Sandbox) -> None:
    handle = sandbox.commands.run("truncate", background=True)
    with pytest.raises(SandboxException, match="output_truncated"):
        handle.wait()
    assert handle.stdout == "partial\n"
    assert handle.error == "output_truncated"
    assert handle.pid in live_pids(sandbox)
    resumed = sandbox.commands.connect(handle.pid, from_seq=handle.last_seq + 1)
    assert sandbox.commands.kill(handle.pid) is True
    with pytest.raises(CommandExitException) as excinfo:
        resumed.wait()
    assert excinfo.value.exit_code == 137


def test_disconnect_keeps_the_process_listed(sandbox: Sandbox) -> None:
    handle = sandbox.commands.run("sleep 30", background=True)
    handle.disconnect()
    handle.disconnect()
    assert handle.pid in live_pids(sandbox)
    with pytest.raises(SandboxException, match="desconectado"):
        handle.wait()
    assert list(handle) == []
    assert handle.kill() is True


def test_disconnect_from_another_thread_ends_wait(sandbox: Sandbox) -> None:
    handle = sandbox.commands.run("sleep 30", background=True)
    disconnector = threading.Timer(DISCONNECT_DELAY_SECONDS, handle.disconnect)
    disconnector.start()
    try:
        with pytest.raises(SandboxException, match="desconectado"):
            handle.wait()
    finally:
        disconnector.join()
    with pytest.raises(SandboxException, match="desconectado"):
        handle.wait()
    assert handle.pid in live_pids(sandbox)
    assert handle.kill() is True


def test_phase_gate_waits_for_a_resume_then_is_a_state_error(
    sandbox: Sandbox, fake_rayd: RaydEndpoint
) -> None:
    """Desde M5 el phase gate dispara la reconexión: el SDK sondea `Health`
    hasta ver una generación nueva y, si no llega en `reconnect_timeout`,
    falla con `SandboxStateException` sin haber reintentado el RPC."""
    sandbox._reconnect_timeout = 0.3
    health_calls = len(fake_rayd.servicer.health_calls)
    for phase in ("suspending", "terminating"):
        fake_rayd.process.phase = phase
        with pytest.raises(SandboxStateException, match=phase):
            sandbox.commands.run("echo hola")
        with pytest.raises(SandboxStateException, match=phase):
            sandbox.commands.run("echo hola", background=True)
        with pytest.raises(SandboxStateException, match=phase):
            sandbox.commands.connect(1)
    fake_rayd.process.phase = None
    assert len(fake_rayd.servicer.health_calls) > health_calls
    assert len(fake_rayd.process.start_requests) == 4
    assert sandbox.commands.run("echo hola").stdout == "hola\n"


def test_root_is_refused_by_the_agent_not_the_proxy(sandbox: Sandbox) -> None:
    with pytest.raises(AuthenticationException) as excinfo:
        sandbox.commands.run("whoami", user="root")
    assert excinfo.value.proxy_rejected is False


def test_invalid_arguments_are_rejected_before_or_by_the_agent(
    sandbox: Sandbox, fake_rayd: RaydEndpoint
) -> None:
    with pytest.raises(InvalidArgumentException):
        sandbox.commands.run("")
    assert fake_rayd.process.start_requests == []
    with pytest.raises(InvalidArgumentException, match="cwd"):
        sandbox.commands.run("pwd", cwd="/does/not/exist")
    with pytest.raises(InvalidArgumentException):
        sandbox.commands.run("true", timeout=-1)
    with pytest.raises(InvalidArgumentException):
        sandbox.commands.kill(0)


def test_proxy_403_on_stream_open_remints_once(
    sandbox: Sandbox,
    fake_rayd: RaydEndpoint,
    control_plane: StubbedControlPlane,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub_remint(control_plane, "jwe-2")
    monkeypatch.setattr(
        sandbox._process,
        "Start",
        start_failing_first(sandbox._process.Start, [lambda: failing_stream(proxy_forbidden())]),
    )
    assert sandbox.commands.run("echo hola").stdout == "hola\n"
    assert len(fake_rayd.process.start_requests) == 1
    assert fake_rayd.process.start_metadata[-1][PROXY_AUTH_KEY] == "jwe-2"
    control_plane.microvms.assert_no_pending_responses()


def test_double_proxy_403_on_stream_open_surfaces_proxy_rejected(
    sandbox: Sandbox, control_plane: StubbedControlPlane, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_remint(control_plane, "jwe-2")
    monkeypatch.setattr(
        sandbox._process,
        "Start",
        start_failing_first(
            sandbox._process.Start,
            [
                lambda: failing_stream(proxy_forbidden()),
                lambda: failing_stream(proxy_forbidden()),
            ],
        ),
    )
    with pytest.raises(AuthenticationException) as excinfo:
        sandbox.commands.run("echo hola")
    assert excinfo.value.proxy_rejected is True
    control_plane.microvms.assert_no_pending_responses()


def test_stream_reset_with_live_agent_resubscribes_with_connect(
    sandbox: Sandbox, fake_rayd: RaydEndpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Desde M5 un reset con el agente vivo se reengancha con
    `Connect(pid, from_seq=last_seq + 1)`; el pid inventado del stream
    falso no existe, así que el resultado es `NotFoundException`."""
    reset = FakeRpcError(grpc.StatusCode.UNAVAILABLE, details="Socket closed")
    monkeypatch.setattr(
        sandbox._process,
        "Start",
        start_failing_first(sandbox._process.Start, [lambda: resetting_stream(reset)]),
    )
    health_calls = len(fake_rayd.servicer.health_calls)
    with pytest.raises(NotFoundException):
        sandbox.commands.run("echo hola")
    assert len(fake_rayd.servicer.health_calls) == health_calls + 1
    connect = fake_rayd.process.connect_requests[-1]
    assert (connect.pid, connect.from_seq) == (1, 1)


def test_repeated_resets_without_a_resume_end_with_the_m2_classification(
    sandbox: Sandbox, fake_rayd: RaydEndpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    always_running(monkeypatch, sandbox, fake_rayd.host)
    reset = FakeRpcError(grpc.StatusCode.UNAVAILABLE, details="Socket closed")
    stream_stub = sandbox._stub(process_pb2_grpc.ProcessServiceStub, stream=True)
    monkeypatch.setattr(stream_stub, "Start", lambda request, timeout=None: resetting_stream(reset))
    monkeypatch.setattr(
        stream_stub, "Connect", lambda request, timeout=None: resetting_stream(reset)
    )
    handle = sandbox.commands.run("echo hola", background=True)
    with pytest.raises(SandboxException, match=r"commands\.connect") as excinfo:
        handle.wait()
    assert not isinstance(excinfo.value, SandboxNotFoundException)
    assert handle.reconnects == 3


def test_stream_reset_with_dead_agent_maps_the_microvm_state(
    sandbox: Sandbox,
    fake_rayd: RaydEndpoint,
    control_plane: StubbedControlPlane,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset = FakeRpcError(grpc.StatusCode.UNAVAILABLE, details="Socket closed")
    stream_stub = sandbox._stub(process_pb2_grpc.ProcessServiceStub, stream=True)
    monkeypatch.setattr(
        stream_stub,
        "Start",
        start_failing_first(stream_stub.Start, [lambda: resetting_stream(reset)]),
    )
    monkeypatch.setattr(ReconnectPoll, "STATE_CHECK_INTERVAL", 0.1)
    fake_rayd.servicer.unavailable_calls = 10_000
    control_plane.microvms.add_response(
        "get_microvm",
        microvm_response(endpoint=fake_rayd.host, state="TERMINATED", state_reason="Success."),
    )
    handle = sandbox.commands.run("echo hola", background=True)
    with pytest.raises(SandboxNotFoundException, match="TERMINATED"):
        handle.wait()
    with pytest.raises(SandboxNotFoundException):
        handle.wait()


def test_at_most_two_channels_per_sandbox(sandbox: Sandbox, fake_rayd: RaydEndpoint) -> None:
    for index in range(30):
        assert sandbox.commands.run(f"echo {index}").stdout == f"{index}\n"
    assert len(fake_rayd.process.peers) == 1
    assert sandbox._stream_channel is None
    first = sandbox.commands.run("sleep 30", background=True)
    second = sandbox.commands.connect(first.pid)
    third = sandbox.commands.run("echo bg", background=True)
    assert third.wait().stdout == "bg\n"
    assert sandbox.commands.list()[0].pid == first.pid
    assert len(fake_rayd.process.peers) == 2
    assert first.kill() is True
    with pytest.raises(CommandExitException):
        second.wait()


def test_get_metrics_maps_the_response(sandbox: Sandbox) -> None:
    metrics = sandbox.get_metrics()
    assert metrics.cpu_used_pct == 12.5
    assert metrics.mem_used_bytes == 512 * 1024 * 1024
    assert metrics.mem_total_bytes == 2 * 1024 * 1024 * 1024
    assert metrics.disk_used_bytes == 1_000_000
    assert metrics.disk_total_bytes == 8_000_000
    assert metrics.cpu_count == 1
    assert int(metrics.timestamp.timestamp() * 1000) == METRICS_TIMESTAMP_UNIX_MS


def test_connect_with_a_wrong_token_is_unauthenticated(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    control_plane.microvms.add_response(
        "get_microvm", microvm_response(endpoint=fake_rayd.host, state="RUNNING")
    )
    control_plane.microvms.add_response("create_microvm_auth_token", auth_token_response())
    other = Sandbox.connect(
        SANDBOX_ID,
        access_token=generate_access_token(),
        control_plane=control_plane.plane,
        transport=fake_rayd.transport,
    )
    try:
        with pytest.raises(AuthenticationException) as excinfo:
            other.commands.run("echo hola")
        assert excinfo.value.grpc_code is grpc.StatusCode.UNAUTHENTICATED
        with pytest.raises(AuthenticationException):
            other.commands.list()
    finally:
        other.close()


def test_rotated_jwe_reaches_the_stream_channel_without_rebuilding_it(
    sandbox: Sandbox, fake_rayd: RaydEndpoint, control_plane: StubbedControlPlane
) -> None:
    first = sandbox.commands.run("echo uno", background=True)
    assert first.wait().stdout == "uno\n"
    stream_channel = sandbox._stream_channel
    stub_remint(control_plane, "jwe-rotated")
    sandbox._refresher.refresh_all()
    second = sandbox.commands.run("echo dos", background=True)
    assert second.wait().stdout == "dos\n"
    assert fake_rayd.process.start_metadata[-1][PROXY_AUTH_KEY] == "jwe-rotated"
    assert sandbox._stream_channel is stream_channel
    assert len(fake_rayd.process.peers) == 1
    control_plane.microvms.assert_no_pending_responses()
