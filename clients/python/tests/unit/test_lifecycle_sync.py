"""Plazo lógico (ADR-011) en el `Sandbox` síncrono contra el `rayd` falso:
`set_timeout` (instancia y clase), `connect(timeout=)`, la reapertura tras
el plazo, la puerta de agente M9 en `create()`, los cierres
`sandbox_timeout`, el disparador del modo `pause` y `get_info().expires_at`."""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import grpc
import pytest

from rayito import IdlePolicy, Sandbox, SandboxPool
from rayito._aws import sandbox_info_from_response
from rayito._persistence_base import launch_kwargs
from rayito._transport import (
    ACCESS_TOKEN_KEY,
    is_reconnectable,
    translate_rpc_error,
    translate_stream_error,
)
from rayito.exceptions import (
    AuthenticationException,
    InvalidArgumentException,
    LifecycleUnsupportedException,
    SandboxNotFoundException,
    SandboxNotReadyException,
    SandboxStateException,
    TimeoutException,
)
from rayito.v1 import lifecycle_pb2

from .conftest import (
    ACCESS_TOKEN,
    IMAGE_ARN,
    SANDBOX_ID,
    STARTED_AT,
    FakeRpcError,
    RaydEndpoint,
    StubbedControlPlane,
    TrackingTransport,
    auth_token_response,
    microvm_response,
    stub_metadata_probe,
)
from .fake_lifecycle import lifecycle_state
from .fake_process import sandbox_timed_out
from .fake_pty import pty_sandbox_timed_out

DEADLINE_TOLERANCE = timedelta(seconds=5)
TRIGGER_SLACK = 1.0


def expect_deadline_in(deadline: datetime | None, seconds: float) -> None:
    assert deadline is not None
    expected = datetime.now(UTC) + timedelta(seconds=seconds)
    assert abs(deadline - expected) < DEADLINE_TOLERANCE


def capture_launch(control_plane: StubbedControlPlane, endpoint: RaydEndpoint) -> dict[str, Any]:
    """Encola `run-microvm` + el primer JWE y captura la petición."""
    captured: dict[str, Any] = {}
    control_plane.microvms.add_response("run_microvm", microvm_response(endpoint=endpoint.host))
    control_plane.microvms.add_response("create_microvm_auth_token", auth_token_response())
    original = control_plane.plane.run_microvm

    def spy(request: Any) -> Any:
        captured.update(request.to_api())
        return original(request)

    control_plane.plane.run_microvm = spy  # type: ignore[method-assign]
    return captured


def launch(control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint, **kwargs: Any) -> Sandbox:
    options: dict[str, Any] = {"idle": None, "max_lifetime": 900, **kwargs}
    return Sandbox.create(
        IMAGE_ARN,
        access_token=ACCESS_TOKEN,
        control_plane=control_plane.plane,
        transport=fake_rayd.transport,
        **options,
    )


def stub_running(control_plane: StubbedControlPlane, endpoint: RaydEndpoint) -> None:
    control_plane.microvms.add_response(
        "get_microvm",
        microvm_response(endpoint=endpoint.host, state="RUNNING", maximum_duration=900),
    )


def expect_terminate(control_plane: StubbedControlPlane) -> None:
    control_plane.microvms.add_response(
        "terminate_microvm", {}, expected_params={"microvmIdentifier": SANDBOX_ID}
    )


def no_pause_trigger(monkeypatch: pytest.MonkeyPatch) -> None:
    """Un lifecycle `pause` vencido armaría un disparo inmediato en otro hilo
    que consumiría respuestas del Stubber; estos tests no lo miran."""
    monkeypatch.setattr("rayito.sandbox_sync.main.pause_trigger_delay", lambda *_: None)


@pytest.fixture
def managed(control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint) -> Iterator[Sandbox]:
    """Un sandbox creado con `max_lifetime=900` (modo `kill`) cuyo agente M9
    reporta un plazo 60 s por delante y el tope 840 s por delante."""
    fake_rayd.servicer.lifecycle = lifecycle_state()
    capture_launch(control_plane, fake_rayd)
    sandbox = launch(control_plane, fake_rayd, timeout=60)
    try:
        yield sandbox
    finally:
        expect_terminate(control_plane)
        sandbox.kill()


