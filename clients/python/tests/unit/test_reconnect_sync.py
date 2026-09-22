"""El contrato de reconexión de `Sandbox` contra el `rayd` falso: handles,
PTY, watches y `run_code` que sobreviven a un ciclo suspend/resume, el
reintento unario, las paradas del sondeo y `pause()`/`resume()` con el
Stubber."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

import grpc
import pytest

from rayito import CommandHandle, Execution, PtyHandle, Sandbox
from rayito._aws import sandbox_info_from_response
from rayito._limits import DEFAULT_PORT
from rayito._models import SandboxInfo
from rayito._sandbox_base import ReconnectPoll
from rayito._transport import PROXY_AUTH_KEY
from rayito.exceptions import (
    CommandExitException,
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
from .test_pty_sync import output_line, read_until

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
def sandbox(control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint) -> Iterator[Sandbox]:
    stub_launch(control_plane, fake_rayd)
    created = Sandbox.create(
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
        created.kill()


@pytest.fixture
def fast_state_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ReconnectPoll, "STATE_CHECK_INTERVAL", FAST_STATE_CHECK_SECONDS)


def wait_until(predicate: Callable[[], bool], budget: float = WAIT_BUDGET_SECONDS) -> None:
    deadline = time.monotonic() + budget
    while not predicate():
        assert time.monotonic() < deadline, "la condición no se cumplió a tiempo"
        time.sleep(POLL_SECONDS)


class Collector:
    """Itera un handle en un hilo daemon acumulando stdout y la excepción final."""

    def __init__(self, handle: CommandHandle) -> None:
        self.handle = handle
        self.chunks: list[str] = []
        self.error: Exception | None = None
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        try:
            for stdout, _, _ in self.handle:
                if stdout is not None:
                    self.chunks.append(stdout)
        except Exception as exc:
            self.error = exc

    def join(self, budget: float = WAIT_BUDGET_SECONDS) -> None:
        self.thread.join(budget)
        assert not self.thread.is_alive(), "el consumidor sigue vivo"


class MicrovmState:
    """`get_microvm` sustituible en caliente: el Stubber exige un número exacto
    de llamadas y un poller dormido hace las que necesite."""

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


def resetting_stream(error: grpc.RpcError) -> Iterator[Any]:
    yield process_pb2.ProcessEvent(start=process_pb2.StartEvent(pid=1))
    raise error


def test_background_handle_survives_a_suspend_resume(
    sandbox: Sandbox, fake_rayd: RaydEndpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    always_running(monkeypatch, sandbox, fake_rayd.host)
    handle = sandbox.commands.run("seq 40", background=True, timeout=None)
    collector = Collector(handle)
    wait_until(lambda: len(collector.chunks) >= 2)
    seen_before = handle.last_seq
    health_before = health_calls(fake_rayd)
    fake_rayd.suspend_resume(3)
    collector.join()
    assert collector.error is None
    assert "".join(collector.chunks) == "".join(f"{n}\n" for n in range(1, 41))
    assert handle.wait().exit_code == 0
    assert handle.reconnects == 1
    connect = fake_rayd.process.connect_requests[-1]
    assert connect.pid == handle.pid
    assert seen_before < connect.from_seq <= handle.last_seq + 1
    assert health_calls(fake_rayd) - health_before == 4
    assert sandbox.resume_generation == 1


def test_unread_handle_reconnects_on_its_first_wait(
    sandbox: Sandbox, fake_rayd: RaydEndpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    always_running(monkeypatch, sandbox, fake_rayd.host)
    handle = sandbox.commands.run("sleep 1", background=True, timeout=None)
    fake_rayd.suspend_resume(1)
    assert handle.wait().exit_code == 0
    assert handle.reconnects == 1
    assert fake_rayd.process.connect_requests[-1].from_seq == 1


def test_out_of_range_falls_back_to_live_output_with_a_warning(
    sandbox: Sandbox,
    fake_rayd: RaydEndpoint,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    always_running(monkeypatch, sandbox, fake_rayd.host)
    handle = sandbox.commands.run("seq 40", background=True, timeout=None)
    collector = Collector(handle)
    wait_until(lambda: len(collector.chunks) >= 2)
    fake_rayd.suspend()
    process = fake_rayd.process.processes[handle.pid]
    wait_until(lambda: len(process.ring) >= handle.last_seq + 3)
    with process.lock:
        del process.ring[:]
    with caplog.at_level(logging.WARNING, logger="rayito.commands"):
        fake_rayd.resume()
        collector.join()
    assert collector.error is None
    assert handle.reconnects == 1
    requests = fake_rayd.process.connect_requests
    assert [request.from_seq for request in requests[-2:]] == [requests[-2].from_seq, 0]
    assert requests[-2].from_seq > 0
    assert any("se perdió salida" in record.message for record in caplog.records)
    assert handle.wait().exit_code == 0


def test_pty_handle_reconnects_through_pty_connect(
    sandbox: Sandbox, fake_rayd: RaydEndpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    always_running(monkeypatch, sandbox, fake_rayd.host)
    pty = sandbox.pty.create(timeout=None)
    assert isinstance(pty, PtyHandle)
    pty.send_input("echo uno\n")
    read_until(pty, output_line("uno"))
    seen = pty.last_seq
    fake_rayd.suspend_resume(2)
    sandbox.pty.send_input(pty.pid, "echo dos\n")
    buffer = read_until(pty, output_line("dos"))
    assert b"uno" not in buffer
    assert pty.reconnects == 1
    connect = fake_rayd.pty.connect_requests[-1]
    assert (connect.pid, connect.from_seq) == (pty.pid, seen + 1)
    assert fake_rayd.process.connect_requests == []
    assert pty.kill() is True


def test_watch_handle_reissues_watch_dir(
    sandbox: Sandbox, fake_rayd: RaydEndpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    always_running(monkeypatch, sandbox, fake_rayd.host)
    exits: list[Exception] = []
    handle = sandbox.files.watch_dir(HOME, recursive=True, include_entry=True, on_exit=exits.append)
    fake_rayd.filesystem.push_event(HOME, "before.txt", filesystem_pb2.FILESYSTEM_EVENT_TYPE_CREATE)
    wait_until(lambda: len(handle.get_new_events()) == 1)
    fake_rayd.suspend_resume(2)
    wait_until(lambda: len(fake_rayd.filesystem.watch_calls) == 2)
    wait_until(lambda: fake_rayd.filesystem.live_watches == 1)
    fake_rayd.filesystem.push_event(HOME, "after.txt", filesystem_pb2.FILESYSTEM_EVENT_TYPE_CREATE)
    wait_until(lambda: any(event.name == "after.txt" for event in handle.get_new_events()))
    assert handle.is_running is True
    assert handle.reconnects == 1
    assert exits == []
    first, second = fake_rayd.filesystem.watch_calls
    assert (first.path, first.recursive, first.include_entry) == (HOME, True, True)
    assert second == first
    handle.stop()
    assert handle.is_running is False


def test_run_code_reattaches_after_started(sandbox: Sandbox, fake_rayd: RaydEndpoint) -> None:
    results: list[Execution] = []
    errors: list[Exception] = []

    def run() -> None:
        try:
            results.append(sandbox.run_code("slow 2", timeout=None))
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    wait_until(lambda: bool(fake_rayd.code.runs))
    run_record = next(iter(fake_rayd.code.runs.values()))
    wait_until(lambda: len(run_record.ring) >= 2)
    fake_rayd.suspend_resume(2)
    thread.join(WAIT_BUDGET_SECONDS)
    assert not thread.is_alive()
    assert errors == []
    execution = results[0]
    assert execution.error is None
    assert execution.text == "'slow'"
    assert "".join(execution.logs.stdout) == "slow start\n"
    reattach = fake_rayd.code.reattach_requests[-1]
    assert reattach.context_id == "default"
    assert reattach.execution_id == run_record.record.execution_id
    assert reattach.from_seq in (2, 3)
    assert len(fake_rayd.code.execute_requests) == 1
    assert run_record.record.interrupted is False


def test_run_code_cut_before_started_is_not_retried(
    sandbox: Sandbox, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.code.phase = "suspending"
    with pytest.raises(SandboxStateException, match="suspending"):
        sandbox.run_code("1+1")
    assert len(fake_rayd.code.execute_requests) == 1
    assert fake_rayd.code.reattach_requests == []
    fake_rayd.code.phase = None
    assert sandbox.run_code("1+1").text == "2"


def test_reattach_not_found_is_a_sandbox_exception(
    sandbox: Sandbox, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.code.retention_seconds = 0.0

    def run() -> None:
        sandbox.run_code("slow 1", timeout=None)

    errors: list[Exception] = []

    def guarded() -> None:
        try:
            run()
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=guarded, daemon=True)
    thread.start()
    wait_until(lambda: bool(fake_rayd.code.runs))
    run_record = next(iter(fake_rayd.code.runs.values()))
    fake_rayd.suspend()
    wait_until(lambda: run_record.ended)
    fake_rayd.resume()
    thread.join(WAIT_BUDGET_SECONDS)
    assert len(errors) == 1
    assert type(errors[0]) is SandboxException
    assert "reenganchar" in str(errors[0])


def test_unary_is_retried_once_after_a_reconnect(sandbox: Sandbox, fake_rayd: RaydEndpoint) -> None:
    handle = sandbox.commands.run("sleep 30", background=True, timeout=None)
    fake_rayd.process.signal_unavailable_calls = 1
    fake_rayd.resume()
    assert sandbox.commands.kill(handle.pid) is True
    assert len(fake_rayd.process.signal_requests) == 2
    assert sandbox.resume_generation == 1


def test_terminated_during_the_poll_is_not_found(
    sandbox: Sandbox,
    fake_rayd: RaydEndpoint,
    control_plane: StubbedControlPlane,
    fast_state_checks: None,
) -> None:
    handle = sandbox.commands.run("sleep 30", background=True, timeout=None)
    fake_rayd.suspend()
    fake_rayd.servicer.unavailable_calls = 10_000
    control_plane.microvms.add_response(
        "get_microvm",
        microvm_response(endpoint=fake_rayd.host, state="TERMINATED", state_reason="Success."),
    )
    started = time.monotonic()
    with pytest.raises(SandboxNotFoundException, match="TERMINATED"):
        handle.wait()
    assert time.monotonic() - started < SHORT_RECONNECT_TIMEOUT
    with pytest.raises(SandboxNotFoundException):
        handle.wait()
    assert handle.reconnects == 0


def test_suspended_without_auto_resume_stops_a_foreground_run(
    sandbox: Sandbox,
    fake_rayd: RaydEndpoint,
    monkeypatch: pytest.MonkeyPatch,
    fast_state_checks: None,
) -> None:
    """Un `run` en foreground cortado por un `/suspend` ajeno sondea `Health`
    (con auto-resume sería la petición que despierta al VM); sin auto-resume
    nadie va a reanudarlo y falla en cuanto `get-microvm` lo dice."""
    install_state(monkeypatch, sandbox, fake_rayd.host, "SUSPENDED", auto_resume=False)
    fake_rayd.servicer.unavailable_calls = 10_000
    threading.Timer(0.2, fake_rayd.suspend).start()
    started = time.monotonic()
    with pytest.raises(SandboxStateException, match=r"resume\(\)"):
        sandbox.commands.run("sleep 30", timeout=None)
    assert time.monotonic() - started < SHORT_RECONNECT_TIMEOUT
    fake_rayd.resume()


@pytest.mark.parametrize("auto_resume", [True, False])
def test_background_handle_sleeps_through_a_suspension(
    sandbox: Sandbox,
    fake_rayd: RaydEndpoint,
    monkeypatch: pytest.MonkeyPatch,
    fast_state_checks: None,
    auto_resume: bool,
) -> None:
    """Un handle en background no despierta un sandbox suspendido (sus sondas
    de `Health` lo harían con auto-resume) ni consume `reconnect_timeout`
    mientras duerme, sea cual sea la política de idle: consulta
    `get-microvm` y sólo sondea cuando el VM vuelve a `RUNNING`."""
    sandbox._reconnect_timeout = 0.5
    microvm = install_state(
        monkeypatch, sandbox, fake_rayd.host, "SUSPENDED", auto_resume=auto_resume
    )
    handle = sandbox.commands.run("sleep 1", background=True, timeout=None)
    fake_rayd.suspend()
    health_before = health_calls(fake_rayd)
    collector = Collector(handle)
    time.sleep(3 * sandbox._reconnect_timeout)
    assert collector.thread.is_alive()
    assert collector.error is None
    assert health_calls(fake_rayd) == health_before
    assert microvm.calls >= 2
    fake_rayd.resume()
    microvm.state = "RUNNING"
    collector.join()
    assert collector.error is None
    assert handle.wait().exit_code == 0
    assert handle.reconnects == 1
    assert health_calls(fake_rayd) - health_before == 1


def test_resume_wakes_a_dormant_handle_at_once(
    sandbox: Sandbox,
    fake_rayd: RaydEndpoint,
    control_plane: StubbedControlPlane,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """La generación nueva que registra `resume()` despierta al handle dormido
    sin esperar al siguiente `get-microvm` (5 s)."""
    microvm = install_state(monkeypatch, sandbox, fake_rayd.host, "SUSPENDED")
    control_plane.microvms.add_response("resume_microvm", {}, {"microvmIdentifier": SANDBOX_ID})
    control_plane.microvms.add_response("create_microvm_auth_token", auth_token_response("jwe-2"))
    handle = sandbox.commands.run("sleep 1", background=True, timeout=None)
    fake_rayd.suspend()
    collector = Collector(handle)
    wait_until(lambda: microvm.calls >= 1)
    fake_rayd.resume()
    started = time.monotonic()
    sandbox.resume()
    collector.join(2.0)
    assert time.monotonic() - started < 2.0
    assert collector.error is None
    assert handle.reconnects == 1


def test_a_dormant_handle_does_not_block_a_foreground_call(
    sandbox: Sandbox, fake_rayd: RaydEndpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El handle duerme fuera del lock: un `run` nuevo sondea `Health` al
    instante (es la petición que despierta un VM con auto-resume) y la
    generación que registra despierta también al handle."""
    microvm = install_state(monkeypatch, sandbox, fake_rayd.host, "SUSPENDED")
    handle = sandbox.commands.run("sleep 1", background=True, timeout=None)
    fake_rayd.suspend()
    collector = Collector(handle)
    wait_until(lambda: microvm.calls >= 1)
    health_before = health_calls(fake_rayd)
    threading.Timer(0.3, fake_rayd.resume).start()
    started = time.monotonic()
    assert sandbox.commands.run("echo hola").stdout.strip() == "hola"
    assert time.monotonic() - started < ReconnectPoll.STATE_CHECK_INTERVAL
    assert health_calls(fake_rayd) > health_before
    collector.join(2.0)
    assert collector.error is None
    assert handle.reconnects == 1


