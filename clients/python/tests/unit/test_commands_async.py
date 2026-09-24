"""`AsyncSandbox.commands` y `AsyncCommandHandle`: misma superficie que la
versión síncrona sobre `grpc.aio`, contra el mismo `rayd` falso."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

import grpc
import pytest

from rayito import AsyncCommandHandle, AsyncSandbox, CommandResult
from rayito._limits import DEFAULT_PORT
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
from rayito.v1 import process_pb2

from .conftest import (
    ACCESS_TOKEN,
    IMAGE_ARN,
    METRICS_TIMESTAMP_UNIX_MS,
    SANDBOX_ID,
    FakeRpcError,
    RaydEndpoint,
    StubbedControlPlane,
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
        control_plane.microvms.add_response(
            "terminate_microvm", {}, expected_params={"microvmIdentifier": SANDBOX_ID}
        )
        await created.kill()


def proxy_forbidden() -> FakeRpcError:
    return FakeRpcError(
        grpc.StatusCode.PERMISSION_DENIED,
        details="Received http2 header with status: 403",
        debug=f'{{"grpc_status":7,"description":"{PROXY_FORBIDDEN_MARKER}"}}',
    )


class ScriptedCall:
    """Una `UnaryStreamCall` de `grpc.aio` con mensajes fijos y un error final."""

    def __init__(self, events: list[Any], error: grpc.RpcError) -> None:
        self._events = list(events)
        self._error = error

    async def read(self) -> Any:
        if self._events:
            return self._events.pop(0)
        raise self._error

    def cancel(self) -> bool:
        return True


def failing_call(error: grpc.RpcError) -> ScriptedCall:
    return ScriptedCall([], error)


def resetting_call(error: grpc.RpcError) -> ScriptedCall:
    return ScriptedCall([process_pb2.ProcessEvent(start=process_pb2.StartEvent(pid=1))], error)


def start_failing_first(
    real: Callable[..., Any], failures: list[Callable[[], ScriptedCall]]
) -> Callable[..., Any]:
    def start(request: Any, timeout: float | None = None) -> Any:
        if failures:
            return failures.pop(0)()
        return real(request, timeout=timeout)

    return start


async def live_pids(sandbox: AsyncSandbox) -> set[int]:
    return {info.pid for info in await sandbox.commands.list()}


async def test_async_foreground_and_background_parity(
    sandbox: AsyncSandbox, fake_rayd: RaydEndpoint
) -> None:
    result = await sandbox.commands.run("echo hola")
    assert result == CommandResult(stdout="hola\n", stderr="", exit_code=0)
    handle = await sandbox.commands.run("echo hola", background=True)
    assert isinstance(handle, AsyncCommandHandle)
    assert await handle.wait() == result
    assert handle.exit_code == 0
    assert handle.last_seq == 1
    assert await handle.wait() is await handle.wait()
    request = fake_rayd.process.start_requests[-1]
    assert list(request.process.args) == ["-l", "-c", "echo hola"]
    assert request.timeout_ms == 60_000


async def test_async_callbacks_and_options(sandbox: AsyncSandbox, fake_rayd: RaydEndpoint) -> None:
    out: list[str] = []
    err: list[str] = []
    await sandbox.commands.run(
        "err b", envs={"A": "1"}, cwd="/tmp", on_stdout=out.append, on_stderr=err.append
    )
    assert (out, err) == ([], ["b\n"])
    request = fake_rayd.process.start_requests[-1]
    assert dict(request.process.envs) == {"A": "1"}
    assert request.process.cwd == "/tmp"


async def test_async_wait_awaits_async_callbacks(
    sandbox: AsyncSandbox, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.process.reply_when("echo out", CannedReply(stdout="out\n", stderr="err\n"))
    out: list[str] = []
    err: list[str] = []

    async def async_append(text: str) -> None:
        await asyncio.sleep(0)
        out.append(text)

    handle = await sandbox.commands.run("echo out; echo err >&2", background=True)
    result = await handle.wait(on_stdout=async_append, on_stderr=err.append)
    assert (out, err) == (["out\n"], ["err\n"])
    assert (result.stdout, result.stderr, result.exit_code) == ("out\n", "err\n", 0)


async def test_async_exit_timeout_and_kill(sandbox: AsyncSandbox) -> None:
    with pytest.raises(CommandExitException) as exit_info:
        await sandbox.commands.run("exit 3")
    assert (exit_info.value.exit_code, exit_info.value.error) == (3, "exited")

    started = time.perf_counter()
    with pytest.raises(TimeoutException):
        await sandbox.commands.run("sleep 10", timeout=0.2)
    assert time.perf_counter() - started < TIMEOUT_LATENCY_BUDGET_SECONDS

    handle = await sandbox.commands.run("sleep 30", background=True, tag="m2")
    listed = [info for info in await sandbox.commands.list() if info.pid == handle.pid]
    assert listed[0].kind == "process" and listed[0].tag == "m2"
    assert await handle.kill() is True
    with pytest.raises(CommandExitException) as killed:
        await handle.wait()
    assert (killed.value.exit_code, killed.value.error) == (137, "signaled")
    assert await sandbox.commands.kill(handle.pid) is False
    assert handle.pid not in await live_pids(sandbox)


async def test_async_stdin_round_trip(sandbox: AsyncSandbox) -> None:
    handle = await sandbox.commands.run("cat", background=True, stdin=True)
    await handle.send_stdin("hola\n")
    await handle.close_stdin()
    assert (await handle.wait()).stdout == "hola\n"
    without_stdin = await sandbox.commands.run("sleep 30", background=True)
    with pytest.raises(InvalidArgumentException):
        await without_stdin.send_stdin("x")
    assert await without_stdin.kill() is True


async def test_async_connect_replay_and_iteration(
    sandbox: AsyncSandbox, fake_rayd: RaydEndpoint
) -> None:
    handle = await sandbox.commands.run("seq 3", background=True)
    full = await sandbox.commands.connect(handle.pid, from_seq=1)
    chunks = [chunk async for chunk in full]
    assert chunks == [("1\n", None, None), ("2\n", None, None), ("3\n", None, None)]
    assert (await full.wait()).stdout == "1\n2\n3\n"
    assert (await handle.wait()).stdout == "1\n2\n3\n"
    retained = await sandbox.commands.connect(handle.pid)
    assert (await retained.wait()).exit_code == 0
    with pytest.raises(NotFoundException):
        await sandbox.commands.connect(999_999)
    with pytest.raises(NotFoundException):
        await sandbox.commands.connect(handle.pid, from_seq=999)
    fake_rayd.process.retention_seconds = 0
    with pytest.raises(NotFoundException):
        await sandbox.commands.connect(handle.pid)


async def test_async_large_output_and_split_character(sandbox: AsyncSandbox) -> None:
    assert len((await sandbox.commands.run("big 3000000")).stdout) == 3_000_000
    assert (await sandbox.commands.run("split")).stdout == "a" * (CHUNK_SIZE - 1) + "é\n"


async def test_async_output_truncated_and_disconnect(sandbox: AsyncSandbox) -> None:
    truncated = await sandbox.commands.run("truncate", background=True)
    with pytest.raises(SandboxException, match="output_truncated"):
        await truncated.wait()
    assert truncated.pid in await live_pids(sandbox)
    assert await truncated.kill() is True

    detached = await sandbox.commands.run("sleep 30", background=True)
    detached.disconnect()
    assert detached.pid in await live_pids(sandbox)
    with pytest.raises(SandboxException, match="desconectado"):
        await detached.wait()
    assert await detached.kill() is True


async def test_async_disconnect_from_another_task_ends_wait(sandbox: AsyncSandbox) -> None:
    handle = await sandbox.commands.run("sleep 30", background=True)
    asyncio.get_running_loop().call_later(DISCONNECT_DELAY_SECONDS, handle.disconnect)
    with pytest.raises(SandboxException, match="desconectado"):
        await handle.wait()
    with pytest.raises(SandboxException, match="desconectado"):
        await handle.wait()
    assert handle.pid in await live_pids(sandbox)
    assert await handle.kill() is True


async def test_async_phase_gate_waits_for_a_resume_then_is_a_state_error(
    sandbox: AsyncSandbox, fake_rayd: RaydEndpoint
) -> None:
    sandbox._reconnect_timeout = 0.3
    health_calls = len(fake_rayd.servicer.health_calls)
    for phase in ("suspending", "terminating"):
        fake_rayd.process.phase = phase
        with pytest.raises(SandboxStateException, match=phase):
            await sandbox.commands.run("echo hola")
        with pytest.raises(SandboxStateException, match=phase):
            await sandbox.commands.connect(1)
    fake_rayd.process.phase = None
    assert len(fake_rayd.servicer.health_calls) > health_calls
    assert (await sandbox.commands.run("echo hola")).stdout == "hola\n"


async def test_async_root_refused_and_invalid_cwd(sandbox: AsyncSandbox) -> None:
    with pytest.raises(AuthenticationException) as excinfo:
        await sandbox.commands.run("whoami", user="root")
    assert excinfo.value.proxy_rejected is False
    with pytest.raises(InvalidArgumentException, match="cwd"):
        await sandbox.commands.run("pwd", cwd="/does/not/exist")


async def test_async_proxy_403_on_stream_open_remints_once(
    sandbox: AsyncSandbox,
    fake_rayd: RaydEndpoint,
    control_plane: StubbedControlPlane,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub_remint(control_plane, "jwe-2")
    monkeypatch.setattr(
        sandbox._process,
        "Start",
        start_failing_first(sandbox._process.Start, [lambda: failing_call(proxy_forbidden())]),
    )
    assert (await sandbox.commands.run("echo hola")).stdout == "hola\n"
    assert len(fake_rayd.process.start_requests) == 1
    assert fake_rayd.process.start_metadata[-1][PROXY_AUTH_KEY] == "jwe-2"
    control_plane.microvms.assert_no_pending_responses()


async def test_async_stream_reset_classification(
    sandbox: AsyncSandbox,
    fake_rayd: RaydEndpoint,
    control_plane: StubbedControlPlane,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset = FakeRpcError(grpc.StatusCode.UNAVAILABLE, details="Socket closed")
    monkeypatch.setattr(
        sandbox._process,
        "Start",
        start_failing_first(
            sandbox._process.Start,
            [lambda: resetting_call(reset), lambda: resetting_call(reset)],
        ),
    )
    with pytest.raises(NotFoundException):
        await sandbox.commands.run("echo hola")
    assert fake_rayd.process.connect_requests[-1].pid == 1
    monkeypatch.setattr(ReconnectPoll, "STATE_CHECK_INTERVAL", 0.1)
    fake_rayd.servicer.unavailable_calls = 10_000
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
    with pytest.raises(SandboxStateException) as excinfo:
        await sandbox.commands.run("echo hola")
    assert not isinstance(excinfo.value, SandboxNotFoundException)
    assert "resume()" in str(excinfo.value)


async def test_async_at_most_two_channels(sandbox: AsyncSandbox, fake_rayd: RaydEndpoint) -> None:
    for index in range(30):
        assert (await sandbox.commands.run(f"echo {index}")).stdout == f"{index}\n"
    assert len(fake_rayd.process.peers) == 1
    assert sandbox._stream_channel is None
    first = await sandbox.commands.run("sleep 30", background=True)
    second = await sandbox.commands.connect(first.pid)
    assert len(fake_rayd.process.peers) == 2
    assert await first.kill() is True
    with pytest.raises(CommandExitException):
        await second.wait()


async def test_async_get_metrics(sandbox: AsyncSandbox) -> None:
    metrics = await sandbox.get_metrics()
    assert metrics.cpu_count == 1
    assert metrics.cpu_used_pct == 12.5
    assert int(metrics.timestamp.timestamp() * 1000) == METRICS_TIMESTAMP_UNIX_MS