def test_set_timeout_sends_exact(managed: Sandbox, fake_rayd: RaydEndpoint) -> None:
    managed.set_timeout(150)
    request = fake_rayd.lifecycle.requests[-1]
    assert (request.mode, request.timeout_ms) == (lifecycle_pb2.TIMEOUT_MODE_EXACT, 150_000)
    assert ACCESS_TOKEN_KEY in fake_rayd.lifecycle.request_metadata[-1]
    expect_deadline_in(managed.get_health().lifecycle.deadline, 150)  # type: ignore[union-attr]

    managed.set_timeout(10)
    assert fake_rayd.lifecycle.requests[-1].timeout_ms == 10_000
    lifecycle = managed.get_health().lifecycle
    assert lifecycle is not None
    expect_deadline_in(lifecycle.deadline, 10)
    assert (lifecycle.timeout_seconds, lifecycle.extensions) == (10.0, 2)


def test_set_timeout_beyond_cap_raises_and_keeps_the_deadline(
    managed: Sandbox, fake_rayd: RaydEndpoint
) -> None:
    before = managed.get_health().lifecycle
    with pytest.raises(
        InvalidArgumentException, match=r"set_timeout\(2000\) supera el max_lifetime.*28800"
    ):
        managed.set_timeout(2000)
    assert managed.get_health().lifecycle == before
    with pytest.raises(InvalidArgumentException, match=r"set_timeout\(28801\).*28800"):
        managed.set_timeout(28_801)
    with pytest.raises(InvalidArgumentException, match=">= 1"):
        managed.set_timeout(0)
    assert len(fake_rayd.lifecycle.requests) == 1