def test_an_explicit_pause_keeps_a_foreground_run_dormant_until_resume(
    sandbox: Sandbox,
    fake_rayd: RaydEndpoint,
    control_plane: StubbedControlPlane,
    monkeypatch: pytest.MonkeyPatch,
    fast_state_checks: None,
) -> None:
    """Con un `pause()` de este `Sandbox` pendiente ni un `run` en foreground
    sondea `Health` (con auto-resume desharía la pausa): espera al `resume()`
    y termina con `Connect`."""
    microvm = install_state(monkeypatch, sandbox, fake_rayd.host, "RUNNING")
    stub_pause_and_resume(control_plane)
    results: list[Any] = []
    errors: list[Exception] = []

    def run() -> None:
        try:
            results.append(sandbox.commands.run("sleep 1", timeout=None))
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    wait_until(lambda: bool(fake_rayd.process.processes))
    assert sandbox.pause(wait=False) is True
    assert sandbox._paused is True
    microvm.state = "SUSPENDED"
    calls_at_pause = microvm.calls
    fake_rayd.suspend()
    health_before = health_calls(fake_rayd)
    wait_until(lambda: microvm.calls >= calls_at_pause + 2)
    assert thread.is_alive()
    assert health_calls(fake_rayd) == health_before
    fake_rayd.resume()
    sandbox.resume()
    thread.join(WAIT_BUDGET_SECONDS)
    assert not thread.is_alive()
    assert errors == []
    assert results[0].exit_code == 0
    assert sandbox._paused is False


