"""Plazo lógico (ADR-011) en `AsyncSandbox`: la misma superficie y los mismos
casos que `test_lifecycle_sync.py` sobre `grpc.aio`, más la paridad de
firmas de `set_timeout` y `connect` con el `Sandbox` síncrono."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import grpc
import pytest

from rayito import AsyncSandbox, AsyncSandboxPool, IdlePolicy, Sandbox
from rayito._aws import sandbox_info_from_response
from rayito._persistence_base import launch_kwargs
from rayito._sandbox_base import ClassMethodVariant
from rayito._transport import ACCESS_TOKEN_KEY
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
LIFECYCLE_VARIANTS = ("set_timeout", "connect")


def expect_deadline_in(deadline: datetime | None, seconds: float) -> None:
    assert deadline is not None
    expected = datetime.now(UTC) + timedelta(seconds=seconds)
    assert abs(deadline - expected) < DEADLINE_TOLERANCE


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


async def launch(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint, **kwargs: Any
) -> AsyncSandbox:
    options: dict[str, Any] = {"idle": None, "max_lifetime": 900, **kwargs}
    return await AsyncSandbox.create(
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
    monkeypatch.setattr("rayito.sandbox_async.main.pause_trigger_delay", lambda *_: None)


@pytest.fixture
async def managed(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> AsyncIterator[AsyncSandbox]:
    fake_rayd.servicer.lifecycle = lifecycle_state()
    capture_launch(control_plane, fake_rayd)
    sandbox = await launch(control_plane, fake_rayd, timeout=60)
    try:
        yield sandbox
    finally:
        expect_terminate(control_plane)
        await sandbox.kill()


def test_async_lifecycle_surface_matches_sync() -> None:
    for name in LIFECYCLE_VARIANTS:
        assert isinstance(inspect.getattr_static(Sandbox, name), ClassMethodVariant)
        assert isinstance(inspect.getattr_static(AsyncSandbox, name), ClassMethodVariant)
        sync_class = inspect.signature(getattr(Sandbox, f"_class_{name}"))
        async_class = inspect.signature(getattr(AsyncSandbox, f"_class_{name}"))
        assert list(sync_class.parameters) == list(async_class.parameters)
    for name in ("set_timeout", "connect", "get_info", "resume"):
        sync_method = inspect.signature(inspect.getattr_static(Sandbox, name)._method)
        async_method = inspect.signature(inspect.getattr_static(AsyncSandbox, name)._method)
        assert list(sync_method.parameters) == list(async_method.parameters)
    sync_create = inspect.signature(Sandbox.create).parameters
    async_create = inspect.signature(AsyncSandbox.create).parameters
    for kwarg in ("max_lifetime", "on_timeout"):
        assert sync_create[kwarg].default == async_create[kwarg].default
    assert list(sync_create) == list(async_create)
    assert not [name for name in sync_create if name.startswith("_")]


async def test_async_set_timeout_sends_exact(
    managed: AsyncSandbox, fake_rayd: RaydEndpoint
) -> None:
    await managed.set_timeout(150)
    request = fake_rayd.lifecycle.requests[-1]
    assert (request.mode, request.timeout_ms) == (lifecycle_pb2.TIMEOUT_MODE_EXACT, 150_000)
    assert ACCESS_TOKEN_KEY in fake_rayd.lifecycle.request_metadata[-1]
    health = await managed.get_health()
    assert health.lifecycle is not None
    expect_deadline_in(health.lifecycle.deadline, 150)

    await managed.set_timeout(10)
    health = await managed.get_health()
    assert health.lifecycle is not None
    expect_deadline_in(health.lifecycle.deadline, 10)
    assert health.lifecycle.extensions == 2


async def test_async_set_timeout_beyond_cap_raises_and_keeps_the_deadline(
    managed: AsyncSandbox, fake_rayd: RaydEndpoint
) -> None:
    before = (await managed.get_health()).lifecycle
    with pytest.raises(
        InvalidArgumentException, match=r"set_timeout\(2000\) supera el max_lifetime.*28800"
    ):
        await managed.set_timeout(2000)
    assert (await managed.get_health()).lifecycle == before
    with pytest.raises(InvalidArgumentException, match=r"set_timeout\(28801\).*28800"):
        await managed.set_timeout(28_801)
    assert len(fake_rayd.lifecycle.requests) == 1


async def test_async_class_set_timeout_uses_the_access_token_and_refuses_suspended(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_rayd.servicer.lifecycle = lifecycle_state()
    transport = TrackingTransport.for_loopback()
    stub_metadata_probe(control_plane, SANDBOX_ID, fake_rayd)
    await AsyncSandbox.set_timeout(
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

    stub_metadata_probe(control_plane, SANDBOX_ID, fake_rayd, state="SUSPENDED")
    with pytest.raises(SandboxStateException, match=r"connect\(\) lo reanuda"):
        await AsyncSandbox.set_timeout(
            SANDBOX_ID, 90, access_token=ACCESS_TOKEN, control_plane=control_plane.plane
        )
    stub_metadata_probe(control_plane, SANDBOX_ID, fake_rayd, state="TERMINATED")
    with pytest.raises(SandboxNotFoundException):
        await AsyncSandbox.set_timeout(
            SANDBOX_ID, 90, access_token=ACCESS_TOKEN, control_plane=control_plane.plane
        )
    monkeypatch.delenv("RAYITO_ACCESS_TOKEN", raising=False)
    with pytest.raises(AuthenticationException, match=r"^set_timeout\(sandbox_id\) necesita"):
        await AsyncSandbox.set_timeout(SANDBOX_ID, 90, control_plane=control_plane.plane)
    assert len(fake_rayd.lifecycle.requests) == 1


async def test_async_connect_timeout_sends_at_least(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.servicer.lifecycle = lifecycle_state(deadline_in_ms=600_000)
    stub_running(control_plane, fake_rayd)
    control_plane.microvms.add_response("create_microvm_auth_token", auth_token_response())
    sandbox = await AsyncSandbox.connect(
        SANDBOX_ID,
        timeout=300,
        access_token=ACCESS_TOKEN,
        control_plane=control_plane.plane,
        transport=fake_rayd.transport,
    )
    try:
        request = fake_rayd.lifecycle.requests[-1]
        assert (request.mode, request.timeout_ms) == (lifecycle_pb2.TIMEOUT_MODE_AT_LEAST, 300_000)
        health = await sandbox.get_health()
        assert health.lifecycle is not None
        expect_deadline_in(health.lifecycle.deadline, 600)

        stub_running(control_plane, fake_rayd)
        assert await sandbox.connect(timeout=800) is sandbox
        request = fake_rayd.lifecycle.requests[-1]
        assert (request.mode, request.timeout_ms) == (lifecycle_pb2.TIMEOUT_MODE_AT_LEAST, 800_000)

        stub_running(control_plane, fake_rayd)
        await sandbox.connect()
        assert len(fake_rayd.lifecycle.requests) == 2
    finally:
        await sandbox.close()


async def test_async_connect_reopens_an_expired_sandbox_with_its_own_timeout(
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
    sandbox = await AsyncSandbox.connect(
        SANDBOX_ID,
        access_token=ACCESS_TOKEN,
        control_plane=control_plane.plane,
        transport=fake_rayd.transport,
    )
    try:
        request = fake_rayd.lifecycle.requests[-1]
        assert (request.mode, request.timeout_ms) == (lifecycle_pb2.TIMEOUT_MODE_AT_LEAST, 60_000)
        health = await sandbox.get_health()
        assert health.lifecycle is not None and health.lifecycle.phase == "active"
    finally:
        await sandbox.close()


async def test_async_resume_reopens_a_grace_sandbox(
    managed: AsyncSandbox, control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.servicer.lifecycle = lifecycle_state(
        phase=lifecycle_pb2.LIFECYCLE_PHASE_RESUME_GRACE, deadline_in_ms=-10_000, timeout_ms=120_000
    )
    control_plane.microvms.add_response(
        "resume_microvm", {}, expected_params={"microvmIdentifier": SANDBOX_ID}
    )
    control_plane.microvms.add_response("create_microvm_auth_token", auth_token_response())
    await managed.resume()
    request = fake_rayd.lifecycle.requests[-1]
    assert (request.mode, request.timeout_ms) == (lifecycle_pb2.TIMEOUT_MODE_AT_LEAST, 120_000)


async def test_async_create_on_an_older_agent_terminates_and_raises(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.servicer.agent_version = "0.2.0"
    captured = capture_launch(control_plane, fake_rayd)
    expect_terminate(control_plane)
    with pytest.raises(LifecycleUnsupportedException, match="publica una imagen M9") as raised:
        await launch(control_plane, fake_rayd, timeout=60, on_timeout="kill")
    assert not isinstance(raised.value, SandboxNotReadyException)
    assert "lifecycle" in json.loads(str(captured["runHookPayload"]))

    capture_launch(control_plane, fake_rayd)
    with pytest.raises(LifecycleUnsupportedException):
        await launch(control_plane, fake_rayd, timeout=60, keep_on_failure=True)


async def test_async_create_without_lifecycle_kwargs_still_works_on_an_older_agent(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    captured = capture_launch(control_plane, fake_rayd)
    expect_terminate(control_plane)
    async with await AsyncSandbox.create(
        IMAGE_ARN,
        timeout=600,
        idle=None,
        access_token=ACCESS_TOKEN,
        control_plane=control_plane.plane,
        transport=fake_rayd.transport,
    ) as sandbox:
        assert (await sandbox.get_health()).lifecycle is None
    assert captured["maximumDurationInSeconds"] == 600
    assert "lifecycle" not in json.loads(str(captured["runHookPayload"]))


async def test_async_sandbox_timeout_stream_end_raises_timeout_exception(
    managed: AsyncSandbox, fake_rayd: RaydEndpoint
) -> None:
    handle = await managed.commands.run("sleep 30", background=True, timeout=None)
    pty = await managed.pty.create(timeout=None)
    fake_rayd.process.processes[handle.pid].close_streams(sandbox_timed_out())
    with pytest.raises(TimeoutException, match="sandbox_timeout"):
        await handle.wait()
    fake_rayd.pty.ptys[pty.pid].close_streams(pty_sandbox_timed_out())
    with pytest.raises(TimeoutException, match="sandbox_timeout"):
        await pty.wait()
    assert (handle.reconnects, pty.reconnects) == (0, 0)


async def test_async_failed_precondition_sandbox_timeout_is_timeout_exception(
    managed: AsyncSandbox, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.lifecycle.abort_with = (grpc.StatusCode.FAILED_PRECONDITION, "sandbox_timeout")
    with pytest.raises(TimeoutException, match="sandbox_timeout"):
        await managed.set_timeout(30)


async def test_async_unmanaged_set_timeout_raises_invalid_argument(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.servicer.lifecycle = lifecycle_state(phase=lifecycle_pb2.LIFECYCLE_PHASE_UNMANAGED)
    capture_launch(control_plane, fake_rayd)
    sandbox = await AsyncSandbox.create(
        IMAGE_ARN,
        idle=None,
        access_token=ACCESS_TOKEN,
        control_plane=control_plane.plane,
        transport=fake_rayd.transport,
    )
    try:
        with pytest.raises(InvalidArgumentException, match="max_lifetime") as raised:
            await sandbox.set_timeout(60)
        assert not isinstance(raised.value, LifecycleUnsupportedException)
        fake_rayd.servicer.lifecycle = None
        with pytest.raises(LifecycleUnsupportedException, match="imagen M9"):
            await sandbox.set_timeout(60)
    finally:
        expect_terminate(control_plane)
        await sandbox.kill()


async def test_async_pause_trigger_suspends_only_when_expired_and_running(
    control_plane: StubbedControlPlane,
    fake_rayd: RaydEndpoint,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake_rayd.servicer.lifecycle = lifecycle_state(on_timeout=lifecycle_pb2.TIMEOUT_ACTION_PAUSE)
    capture_launch(control_plane, fake_rayd)
    sandbox = await launch(
        control_plane,
        fake_rayd,
        timeout=60,
        on_timeout="pause",
        idle=IdlePolicy(max_idle_seconds=300, auto_resume=True),
    )
    try:
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
        await trigger.fire()
        assert len(fake_rayd.servicer.health_calls) == health_calls
        assert suspended == []

        state["value"] = "RUNNING"
        fake_rayd.servicer.lifecycle = lifecycle_state(
            on_timeout=lifecycle_pb2.TIMEOUT_ACTION_PAUSE, deadline_in_ms=120_000
        )
        await trigger.fire()
        assert suspended == []
        assert trigger.scheduled_delay == pytest.approx(120 + TRIGGER_SLACK, abs=3)

        fake_rayd.servicer.lifecycle = lifecycle_state(
            phase=lifecycle_pb2.LIFECYCLE_PHASE_EXPIRED,
            on_timeout=lifecycle_pb2.TIMEOUT_ACTION_PAUSE,
            deadline_in_ms=-1_000,
        )
        await trigger.fire()
        assert suspended == [SANDBOX_ID]
        assert trigger.scheduled_delay is None
        assert sandbox._paused is False

        def broken(sandbox_id: str) -> Any:
            raise RuntimeError("get-microvm caído")

        monkeypatch.setattr(sandbox._control_plane, "get_microvm", broken)
        with caplog.at_level(logging.WARNING, logger="rayito.sandbox"):
            await trigger.fire()
        assert any("disparador del plazo" in record.getMessage() for record in caplog.records)
    finally:
        monkeypatch.undo()
        expect_terminate(control_plane)
        await sandbox.kill()
    assert sandbox._deadline_trigger.scheduled_delay is None


def expired_pause_state() -> lifecycle_pb2.LifecycleState:
    return lifecycle_state(
        phase=lifecycle_pb2.LIFECYCLE_PHASE_EXPIRED,
        on_timeout=lifecycle_pb2.TIMEOUT_ACTION_PAUSE,
        auto_resume=True,
        deadline_in_ms=-2_000,
    )


async def test_async_a_brief_deadline_pause_reopens_with_the_auto_resume_rule(
    control_plane: StubbedControlPlane,
    fake_rayd: RaydEndpoint,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    no_pause_trigger(monkeypatch)
    fake_rayd.servicer.lifecycle = lifecycle_state(
        on_timeout=lifecycle_pb2.TIMEOUT_ACTION_PAUSE, auto_resume=True
    )
    capture_launch(control_plane, fake_rayd)
    sandbox = await launch(
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
            await sandbox.files.make_dir("/home/user/never")
        await sandbox._suspend_for_deadline()
        with pytest.raises(TimeoutException, match="sandbox_timeout"):
            await sandbox.files.make_dir("/home/user/not-resumed")
        assert fake_rayd.lifecycle.requests == []

        await sandbox._suspend_for_deadline()
        fake_rayd.resume()
        assert await sandbox.files.make_dir("/home/user/unary") is True
        assert [(r.mode, r.timeout_ms) for r in fake_rayd.lifecycle.requests] == [
            (lifecycle_pb2.TIMEOUT_MODE_EXACT, 300_000)
        ]
        assert sandbox._lifecycle is not None and sandbox._lifecycle.phase == "active"
        expect_deadline_in(sandbox._lifecycle.deadline, 300)

        fake_rayd.servicer.lifecycle = expired_pause_state()
        await sandbox._suspend_for_deadline()
        fake_rayd.resume()
        assert (await sandbox.commands.run("echo hola")).stdout == "hola\n"
        assert len(fake_rayd.lifecycle.requests) == 2

        fake_rayd.servicer.lifecycle = expired_pause_state()
        fake_rayd.resume()
        with pytest.raises(TimeoutException, match="sandbox_timeout"):
            await sandbox.files.make_dir("/home/user/stray")
        assert len(fake_rayd.lifecycle.requests) == 2
    finally:
        monkeypatch.undo()
        expect_terminate(control_plane)
        await sandbox.kill()


async def launch_paused_by_deadline(
    control_plane: StubbedControlPlane,
    fake_rayd: RaydEndpoint,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncSandbox:
    """Un sandbox `pause` con `auto_resume` que este cliente ya suspendió por
    su plazo, con `rayd` todavía `expired` tras el resume."""
    no_pause_trigger(monkeypatch)
    fake_rayd.servicer.lifecycle = lifecycle_state(
        on_timeout=lifecycle_pb2.TIMEOUT_ACTION_PAUSE, auto_resume=True
    )
    capture_launch(control_plane, fake_rayd)
    sandbox = await launch(
        control_plane,
        fake_rayd,
        timeout=60,
        on_timeout="pause",
        idle=IdlePolicy(max_idle_seconds=300, auto_resume=True),
    )
    monkeypatch.setattr(sandbox._control_plane, "suspend_microvm", lambda _: True)
    fake_rayd.servicer.timeout_gate = True
    fake_rayd.servicer.lifecycle = expired_pause_state()
    await sandbox._suspend_for_deadline()
    return sandbox


async def test_async_a_sandbox_timeout_before_the_freeze_does_not_burn_the_reopen(
    control_plane: StubbedControlPlane,
    fake_rayd: RaydEndpoint,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = await launch_paused_by_deadline(control_plane, fake_rayd, monkeypatch)
    try:
        with pytest.raises(TimeoutException, match="sandbox_timeout"):
            await sandbox.files.make_dir("/home/user/before-freeze")
        assert fake_rayd.lifecycle.requests == []
        fake_rayd.resume()
        assert await sandbox.files.make_dir("/home/user/after-resume") is True
        assert [(r.mode, r.timeout_ms) for r in fake_rayd.lifecycle.requests] == [
            (lifecycle_pb2.TIMEOUT_MODE_EXACT, 300_000)
        ]
    finally:
        monkeypatch.undo()
        expect_terminate(control_plane)
        await sandbox.kill()


async def test_async_concurrent_callers_share_one_reopen(
    control_plane: StubbedControlPlane,
    fake_rayd: RaydEndpoint,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = await launch_paused_by_deadline(control_plane, fake_rayd, monkeypatch)
    entered: list[int] = []
    both_timed_out = asyncio.Event()
    reopened = sandbox._reopened_after_deadline_pause
    refresh = sandbox._refresh_health

    async def counting(*args: Any) -> bool:
        entered.append(1)
        if len(entered) == 2:
            both_timed_out.set()
        return await reopened(*args)

    async def held_refresh() -> None:
        await asyncio.wait_for(both_timed_out.wait(), timeout=5)
        await refresh()

    monkeypatch.setattr(sandbox, "_reopened_after_deadline_pause", counting)
    monkeypatch.setattr(sandbox, "_refresh_health", held_refresh)
    try:
        fake_rayd.resume()
        results = await asyncio.gather(
            sandbox.files.make_dir("/home/user/first"),
            sandbox.files.make_dir("/home/user/second"),
        )
        assert list(results) == [True, True]
        assert len(fake_rayd.lifecycle.requests) == 1
    finally:
        monkeypatch.undo()
        expect_terminate(control_plane)
        await sandbox.kill()


@pytest.mark.parametrize("ending", ["close", "kill", "exit"])
async def test_async_ending_the_handle_cancels_the_pause_trigger(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint, ending: str
) -> None:
    fake_rayd.servicer.lifecycle = lifecycle_state(on_timeout=lifecycle_pb2.TIMEOUT_ACTION_PAUSE)
    capture_launch(control_plane, fake_rayd)
    sandbox = await launch(
        control_plane,
        fake_rayd,
        timeout=60,
        on_timeout="pause",
        idle=IdlePolicy(max_idle_seconds=300, auto_resume=True),
    )
    trigger = sandbox._deadline_trigger
    assert trigger.scheduled_delay == pytest.approx(60 + TRIGGER_SLACK, abs=3)
    handle = trigger._handle
    assert handle is not None and not handle.cancelled()
    if ending == "close":
        await sandbox.close()
    else:
        expect_terminate(control_plane)
        if ending == "kill":
            await sandbox.kill()
        else:
            await sandbox.__aexit__(None, None, None)
    assert trigger.scheduled_delay is None
    assert handle.cancelled()
    trigger.arm(30)
    assert trigger.scheduled_delay is None
    control_plane.microvms.assert_no_pending_responses()


async def test_async_get_info_expires_at_is_the_logical_deadline(
    managed: AsyncSandbox, control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    deadline_ms = int(fake_rayd.servicer.lifecycle.deadline_unix_ms)
    stub_running(control_plane, fake_rayd)
    info = await managed.get_info()
    assert info.expires_at == datetime.fromtimestamp(deadline_ms / 1000, tz=UTC)
    assert info.platform_expires_at == STARTED_AT + timedelta(seconds=900)

    fake_rayd.servicer.lifecycle = lifecycle_state(deadline_in_ms=300_000)
    moved_ms = int(fake_rayd.servicer.lifecycle.deadline_unix_ms)
    stub_running(control_plane, fake_rayd)
    moved = await managed.get_info()
    assert moved.expires_at == datetime.fromtimestamp(moved_ms / 1000, tz=UTC)

    health_calls = len(fake_rayd.servicer.health_calls)
    control_plane.microvms.add_response(
        "get_microvm", microvm_response(endpoint=fake_rayd.host, state="SUSPENDED")
    )
    asleep = await managed.get_info()
    assert len(fake_rayd.servicer.health_calls) == health_calls
    assert asleep.expires_at == moved.expires_at


async def test_async_class_get_info_reads_the_logical_deadline(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    fake_rayd.servicer.lifecycle = lifecycle_state(deadline_in_ms=120_000)
    deadline_ms = int(fake_rayd.servicer.lifecycle.deadline_unix_ms)
    stub_metadata_probe(control_plane, SANDBOX_ID, fake_rayd)
    info = await AsyncSandbox.get_info(
        SANDBOX_ID, control_plane=control_plane.plane, transport=TrackingTransport.for_loopback()
    )
    assert info.expires_at == datetime.fromtimestamp(deadline_ms / 1000, tz=UTC)


@pytest.mark.parametrize(("name", "value"), [("max_lifetime", 900), ("on_timeout", "kill")])
async def test_async_pool_rejects_lifecycle_kwargs(name: str, value: object) -> None:
    kwargs: Any = {name: value}
    with pytest.raises(InvalidArgumentException, match=f"`{name}`"):
        await AsyncSandbox.create(pool=cast("AsyncSandboxPool", object()), **kwargs)


async def test_async_create_records_the_lifecycle_kwargs_for_reincarnate(
    managed: AsyncSandbox, control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    options = managed._launch_options
    assert options is not None
    assert (options.timeout, options.max_lifetime, options.on_timeout) == (60, 900, None)
    relaunch = launch_kwargs(options)
    assert (relaunch["max_lifetime"], relaunch["on_timeout"]) == (900, None)

    captured = capture_launch(control_plane, fake_rayd)
    shim_like = await AsyncSandbox.create(
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
        await shim_like.close()


async def test_async_kill_mode_never_arms_the_pause_trigger(managed: AsyncSandbox) -> None:
    assert managed._deadline_trigger.scheduled_delay is None
    await managed.set_timeout(30)
    assert managed._deadline_trigger.scheduled_delay is None