def test_class_set_timeout_uses_the_access_token_and_refuses_suspended(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_rayd.servicer.lifecycle = lifecycle_state()
    transport = TrackingTransport.for_loopback()
    stub_metadata_probe(control_plane, SANDBOX_ID, fake_rayd)
    Sandbox.set_timeout(
        SANDBOX_ID,
        90,
        access_token=ACCESS_TOKEN,
        control_plane=control_plane.plane,
        transport=transport,
    )
    request = fake_rayd.lifecycle.requests[-1]
    assert (request.mode, request.timeout_ms) == (lifecycle_pb2.TIMEOUT_MODE_EXACT, 90_000)
    assert ACCESS_TOKEN_KEY in fake_rayd.lifecycle.request_metadata[-1]
    assert (transport.open_count, transport.all_closed) == (1, True)

    for state in ("SUSPENDING", "SUSPENDED"):
        stub_metadata_probe(control_plane, SANDBOX_ID, fake_rayd, state=state)
        with pytest.raises(SandboxStateException, match=r"connect\(\) lo reanuda"):
            Sandbox.set_timeout(
                SANDBOX_ID, 90, access_token=ACCESS_TOKEN, control_plane=control_plane.plane
            )
    stub_metadata_probe(control_plane, SANDBOX_ID, fake_rayd, state="TERMINATED")
    with pytest.raises(SandboxNotFoundException):
        Sandbox.set_timeout(
            SANDBOX_ID, 90, access_token=ACCESS_TOKEN, control_plane=control_plane.plane
        )
    monkeypatch.delenv("RAYITO_ACCESS_TOKEN", raising=False)
    with pytest.raises(AuthenticationException, match=r"^set_timeout\(sandbox_id\) necesita"):
        Sandbox.set_timeout(SANDBOX_ID, 90, control_plane=control_plane.plane)
    assert len(fake_rayd.lifecycle.requests) == 1
    assert transport.open_count == 1


def test_connect_timeout_sends_at_least(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.servicer.lifecycle = lifecycle_state(deadline_in_ms=600_000)
    stub_running(control_plane, fake_rayd)
    control_plane.microvms.add_response("create_microvm_auth_token", auth_token_response())
    sandbox = Sandbox.connect(
        SANDBOX_ID,
        timeout=300,
        access_token=ACCESS_TOKEN,
        control_plane=control_plane.plane,
        transport=fake_rayd.transport,
    )
    try:
        request = fake_rayd.lifecycle.requests[-1]
        assert (request.mode, request.timeout_ms) == (lifecycle_pb2.TIMEOUT_MODE_AT_LEAST, 300_000)
        expect_deadline_in(sandbox.get_health().lifecycle.deadline, 600)  # type: ignore[union-attr]

        stub_running(control_plane, fake_rayd)
        assert sandbox.connect(timeout=800) is sandbox
        request = fake_rayd.lifecycle.requests[-1]
        assert (request.mode, request.timeout_ms) == (lifecycle_pb2.TIMEOUT_MODE_AT_LEAST, 800_000)
        expect_deadline_in(sandbox.get_health().lifecycle.deadline, 800)  # type: ignore[union-attr]

        stub_running(control_plane, fake_rayd)
        sandbox.connect()
        assert len(fake_rayd.lifecycle.requests) == 2

        stub_running(control_plane, fake_rayd)
        with pytest.raises(InvalidArgumentException, match=r"connect\(timeout=900\).*max_lifetime"):
            sandbox.connect(timeout=900)
    finally:
        sandbox.close()


def test_connect_reopens_an_expired_sandbox_with_its_own_timeout(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    no_pause_trigger(monkeypatch)
    fake_rayd.servicer.lifecycle = lifecycle_state(
        phase=lifecycle_pb2.LIFECYCLE_PHASE_EXPIRED,
        deadline_in_ms=-5_000,
        timeout_ms=60_000,
        on_timeout=lifecycle_pb2.TIMEOUT_ACTION_PAUSE,
    )
    stub_running(control_plane, fake_rayd)
    control_plane.microvms.add_response("create_microvm_auth_token", auth_token_response())
    sandbox = Sandbox.connect(
        SANDBOX_ID,
        access_token=ACCESS_TOKEN,
        control_plane=control_plane.plane,
        transport=fake_rayd.transport,
    )
    try:
        request = fake_rayd.lifecycle.requests[-1]
        assert (request.mode, request.timeout_ms) == (lifecycle_pb2.TIMEOUT_MODE_AT_LEAST, 60_000)
        lifecycle = sandbox.get_health().lifecycle
        assert lifecycle is not None
        assert lifecycle.phase == "active"
        expect_deadline_in(lifecycle.deadline, 60)
    finally:
        sandbox.close()


def test_resume_reopens_a_grace_sandbox(
    managed: Sandbox, control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.servicer.lifecycle = lifecycle_state(
        phase=lifecycle_pb2.LIFECYCLE_PHASE_RESUME_GRACE, deadline_in_ms=-10_000, timeout_ms=120_000
    )
    control_plane.microvms.add_response(
        "resume_microvm", {}, expected_params={"microvmIdentifier": SANDBOX_ID}
    )
    control_plane.microvms.add_response("create_microvm_auth_token", auth_token_response())
    managed.resume()
    request = fake_rayd.lifecycle.requests[-1]
    assert (request.mode, request.timeout_ms) == (lifecycle_pb2.TIMEOUT_MODE_AT_LEAST, 120_000)
    lifecycle = managed.get_health().lifecycle
    assert lifecycle is not None
    assert lifecycle.phase == "active"


def test_create_on_an_older_agent_terminates_and_raises(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.servicer.agent_version = "0.2.0"
    captured = capture_launch(control_plane, fake_rayd)
    expect_terminate(control_plane)
    with pytest.raises(LifecycleUnsupportedException, match="publica una imagen M9") as raised:
        launch(control_plane, fake_rayd, timeout=60, on_timeout="kill")
    assert not isinstance(raised.value, SandboxNotReadyException)
    assert "0.2.0" in str(raised.value)
    assert "lifecycle" in json.loads(str(captured["runHookPayload"]))

    capture_launch(control_plane, fake_rayd)
    with pytest.raises(LifecycleUnsupportedException):
        launch(control_plane, fake_rayd, timeout=60, keep_on_failure=True)


def test_create_without_lifecycle_kwargs_still_works_on_an_older_agent(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    captured = capture_launch(control_plane, fake_rayd)
    expect_terminate(control_plane)
    with Sandbox.create(
        IMAGE_ARN,
        timeout=600,
        idle=None,
        access_token=ACCESS_TOKEN,
        control_plane=control_plane.plane,
        transport=fake_rayd.transport,
    ) as sandbox:
        assert sandbox.get_health().lifecycle is None
    assert captured["maximumDurationInSeconds"] == 600
    assert "lifecycle" not in json.loads(str(captured["runHookPayload"]))


def test_create_records_the_lifecycle_kwargs_for_reincarnate(
    managed: Sandbox, control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    options = managed._launch_options
    assert options is not None
    assert (options.timeout, options.max_lifetime, options.on_timeout) == (60, 900, None)
    relaunch = launch_kwargs(options)
    assert (relaunch["max_lifetime"], relaunch["on_timeout"]) == (900, None)

    captured = capture_launch(control_plane, fake_rayd)
    shim_like = Sandbox.create(
        IMAGE_ARN,
        timeout=300,
        idle=None,
        on_timeout="kill",
        access_token=ACCESS_TOKEN,
        control_plane=control_plane.plane,
        transport=fake_rayd.transport,
        max_lifetime=3600,
    )
    try:
        assert captured["maximumDurationInSeconds"] == 3600
        recorded = shim_like._launch_options
        assert recorded is not None
        assert (recorded.max_lifetime, recorded.on_timeout) == (3600, "kill")
    finally:
        shim_like.close()


def test_sandbox_timeout_stream_end_raises_timeout_exception(
    managed: Sandbox, fake_rayd: RaydEndpoint
) -> None:
    handle = managed.commands.run("sleep 30", background=True, timeout=None)
    pty = managed.pty.create(timeout=None)
    fake_rayd.process.processes[handle.pid].close_streams(sandbox_timed_out())
    with pytest.raises(TimeoutException, match="sandbox_timeout"):
        handle.wait()
    fake_rayd.pty.ptys[pty.pid].close_streams(pty_sandbox_timed_out())
    with pytest.raises(TimeoutException, match="sandbox_timeout"):
        pty.wait()
    assert (handle.reconnects, pty.reconnects) == (0, 0)


def test_failed_precondition_sandbox_timeout_is_timeout_exception(
    managed: Sandbox, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.lifecycle.abort_with = (grpc.StatusCode.FAILED_PRECONDITION, "sandbox_timeout")
    with pytest.raises(TimeoutException, match="sandbox_timeout"):
        managed.set_timeout(30)

    gate = FakeRpcError(grpc.StatusCode.FAILED_PRECONDITION, details="sandbox_timeout")
    assert isinstance(translate_rpc_error(gate), TimeoutException)
    assert is_reconnectable(gate) is False
    assert isinstance(
        translate_stream_error("sandbox_timeout", "sandbox timeout"), TimeoutException
    )
    other = FakeRpcError(grpc.StatusCode.FAILED_PRECONDITION, details="stdin is not open")
    assert type(translate_rpc_error(other)) is InvalidArgumentException


def test_unmanaged_set_timeout_raises_invalid_argument(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.servicer.lifecycle = lifecycle_state(phase=lifecycle_pb2.LIFECYCLE_PHASE_UNMANAGED)
    capture_launch(control_plane, fake_rayd)
    sandbox = Sandbox.create(
        IMAGE_ARN,
        idle=None,
        access_token=ACCESS_TOKEN,
        control_plane=control_plane.plane,
        transport=fake_rayd.transport,
    )
    try:
        with pytest.raises(InvalidArgumentException, match="max_lifetime") as raised:
            sandbox.set_timeout(60)
        assert not isinstance(raised.value, LifecycleUnsupportedException)
        stub_running(control_plane, fake_rayd)
        with pytest.raises(InvalidArgumentException, match="max_lifetime"):
            sandbox.connect(timeout=60)
        fake_rayd.servicer.lifecycle = None
        with pytest.raises(LifecycleUnsupportedException, match="imagen M9"):
            sandbox.set_timeout(60)
    finally:
        expect_terminate(control_plane)
        sandbox.kill()


def test_pause_trigger_suspends_only_when_expired_and_running(
    control_plane: StubbedControlPlane,
    fake_rayd: RaydEndpoint,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake_rayd.servicer.lifecycle = lifecycle_state(on_timeout=lifecycle_pb2.TIMEOUT_ACTION_PAUSE)
    captured = capture_launch(control_plane, fake_rayd)
    sandbox = launch(
        control_plane,
        fake_rayd,
        timeout=60,
        on_timeout="pause",
        idle=IdlePolicy(max_idle_seconds=300, auto_resume=True),
    )
    try:
        assert json.loads(str(captured["runHookPayload"]))["lifecycle"]["on_timeout"] == "pause"
        trigger = sandbox._deadline_trigger
        assert trigger.scheduled_delay == pytest.approx(60 + TRIGGER_SLACK, abs=3)
        state = {"value": "SUSPENDED"}
        suspended: list[str] = []

        def get_microvm(sandbox_id: str) -> Any:
            return sandbox_info_from_response(
                microvm_response(endpoint=fake_rayd.host, state=state["value"])
            )

        def suspend_microvm(sandbox_id: str) -> bool:
            suspended.append(sandbox_id)
            return True

        monkeypatch.setattr(sandbox._control_plane, "get_microvm", get_microvm)
        monkeypatch.setattr(sandbox._control_plane, "suspend_microvm", suspend_microvm)

        health_calls = len(fake_rayd.servicer.health_calls)
        trigger.fire()
        assert len(fake_rayd.servicer.health_calls) == health_calls
        assert suspended == []

        state["value"] = "RUNNING"
        fake_rayd.servicer.lifecycle = lifecycle_state(
            on_timeout=lifecycle_pb2.TIMEOUT_ACTION_PAUSE, deadline_in_ms=120_000
        )
        trigger.fire()
        assert suspended == []
        assert trigger.scheduled_delay == pytest.approx(120 + TRIGGER_SLACK, abs=3)

        fake_rayd.servicer.lifecycle = lifecycle_state(
            phase=lifecycle_pb2.LIFECYCLE_PHASE_EXPIRED,
            on_timeout=lifecycle_pb2.TIMEOUT_ACTION_PAUSE,
            deadline_in_ms=-1_000,
        )
        trigger.fire()
        assert suspended == [SANDBOX_ID]
        assert trigger.scheduled_delay is None
        assert sandbox._paused is False

        def broken(sandbox_id: str) -> Any:
            raise RuntimeError("get-microvm caído")

        monkeypatch.setattr(sandbox._control_plane, "get_microvm", broken)
        with caplog.at_level(logging.WARNING, logger="rayito.sandbox"):
            trigger.fire()
        assert any("disparador del plazo" in record.getMessage() for record in caplog.records)
        assert ACCESS_TOKEN not in caplog.text
    finally:
        monkeypatch.undo()
        expect_terminate(control_plane)
        sandbox.kill()
    assert sandbox._deadline_trigger.scheduled_delay is None


def expired_pause_state() -> lifecycle_pb2.LifecycleState:
    return lifecycle_state(
        phase=lifecycle_pb2.LIFECYCLE_PHASE_EXPIRED,
        on_timeout=lifecycle_pb2.TIMEOUT_ACTION_PAUSE,
        auto_resume=True,
        deadline_in_ms=-2_000,
    )


def test_a_brief_deadline_pause_reopens_with_the_auto_resume_rule(
    control_plane: StubbedControlPlane,
    fake_rayd: RaydEndpoint,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    no_pause_trigger(monkeypatch)
    fake_rayd.servicer.lifecycle = lifecycle_state(
        on_timeout=lifecycle_pb2.TIMEOUT_ACTION_PAUSE, auto_resume=True
    )
    capture_launch(control_plane, fake_rayd)
    sandbox = launch(
        control_plane,
        fake_rayd,
        timeout=60,
        on_timeout="pause",
        idle=IdlePolicy(max_idle_seconds=300, auto_resume=True),
    )
    try:
        monkeypatch.setattr(sandbox._control_plane, "suspend_microvm", lambda _: True)
        fake_rayd.servicer.timeout_gate = True
        fake_rayd.servicer.lifecycle = expired_pause_state()
        with pytest.raises(TimeoutException, match="sandbox_timeout"):
            sandbox.files.make_dir("/home/user/never")
        sandbox._suspend_for_deadline()
        with pytest.raises(TimeoutException, match="sandbox_timeout"):
            sandbox.files.make_dir("/home/user/not-resumed")
        assert fake_rayd.lifecycle.requests == []

        sandbox._suspend_for_deadline()
        fake_rayd.resume()
        assert sandbox.files.make_dir("/home/user/unary") is True
        assert [(r.mode, r.timeout_ms) for r in fake_rayd.lifecycle.requests] == [
            (lifecycle_pb2.TIMEOUT_MODE_EXACT, 300_000)
        ]
        assert sandbox._lifecycle is not None and sandbox._lifecycle.phase == "active"
        expect_deadline_in(sandbox._lifecycle.deadline, 300)

        fake_rayd.servicer.lifecycle = expired_pause_state()
        sandbox._suspend_for_deadline()
        fake_rayd.resume()
        assert sandbox.commands.run("echo hola").stdout == "hola\n"
        assert len(fake_rayd.lifecycle.requests) == 2

        fake_rayd.servicer.lifecycle = expired_pause_state()
        fake_rayd.resume()
        with pytest.raises(TimeoutException, match="sandbox_timeout"):
            sandbox.files.make_dir("/home/user/stray")
        assert len(fake_rayd.lifecycle.requests) == 2
    finally:
        monkeypatch.undo()
        expect_terminate(control_plane)
        sandbox.kill()


def launch_paused_by_deadline(
    control_plane: StubbedControlPlane,
    fake_rayd: RaydEndpoint,
    monkeypatch: pytest.MonkeyPatch,
) -> Sandbox:
    """Un sandbox `pause` con `auto_resume` que este cliente ya suspendió por
    su plazo, con `rayd` todavía `expired` tras el resume."""
    no_pause_trigger(monkeypatch)
    fake_rayd.servicer.lifecycle = lifecycle_state(
        on_timeout=lifecycle_pb2.TIMEOUT_ACTION_PAUSE, auto_resume=True
    )
    capture_launch(control_plane, fake_rayd)
    sandbox = launch(
        control_plane,
        fake_rayd,
        timeout=60,
        on_timeout="pause",
        idle=IdlePolicy(max_idle_seconds=300, auto_resume=True),
    )
    monkeypatch.setattr(sandbox._control_plane, "suspend_microvm", lambda _: True)
    fake_rayd.servicer.timeout_gate = True
    fake_rayd.servicer.lifecycle = expired_pause_state()
    sandbox._suspend_for_deadline()
    return sandbox


def test_a_sandbox_timeout_before_the_freeze_does_not_burn_the_reopen(
    control_plane: StubbedControlPlane,
    fake_rayd: RaydEndpoint,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = launch_paused_by_deadline(control_plane, fake_rayd, monkeypatch)
    try:
        with pytest.raises(TimeoutException, match="sandbox_timeout"):
            sandbox.files.make_dir("/home/user/before-freeze")
        assert fake_rayd.lifecycle.requests == []
        fake_rayd.resume()
        assert sandbox.files.make_dir("/home/user/after-resume") is True
        assert [(r.mode, r.timeout_ms) for r in fake_rayd.lifecycle.requests] == [
            (lifecycle_pb2.TIMEOUT_MODE_EXACT, 300_000)
        ]
    finally:
        monkeypatch.undo()
        expect_terminate(control_plane)
        sandbox.kill()


def test_concurrent_callers_share_one_reopen(
    control_plane: StubbedControlPlane,
    fake_rayd: RaydEndpoint,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = launch_paused_by_deadline(control_plane, fake_rayd, monkeypatch)
    entered: list[int] = []
    both_timed_out = threading.Event()
    reopened = sandbox._reopened_after_deadline_pause
    refresh = sandbox._refresh_health

    def counting(*args: Any) -> bool:
        entered.append(1)
        if len(entered) == 2:
            both_timed_out.set()
        return reopened(*args)

    def held_refresh() -> None:
        both_timed_out.wait(timeout=5)
        refresh()

    monkeypatch.setattr(sandbox, "_reopened_after_deadline_pause", counting)
    monkeypatch.setattr(sandbox, "_refresh_health", held_refresh)
    try:
        fake_rayd.resume()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(sandbox.files.make_dir, ["/home/user/first", "/home/user/second"])
            )
        assert results == [True, True]
        assert both_timed_out.is_set()
        assert len(fake_rayd.lifecycle.requests) == 1
    finally:
        monkeypatch.undo()
        expect_terminate(control_plane)
        sandbox.kill()


@pytest.mark.parametrize("ending", ["close", "kill", "exit"])
def test_ending_the_handle_cancels_the_pause_trigger(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint, ending: str
) -> None:
    fake_rayd.servicer.lifecycle = lifecycle_state(on_timeout=lifecycle_pb2.TIMEOUT_ACTION_PAUSE)
    capture_launch(control_plane, fake_rayd)
    sandbox = launch(
        control_plane,
        fake_rayd,
        timeout=60,
        on_timeout="pause",
        idle=IdlePolicy(max_idle_seconds=300, auto_resume=True),
    )
    trigger = sandbox._deadline_trigger
    assert trigger.scheduled_delay == pytest.approx(60 + TRIGGER_SLACK, abs=3)
    timer = trigger._timer
    assert timer is not None and timer.is_alive()
    if ending == "close":
        sandbox.close()
    else:
        expect_terminate(control_plane)
        if ending == "kill":
            sandbox.kill()
        else:
            sandbox.__exit__(None, None, None)
    assert trigger.scheduled_delay is None
    timer.join(timeout=1)
    assert not timer.is_alive()
    trigger.arm(30)
    assert trigger.scheduled_delay is None
    control_plane.microvms.assert_no_pending_responses()


def test_kill_mode_never_arms_the_pause_trigger(managed: Sandbox) -> None:
    assert managed._deadline_trigger.scheduled_delay is None
    managed.set_timeout(30)
    assert managed._deadline_trigger.scheduled_delay is None


def test_get_info_expires_at_is_the_logical_deadline(
    managed: Sandbox, control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    deadline_ms = int(fake_rayd.servicer.lifecycle.deadline_unix_ms)
    stub_running(control_plane, fake_rayd)
    info = managed.get_info()
    assert info.expires_at == datetime.fromtimestamp(deadline_ms / 1000, tz=UTC)
    assert info.platform_expires_at == STARTED_AT + timedelta(seconds=900)
    assert info.lifecycle is not None and info.lifecycle.phase == "active"

    fake_rayd.servicer.lifecycle = lifecycle_state(deadline_in_ms=300_000)
    moved_ms = int(fake_rayd.servicer.lifecycle.deadline_unix_ms)
    stub_running(control_plane, fake_rayd)
    moved = managed.get_info()
    assert moved.expires_at == datetime.fromtimestamp(moved_ms / 1000, tz=UTC)

    health_calls = len(fake_rayd.servicer.health_calls)
    control_plane.microvms.add_response(
        "get_microvm", microvm_response(endpoint=fake_rayd.host, state="SUSPENDED")
    )
    asleep = managed.get_info()
    assert len(fake_rayd.servicer.health_calls) == health_calls
    assert asleep.expires_at == moved.expires_at


def test_class_get_info_reads_the_logical_deadline(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.servicer.lifecycle = lifecycle_state(deadline_in_ms=120_000)
    deadline_ms = int(fake_rayd.servicer.lifecycle.deadline_unix_ms)
    stub_metadata_probe(control_plane, SANDBOX_ID, fake_rayd)
    info = Sandbox.get_info(
        SANDBOX_ID, control_plane=control_plane.plane, transport=TrackingTransport.for_loopback()
    )
    assert info.expires_at == datetime.fromtimestamp(deadline_ms / 1000, tz=UTC)
    assert info.lifecycle is not None and info.lifecycle.on_timeout == "kill"


@pytest.mark.parametrize(("name", "value"), [("max_lifetime", 900), ("on_timeout", "kill")])
def test_pool_rejects_lifecycle_kwargs(name: str, value: object) -> None:
    kwargs: Any = {name: value}
    with pytest.raises(InvalidArgumentException, match=f"`{name}`"):
        Sandbox.create(pool=cast("SandboxPool", object()), **kwargs)
