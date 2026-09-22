"""El contrato de reconexión de `AsyncSandbox`: paridad con
`test_reconnect_sync.py` sobre `grpc.aio`."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

import grpc
import pytest

from rayito import AsyncCommandHandle, AsyncSandbox, Execution
from rayito._aws import sandbox_info_from_response
from rayito._limits import DEFAULT_PORT
from rayito._models import SandboxInfo
from rayito._sandbox_base import ReconnectPoll
from rayito._transport import PROXY_AUTH_KEY
from rayito.exceptions import (
    SandboxException,
    SandboxNotFoundException,
    SandboxStateException,
)
from rayito.v1 import filesystem_pb2, process_pb2

from .conftest import (
    ACCESS_TOKEN,
    IMAGE_ARN,
    SANDBOX_ID,
    FakeRpcError,
    RaydEndpoint,
    StubbedControlPlane,
    always_running,
    auth_token_response,
    microvm_response,
)
from .test_pty_async import output_line, read_until

HOME = "/home/user"
WAIT_BUDGET_SECONDS = 10.0
POLL_SECONDS = 0.02
FAST_STATE_CHECK_SECONDS = 0.1
SHORT_RECONNECT_TIMEOUT = 5.0


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
        reconnect_timeout=SHORT_RECONNECT_TIMEOUT,
    )
    try:
        yield created
    finally:
        control_plane.microvms.add_response(
            "terminate_microvm", {}, expected_params={"microvmIdentifier": SANDBOX_ID}
        )
        await created.kill()


@pytest.fixture
def fast_state_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ReconnectPoll, "STATE_CHECK_INTERVAL", FAST_STATE_CHECK_SECONDS)


async def wait_until(predicate: Callable[[], bool], budget: float = WAIT_BUDGET_SECONDS) -> None:
    deadline = time.monotonic() + budget
    while not predicate():
        assert time.monotonic() < deadline, "la condición no se cumplió a tiempo"
        await asyncio.sleep(POLL_SECONDS)


class Collector:
    """Consume un handle en una task acumulando stdout y la excepción final."""

    def __init__(self, handle: AsyncCommandHandle) -> None:
        self.handle = handle
        self.chunks: list[str] = []
        self.error: Exception | None = None
        self.task = asyncio.get_running_loop().create_task(self._run())

    async def _run(self) -> None:
        try:
            async for stdout, _, _ in self.handle:
                if stdout is not None:
                    self.chunks.append(stdout)
        except Exception as exc:
            self.error = exc

    async def join(self, budget: float = WAIT_BUDGET_SECONDS) -> None:
        await asyncio.wait_for(self.task, timeout=budget)


class MicrovmState:
    """`get_microvm` sustituible en caliente, como en `test_reconnect_sync`."""

    def __init__(self, host: str, state: str, *, auto_resume: bool) -> None:
        self.host = host
        self.state = state
        self.auto_resume = auto_resume
        self.calls = 0

    def get_microvm(self, sandbox_id: str) -> SandboxInfo:
        self.calls += 1
        return sandbox_info_from_response(
            microvm_response(
                endpoint=self.host,
                state=self.state,
                idle={
                    "maxIdleDurationSeconds": 60,
                    "suspendedDurationSeconds": 0,
                    "autoResumeEnabled": self.auto_resume,
                },
            )
        )


def install_state(
    monkeypatch: pytest.MonkeyPatch,
    sandbox: Any,
    host: str,
    state: str,
    *,
    auto_resume: bool = True,
) -> MicrovmState:
    microvm = MicrovmState(host, state, auto_resume=auto_resume)
    monkeypatch.setattr(sandbox._control_plane, "get_microvm", microvm.get_microvm)
    return microvm


def stub_pause_and_resume(control_plane: StubbedControlPlane) -> None:
    control_plane.microvms.add_response("suspend_microvm", {}, {"microvmIdentifier": SANDBOX_ID})
    control_plane.microvms.add_response("resume_microvm", {}, {"microvmIdentifier": SANDBOX_ID})
    control_plane.microvms.add_response("create_microvm_auth_token", auth_token_response("jwe-2"))


def health_calls(fake_rayd: RaydEndpoint) -> int:
    return len(fake_rayd.servicer.health_calls)


class ScriptedCall:
    def __init__(self, events: list[Any], error: grpc.RpcError) -> None:
        self._events = list(events)
        self._error = error

    async def read(self) -> Any:
        if self._events:
            return self._events.pop(0)
        raise self._error

    def cancel(self) -> bool:
        return True


async def test_async_background_handle_survives_a_suspend_resume(
    sandbox: AsyncSandbox, fake_rayd: RaydEndpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    always_running(monkeypatch, sandbox, fake_rayd.host)
    handle = await sandbox.commands.run("seq 40", background=True, timeout=None)
    collector = Collector(handle)
    await wait_until(lambda: len(collector.chunks) >= 2)
    seen_before = handle.last_seq
    health_before = health_calls(fake_rayd)
    fake_rayd.suspend_resume(3)
    await collector.join()
    assert collector.error is None
    assert "".join(collector.chunks) == "".join(f"{n}\n" for n in range(1, 41))
    assert (await handle.wait()).exit_code == 0
    assert handle.reconnects == 1
    connect = fake_rayd.process.connect_requests[-1]
    assert connect.pid == handle.pid
    assert seen_before < connect.from_seq <= handle.last_seq + 1
    assert health_calls(fake_rayd) - health_before == 4
    assert sandbox.resume_generation == 1


async def test_async_unread_handle_reconnects_on_its_first_wait(
    sandbox: AsyncSandbox, fake_rayd: RaydEndpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    always_running(monkeypatch, sandbox, fake_rayd.host)
    handle = await sandbox.commands.run("sleep 1", background=True, timeout=None)
    fake_rayd.suspend_resume(1)
    assert (await handle.wait()).exit_code == 0
    assert handle.reconnects == 1
    assert fake_rayd.process.connect_requests[-1].from_seq == 1


async def test_async_out_of_range_falls_back_with_a_warning(
    sandbox: AsyncSandbox,
    fake_rayd: RaydEndpoint,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    always_running(monkeypatch, sandbox, fake_rayd.host)
    handle = await sandbox.commands.run("seq 40", background=True, timeout=None)
    collector = Collector(handle)
    await wait_until(lambda: len(collector.chunks) >= 2)
    fake_rayd.suspend()
    process = fake_rayd.process.processes[handle.pid]
    await wait_until(lambda: len(process.ring) >= handle.last_seq + 3)
    with process.lock:
        del process.ring[:]
    with caplog.at_level(logging.WARNING, logger="rayito.commands"):
        fake_rayd.resume()
        await collector.join()
    assert collector.error is None
    assert handle.reconnects == 1
    assert fake_rayd.process.connect_requests[-1].from_seq == 0
    assert any("se perdió salida" in record.message for record in caplog.records)


async def test_async_pty_handle_reconnects_through_pty_connect(
    sandbox: AsyncSandbox, fake_rayd: RaydEndpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    always_running(monkeypatch, sandbox, fake_rayd.host)
    pty = await sandbox.pty.create(timeout=None)
    await pty.send_input("echo uno\n")
    await read_until(pty, output_line("uno"))
    seen = pty.last_seq
    fake_rayd.suspend_resume(2)
    await sandbox.pty.send_input(pty.pid, "echo dos\n")
    buffer = await read_until(pty, output_line("dos"))
    assert b"uno" not in buffer
    assert pty.reconnects == 1
    connect = fake_rayd.pty.connect_requests[-1]
    assert (connect.pid, connect.from_seq) == (pty.pid, seen + 1)
    assert await pty.kill() is True


async def test_async_watch_handle_reissues_watch_dir(
    sandbox: AsyncSandbox, fake_rayd: RaydEndpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    always_running(monkeypatch, sandbox, fake_rayd.host)
    exits: list[Exception] = []
    handle = await sandbox.files.watch_dir(HOME, recursive=True, on_exit=exits.append)
    fake_rayd.suspend_resume(2)
    await wait_until(lambda: len(fake_rayd.filesystem.watch_calls) == 2)
    await wait_until(lambda: fake_rayd.filesystem.live_watches == 1)
    fake_rayd.filesystem.push_event(HOME, "after.txt", filesystem_pb2.FILESYSTEM_EVENT_TYPE_CREATE)
    seen: list[str] = []

    async def arrived() -> bool:
        seen.extend(event.name for event in await handle.get_new_events())
        return "after.txt" in seen

    deadline = time.monotonic() + WAIT_BUDGET_SECONDS
    while not await arrived():
        assert time.monotonic() < deadline
        await asyncio.sleep(POLL_SECONDS)
    assert handle.is_running is True
    assert handle.reconnects == 1
    assert exits == []
    first, second = fake_rayd.filesystem.watch_calls
    assert second == first
    assert first.recursive is True
    await handle.stop()


async def test_async_run_code_reattaches_after_started(
    sandbox: AsyncSandbox, fake_rayd: RaydEndpoint
) -> None:
    task: asyncio.Task[Execution] = asyncio.get_running_loop().create_task(
        sandbox.run_code("slow 2", timeout=None)
    )
    await wait_until(lambda: bool(fake_rayd.code.runs))
    run_record = next(iter(fake_rayd.code.runs.values()))
    await wait_until(lambda: len(run_record.ring) >= 2)
    fake_rayd.suspend_resume(2)
    execution = await asyncio.wait_for(task, timeout=WAIT_BUDGET_SECONDS)
    assert execution.error is None
    assert execution.text == "'slow'"
    assert "".join(execution.logs.stdout) == "slow start\n"
    reattach = fake_rayd.code.reattach_requests[-1]
    assert reattach.execution_id == run_record.record.execution_id
    assert reattach.from_seq in (2, 3)
    assert len(fake_rayd.code.execute_requests) == 1
    assert run_record.record.interrupted is False


async def test_async_run_code_cut_before_started_is_not_retried(
    sandbox: AsyncSandbox, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.code.phase = "suspending"
    with pytest.raises(SandboxStateException, match="suspending"):
        await sandbox.run_code("1+1")
    assert len(fake_rayd.code.execute_requests) == 1
    fake_rayd.code.phase = None


async def test_async_unary_is_retried_once_after_a_reconnect(
    sandbox: AsyncSandbox, fake_rayd: RaydEndpoint
) -> None:
    handle = await sandbox.commands.run("sleep 30", background=True, timeout=None)
    fake_rayd.process.signal_unavailable_calls = 1
    fake_rayd.resume()
    assert await sandbox.commands.kill(handle.pid) is True
    assert len(fake_rayd.process.signal_requests) == 2


async def test_async_terminated_during_the_poll_is_not_found(
    sandbox: AsyncSandbox,
    fake_rayd: RaydEndpoint,
    control_plane: StubbedControlPlane,
    fast_state_checks: None,
) -> None:
    handle = await sandbox.commands.run("sleep 30", background=True, timeout=None)
    fake_rayd.suspend()
    fake_rayd.servicer.unavailable_calls = 10_000
    control_plane.microvms.add_response(
        "get_microvm",
        microvm_response(endpoint=fake_rayd.host, state="TERMINATED", state_reason="Success."),
    )
    with pytest.raises(SandboxNotFoundException, match="TERMINATED"):
        await handle.wait()


async def test_async_suspended_without_auto_resume_stops_a_foreground_run(
    sandbox: AsyncSandbox,
    fake_rayd: RaydEndpoint,
    monkeypatch: pytest.MonkeyPatch,
    fast_state_checks: None,
) -> None:
    install_state(monkeypatch, sandbox, fake_rayd.host, "SUSPENDED", auto_resume=False)
    fake_rayd.servicer.unavailable_calls = 10_000
    asyncio.get_running_loop().call_later(0.2, fake_rayd.suspend)
    started = time.monotonic()
    with pytest.raises(SandboxStateException, match=r"resume\(\)"):
        await sandbox.commands.run("sleep 30", timeout=None)
    assert time.monotonic() - started < SHORT_RECONNECT_TIMEOUT
    fake_rayd.resume()


@pytest.mark.parametrize("auto_resume", [True, False])
async def test_async_background_handle_sleeps_through_a_suspension(
    sandbox: AsyncSandbox,
    fake_rayd: RaydEndpoint,
    monkeypatch: pytest.MonkeyPatch,
    fast_state_checks: None,
    auto_resume: bool,
) -> None:
    sandbox._reconnect_timeout = 0.5
    microvm = install_state(
        monkeypatch, sandbox, fake_rayd.host, "SUSPENDED", auto_resume=auto_resume
    )
    handle = await sandbox.commands.run("sleep 1", background=True, timeout=None)
    fake_rayd.suspend()
    health_before = health_calls(fake_rayd)
    collector = Collector(handle)
    await asyncio.sleep(3 * sandbox._reconnect_timeout)
    assert not collector.task.done()
    assert health_calls(fake_rayd) == health_before
    assert microvm.calls >= 2
    fake_rayd.resume()
    microvm.state = "RUNNING"
    await collector.join()
    assert collector.error is None
    assert (await handle.wait()).exit_code == 0
    assert handle.reconnects == 1
    assert health_calls(fake_rayd) - health_before == 1


async def test_async_resume_wakes_a_dormant_handle_at_once(
    sandbox: AsyncSandbox,
    fake_rayd: RaydEndpoint,
    control_plane: StubbedControlPlane,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    microvm = install_state(monkeypatch, sandbox, fake_rayd.host, "SUSPENDED")
    control_plane.microvms.add_response("resume_microvm", {}, {"microvmIdentifier": SANDBOX_ID})
    control_plane.microvms.add_response("create_microvm_auth_token", auth_token_response("jwe-2"))
    handle = await sandbox.commands.run("sleep 1", background=True, timeout=None)
    fake_rayd.suspend()
    collector = Collector(handle)
    await wait_until(lambda: microvm.calls >= 1)
    fake_rayd.resume()
    started = time.monotonic()
    await sandbox.resume()
    await collector.join(2.0)
    assert time.monotonic() - started < 2.0
    assert collector.error is None
    assert handle.reconnects == 1


async def test_async_dormant_handle_does_not_block_a_foreground_call(
    sandbox: AsyncSandbox, fake_rayd: RaydEndpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    microvm = install_state(monkeypatch, sandbox, fake_rayd.host, "SUSPENDED")
    handle = await sandbox.commands.run("sleep 1", background=True, timeout=None)
    fake_rayd.suspend()
    collector = Collector(handle)
    await wait_until(lambda: microvm.calls >= 1)
    health_before = health_calls(fake_rayd)
    asyncio.get_running_loop().call_later(0.3, fake_rayd.resume)
    started = time.monotonic()
    result = await sandbox.commands.run("echo hola")
    assert result.stdout.strip() == "hola"
    assert time.monotonic() - started < ReconnectPoll.STATE_CHECK_INTERVAL
    assert health_calls(fake_rayd) > health_before
    await collector.join(2.0)
    assert collector.error is None
    assert handle.reconnects == 1


async def test_async_explicit_pause_keeps_a_foreground_run_dormant_until_resume(
    sandbox: AsyncSandbox,
    fake_rayd: RaydEndpoint,
    control_plane: StubbedControlPlane,
    monkeypatch: pytest.MonkeyPatch,
    fast_state_checks: None,
) -> None:
    microvm = install_state(monkeypatch, sandbox, fake_rayd.host, "RUNNING")
    stub_pause_and_resume(control_plane)
    task = asyncio.get_running_loop().create_task(sandbox.commands.run("sleep 1", timeout=None))
    await wait_until(lambda: bool(fake_rayd.process.processes))
    assert await sandbox.pause(wait=False) is True
    assert sandbox._paused is True
    microvm.state = "SUSPENDED"
    calls_at_pause = microvm.calls
    fake_rayd.suspend()
    health_before = health_calls(fake_rayd)
    await wait_until(lambda: microvm.calls >= calls_at_pause + 2)
    assert not task.done()
    assert health_calls(fake_rayd) == health_before
    fake_rayd.resume()
    await sandbox.resume()
    result = await asyncio.wait_for(task, timeout=WAIT_BUDGET_SECONDS)
    assert result.exit_code == 0
    assert sandbox._paused is False


async def test_async_explicit_pause_keeps_run_code_dormant_until_resume(
    sandbox: AsyncSandbox,
    fake_rayd: RaydEndpoint,
    control_plane: StubbedControlPlane,
    monkeypatch: pytest.MonkeyPatch,
    fast_state_checks: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    microvm = install_state(monkeypatch, sandbox, fake_rayd.host, "RUNNING")
    stub_pause_and_resume(control_plane)
    task: asyncio.Task[Execution] = asyncio.get_running_loop().create_task(
        sandbox.run_code("slow 2", timeout=None)
    )
    await wait_until(lambda: bool(fake_rayd.code.runs))
    run_record = next(iter(fake_rayd.code.runs.values()))
    await wait_until(lambda: len(run_record.ring) >= 2)
    assert await sandbox.pause(wait=False) is True
    microvm.state = "SUSPENDED"
    calls_at_pause = microvm.calls
    fake_rayd.suspend()
    health_before = health_calls(fake_rayd)
    await wait_until(lambda: microvm.calls >= calls_at_pause + 2)
    assert not task.done()
    assert health_calls(fake_rayd) == health_before
    fake_rayd.resume()
    with caplog.at_level(logging.INFO, logger="rayito.code"):
        await sandbox.resume()
        execution = await asyncio.wait_for(task, timeout=WAIT_BUDGET_SECONDS)
    assert execution.text == "'slow'"
    assert execution.error is None
    assert fake_rayd.code.reattach_requests[-1].execution_id == run_record.record.execution_id
    assert any("Reattach" in record.message for record in caplog.records)


async def test_async_reconnect_deadline_is_a_sandbox_exception(
    sandbox: AsyncSandbox, fake_rayd: RaydEndpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    reset = FakeRpcError(grpc.StatusCode.UNAVAILABLE, details="Socket closed")
    monkeypatch.setattr(
        sandbox._process,
        "Start",
        lambda request, timeout=None: ScriptedCall(
            [process_pb2.ProcessEvent(start=process_pb2.StartEvent(pid=1))], reset
        ),
    )
    fake_rayd.servicer.unavailable_calls = 10_000
    health_before = health_calls(fake_rayd)
    with pytest.raises(SandboxException, match="no volvió a responder") as excinfo:
        await sandbox.commands.run("sleep 30", timeout=None)
    assert type(excinfo.value) is SandboxException
    assert health_calls(fake_rayd) - health_before >= 4


async def test_async_disconnected_handles_never_reconnect(
    sandbox: AsyncSandbox, fake_rayd: RaydEndpoint
) -> None:
    handle = await sandbox.commands.run("sleep 30", background=True, timeout=None)
    handle.disconnect()
    fake_rayd.suspend_resume(1)
    with pytest.raises(SandboxException, match="desconectado"):
        await handle.wait()
    assert fake_rayd.process.connect_requests == []
    assert await sandbox.commands.kill(handle.pid) is True


async def test_async_two_handles_share_one_reconnect_poll(
    sandbox: AsyncSandbox, fake_rayd: RaydEndpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    always_running(monkeypatch, sandbox, fake_rayd.host)
    first = await sandbox.commands.run("seq 40", background=True, timeout=None)
    second = await sandbox.commands.connect(first.pid, from_seq=1)
    collectors = [Collector(first), Collector(second)]
    await wait_until(lambda: all(len(collector.chunks) >= 2 for collector in collectors))
    health_before = health_calls(fake_rayd)
    fake_rayd.suspend_resume(3)
    for collector in collectors:
        await collector.join()
        assert collector.error is None
        assert "".join(collector.chunks) == "".join(f"{n}\n" for n in range(1, 41))
    assert first.reconnects == 1
    assert second.reconnects == 1
    assert health_calls(fake_rayd) - health_before == 4


async def test_async_resume_remints_and_get_health(
    sandbox: AsyncSandbox,
    fake_rayd: RaydEndpoint,
    control_plane: StubbedControlPlane,
    caplog: pytest.LogCaptureFixture,
) -> None:
    control_plane.microvms.add_response(
        "get_microvm", microvm_response(endpoint=fake_rayd.host, state="RUNNING")
    )
    control_plane.microvms.add_response("suspend_microvm", {}, {"microvmIdentifier": SANDBOX_ID})
    control_plane.microvms.add_response(
        "get_microvm", microvm_response(endpoint=fake_rayd.host, state="SUSPENDED")
    )
    control_plane.microvms.add_response(
        "get_microvm", microvm_response(endpoint=fake_rayd.host, state="SUSPENDED")
    )
    control_plane.microvms.add_response("resume_microvm", {}, {"microvmIdentifier": SANDBOX_ID})
    control_plane.microvms.add_response("create_microvm_auth_token", auth_token_response("jwe-2"))
    assert await sandbox.pause() is True
    assert await sandbox.pause() is False
    assert sandbox._refresher._task is not None and not sandbox._refresher._task.done()
    fake_rayd.suspend_resume(0, clock_offset_ms=-7000)
    with caplog.at_level(logging.WARNING, logger="rayito.sandbox"):
        await sandbox.resume()
    assert sandbox.resume_generation == 1
    assert fake_rayd.servicer.health_calls[-1][PROXY_AUTH_KEY] == "jwe-2"
    assert any("-7000 ms" in record.message for record in caplog.records)
    health = await sandbox.get_health()
    assert health.resume_generation == 1
    assert health.clock_offset_ms == -7000
    assert health.kernel_state_lost is False
    control_plane.microvms.assert_no_pending_responses()