def test_an_explicit_pause_keeps_run_code_dormant_until_resume(
    sandbox: Sandbox,
    fake_rayd: RaydEndpoint,
    control_plane: StubbedControlPlane,
    monkeypatch: pytest.MonkeyPatch,
    fast_state_checks: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    microvm = install_state(monkeypatch, sandbox, fake_rayd.host, "RUNNING")
    stub_pause_and_resume(control_plane)
    results: list[Execution] = []
    errors: list[Exception] = []

    def run() -> None:
        try:
            results.append(sandbox.run_code("slow 2", timeout=None))
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    wait_until(lambda: bool(fake_rayd.code.runs))
    run_record = next(iter(fake_rayd.code.runs.values()))
    wait_until(lambda: len(run_record.ring) >= 2)
    assert sandbox.pause(wait=False) is True
    microvm.state = "SUSPENDED"
    calls_at_pause = microvm.calls
    fake_rayd.suspend()
    health_before = health_calls(fake_rayd)
    wait_until(lambda: microvm.calls >= calls_at_pause + 2)
    assert thread.is_alive()
    assert health_calls(fake_rayd) == health_before
    fake_rayd.resume()
    with caplog.at_level(logging.INFO, logger="rayito.code"):
        sandbox.resume()
        thread.join(WAIT_BUDGET_SECONDS)
    assert not thread.is_alive()
    assert errors == []
    assert results[0].text == "'slow'"
    assert results[0].error is None
    assert fake_rayd.code.reattach_requests[-1].execution_id == run_record.record.execution_id
    assert any("Reattach" in record.message for record in caplog.records)


def test_a_pause_that_did_not_suspend_leaves_no_pending_pause(
    sandbox: Sandbox, control_plane: StubbedControlPlane
) -> None:
    control_plane.microvms.add_response("get_microvm", microvm_response(state="RUNNING"))
    control_plane.microvms.add_client_error(
        "suspend_microvm", service_error_code="ConflictException", http_status_code=409
    )
    assert sandbox.pause() is False
    assert sandbox._paused is False


def test_reconnect_deadline_is_a_sandbox_exception(
    sandbox: Sandbox, fake_rayd: RaydEndpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    reset = FakeRpcError(grpc.StatusCode.UNAVAILABLE, details="Socket closed")
    monkeypatch.setattr(
        sandbox._process, "Start", lambda request, timeout=None: resetting_stream(reset)
    )
    fake_rayd.servicer.unavailable_calls = 10_000
    health_before = health_calls(fake_rayd)
    started = time.monotonic()
    with pytest.raises(SandboxException, match="no volvió a responder") as excinfo:
        sandbox.commands.run("sleep 30", timeout=None)
    elapsed = time.monotonic() - started
    assert type(excinfo.value) is SandboxException
    assert excinfo.value.__cause__ is reset
    assert SHORT_RECONNECT_TIMEOUT <= elapsed < SHORT_RECONNECT_TIMEOUT + 3
    assert health_calls(fake_rayd) - health_before >= 4


def test_suspending_reason_times_out_as_a_state_exception(
    sandbox: Sandbox, fake_rayd: RaydEndpoint
) -> None:
    sandbox._reconnect_timeout = 0.5
    threading.Timer(0.2, fake_rayd.suspend).start()
    with pytest.raises(SandboxStateException, match="suspending"):
        sandbox.commands.run("sleep 30", timeout=None)
    fake_rayd.resume()


def test_two_handles_share_one_reconnect_poll(
    sandbox: Sandbox, fake_rayd: RaydEndpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    always_running(monkeypatch, sandbox, fake_rayd.host)
    first = sandbox.commands.run("seq 40", background=True, timeout=None)
    second = sandbox.commands.connect(first.pid, from_seq=1)
    collectors = [Collector(first), Collector(second)]
    wait_until(lambda: all(len(collector.chunks) >= 2 for collector in collectors))
    health_before = health_calls(fake_rayd)
    fake_rayd.suspend_resume(3)
    for collector in collectors:
        collector.join()
        assert collector.error is None
        assert "".join(collector.chunks) == "".join(f"{n}\n" for n in range(1, 41))
    assert first.reconnects == 1
    assert second.reconnects == 1
    assert health_calls(fake_rayd) - health_before == 4


def test_disconnected_handles_never_reconnect(sandbox: Sandbox, fake_rayd: RaydEndpoint) -> None:
    handle = sandbox.commands.run("sleep 30", background=True, timeout=None)
    handle.disconnect()
    fake_rayd.suspend_resume(1)
    with pytest.raises(SandboxException, match="desconectado"):
        handle.wait()
    assert fake_rayd.process.connect_requests == []
    assert handle.reconnects == 0
    assert sandbox.commands.kill(handle.pid) is True


def test_close_during_a_reconnect_fails_the_handle(
    sandbox: Sandbox, fake_rayd: RaydEndpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    always_running(monkeypatch, sandbox, fake_rayd.host)
    handle = sandbox.commands.run("sleep 30", background=True, timeout=None)
    fake_rayd.suspend()
    fake_rayd.servicer.unavailable_calls = 10_000
    collector = Collector(handle)
    wait_until(lambda: health_calls(fake_rayd) >= 2)
    sandbox.close()
    collector.join()
    assert isinstance(collector.error, SandboxException)
    assert "cerrado" in str(collector.error)


def test_resume_remints_records_the_generation_and_warns_on_clock_offset(
    sandbox: Sandbox,
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
    control_plane.microvms.add_response(
        "create_microvm_auth_token",
        auth_token_response("jwe-2"),
        expected_params={
            "microvmIdentifier": SANDBOX_ID,
            "expirationInMinutes": 60,
            "allowedPorts": [{"port": DEFAULT_PORT}],
        },
    )
    assert sandbox.pause() is True
    assert sandbox.info.state == "SUSPENDED"
    assert sandbox.pause() is False
    refresher_thread = sandbox._refresher._thread
    assert refresher_thread is not None and refresher_thread.is_alive()
    fake_rayd.suspend_resume(0, clock_offset_ms=6000, kernel_state_lost=True)
    with caplog.at_level(logging.WARNING, logger="rayito.sandbox"):
        sandbox.resume()
    assert sandbox.resume_generation == 1
    assert fake_rayd.servicer.health_calls[-1][PROXY_AUTH_KEY] == "jwe-2"
    messages = [record.message for record in caplog.records]
    assert any("6000 ms" in message for message in messages)
    assert any("perdió su estado" in message for message in messages)
    health = sandbox.get_health()
    assert health.resume_generation == 1
    assert health.clock_offset_ms == 6000
    assert health.kernel_state_lost is True
    assert health.agent_ready is True
    assert health.kernel_ready is True
    assert health.sandbox_id == SANDBOX_ID
    assert health.uptime_ms == fake_rayd.servicer.uptime_ms
    assert health.agent_version == "test"
    control_plane.microvms.assert_no_pending_responses()


def test_resume_tolerates_a_conflict_when_already_running(
    sandbox: Sandbox, fake_rayd: RaydEndpoint, control_plane: StubbedControlPlane
) -> None:
    control_plane.microvms.add_client_error(
        "resume_microvm", service_error_code="ConflictException", http_status_code=409
    )
    control_plane.microvms.add_response("create_microvm_auth_token", auth_token_response("jwe-3"))
    sandbox.resume()
    assert fake_rayd.servicer.health_calls[-1][PROXY_AUTH_KEY] == "jwe-3"


def test_handles_created_after_a_resume_carry_the_new_generation(
    sandbox: Sandbox, fake_rayd: RaydEndpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    always_running(monkeypatch, sandbox, fake_rayd.host)
    fake_rayd.suspend_resume(0)
    assert sandbox.get_health().resume_generation == 1
    handle = sandbox.commands.run("sleep 30", background=True, timeout=None)
    assert handle._generation == 1
    fake_rayd.suspend_resume(1)
    fake_rayd.process.processes[handle.pid].signal.set()
    with pytest.raises(CommandExitException) as excinfo:
        handle.wait()
    assert excinfo.value.exit_code == 143
    assert handle.reconnects == 1
