"""M6 hardening seen from the async client: the same contract as
`test_m6_hardening_sync` for `AsyncSandbox` (warnings, `cpu_time_limit`,
and the reconnection of command, watch and `run_code` handles through a
stale `/suspend` with the gate reopening on the third attempt)."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import AsyncIterator
from typing import Any

import grpc
import pytest

from rayito import AsyncSandbox, Execution
from rayito._sandbox_base import HOOK_ANOMALIES_WARNING, IMDS_VERIFY_BUDGET_MS
from rayito.v1 import filesystem_pb2

from .conftest import (
    ACCESS_TOKEN,
    IMAGE_ARN,
    SANDBOX_ID,
    RaydEndpoint,
    StubbedControlPlane,
    always_running,
    auth_token_response,
    microvm_response,
)
from .test_reconnect_async import (
    HOME,
    WAIT_BUDGET_SECONDS,
    Collector,
    fast_state_checks,
    stub_launch,
    wait_until,
)

__all__ = ["fast_state_checks"]

ROLE_ARN = "arn:aws:iam::123456789012:role/rayito-execution"


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
        reconnect_timeout=5.0,
        cpu_time_limit=3,
    )
    try:
        yield created
    finally:
        control_plane.microvms.add_response(
            "terminate_microvm", {}, expected_params={"microvmIdentifier": SANDBOX_ID}
        )
        await created.kill()


@pytest.fixture
async def sandbox_with_role(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint, caplog: pytest.LogCaptureFixture
) -> AsyncIterator[AsyncSandbox]:
    caplog.set_level(logging.WARNING, logger="rayito.sandbox")
    response = microvm_response(endpoint=fake_rayd.host)
    response["executionRoleArn"] = ROLE_ARN
    control_plane.microvms.add_response("run_microvm", response)
    control_plane.microvms.add_response("create_microvm_auth_token", auth_token_response())
    created = await AsyncSandbox.create(
        IMAGE_ARN,
        idle=None,
        execution_role_arn=ROLE_ARN,
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


def warnings_about(caplog: pytest.LogCaptureFixture, needle: str) -> list[str]:
    records = [*caplog.get_records("setup"), *caplog.records]
    return [
        record.getMessage()
        for record in records
        if record.levelno == logging.WARNING and needle in record.getMessage()
    ]


async def test_health_reads_the_new_fields(sandbox: AsyncSandbox, fake_rayd: RaydEndpoint) -> None:
    health = await sandbox.get_health()
    assert health.imds_blocked is False
    assert health.hook_anomalies == 0
    with fake_rayd.servicer.lock:
        fake_rayd.servicer.imds_blocked = True
        fake_rayd.servicer.hook_anomalies = 2
    health = await sandbox.get_health()
    assert health.imds_blocked is True
    assert health.hook_anomalies == 2


async def test_hook_anomalies_warn_once_per_generation(
    sandbox: AsyncSandbox, fake_rayd: RaydEndpoint, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="rayito.sandbox")
    with fake_rayd.servicer.lock:
        fake_rayd.servicer.hook_anomalies = 1
    for _ in range(3):
        await sandbox.get_health()
    assert warnings_about(caplog, "hook_anomalies") == [HOOK_ANOMALIES_WARNING % (SANDBOX_ID, 1)]
    with fake_rayd.servicer.lock:
        fake_rayd.servicer.resume_generation = 1
    await sandbox.get_health()
    assert len(warnings_about(caplog, "hook_anomalies")) == 2
    assert warnings_about(caplog, "imds_blocked") == []


def advance_uptime(fake_rayd: RaydEndpoint, by_ms: int) -> None:
    with fake_rayd.servicer.lock:
        fake_rayd.servicer.uptime_ms += by_ms


async def test_imds_open_warns_once_only_with_a_role_and_after_the_verification_window(
    sandbox_with_role: AsyncSandbox, fake_rayd: RaydEndpoint, caplog: pytest.LogCaptureFixture
) -> None:
    assert warnings_about(caplog, "imds_blocked") == []
    advance_uptime(fake_rayd, IMDS_VERIFY_BUDGET_MS - 1)
    assert (await sandbox_with_role.get_health()).imds_blocked is False
    assert warnings_about(caplog, "imds_blocked") == []
    advance_uptime(fake_rayd, 1)
    for _ in range(2):
        assert (await sandbox_with_role.get_health()).imds_blocked is False
    assert len(warnings_about(caplog, "imds_blocked")) == 1
    with fake_rayd.servicer.lock:
        fake_rayd.servicer.imds_blocked = True
    assert (await sandbox_with_role.get_health()).imds_blocked is True
    assert len(warnings_about(caplog, "imds_blocked")) == 1


async def test_imds_verified_inside_the_window_never_warns(
    sandbox_with_role: AsyncSandbox, fake_rayd: RaydEndpoint, caplog: pytest.LogCaptureFixture
) -> None:
    advance_uptime(fake_rayd, 100)
    with fake_rayd.servicer.lock:
        fake_rayd.servicer.imds_blocked = True
    assert (await sandbox_with_role.get_health()).imds_blocked is True
    advance_uptime(fake_rayd, IMDS_VERIFY_BUDGET_MS)
    assert (await sandbox_with_role.get_health()).imds_blocked is True
    assert warnings_about(caplog, "imds_blocked") == []


class GateThatReopensOnTheThirdAttempt:
    def __init__(self, service: Any) -> None:
        self.service = service
        self.refused = 0
        self.lock = threading.Lock()

    def gate(self, context: grpc.ServicerContext) -> None:
        with self.lock:
            if self.refused < 2:
                self.refused += 1
                context.abort(grpc.StatusCode.UNAVAILABLE, "suspending")
            self.service.phase = None


@pytest.mark.usefixtures("fast_state_checks")
async def test_handle_reconnects_through_a_stale_suspend_when_the_vm_stays_running(
    sandbox: AsyncSandbox, fake_rayd: RaydEndpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    always_running(monkeypatch, sandbox, fake_rayd.host)
    handle = await sandbox.commands.run("seq 40", background=True, timeout=None)
    collector = Collector(handle)
    await wait_until(lambda: len(collector.chunks) >= 2)
    generation_before = sandbox.resume_generation
    fake_rayd.suspend()
    gate = GateThatReopensOnTheThirdAttempt(fake_rayd.process)
    monkeypatch.setattr(fake_rayd.process, "_gate_phase", gate.gate)
    await collector.join(budget=30.0)
    assert collector.error is None
    assert "".join(collector.chunks) == "".join(f"{n}\n" for n in range(1, 41))
    assert (await handle.wait()).exit_code == 0
    assert handle.reconnects == 1
    assert gate.refused == 2
    assert sandbox.resume_generation == generation_before


@pytest.mark.usefixtures("fast_state_checks")
async def test_watch_reconnects_through_a_stale_suspend_when_the_vm_stays_running(
    sandbox: AsyncSandbox, fake_rayd: RaydEndpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    always_running(monkeypatch, sandbox, fake_rayd.host)
    exits: list[Exception] = []
    handle = await sandbox.files.watch_dir(HOME, recursive=True, on_exit=exits.append)
    generation_before = sandbox.resume_generation
    fake_rayd.suspend()
    gate = GateThatReopensOnTheThirdAttempt(fake_rayd.filesystem)
    monkeypatch.setattr(fake_rayd.filesystem, "_gate_phase", gate.gate)
    await wait_until(
        lambda: gate.refused == 2 and fake_rayd.filesystem.live_watches == 1, budget=30.0
    )
    fake_rayd.filesystem.push_event(HOME, "after.txt", filesystem_pb2.FILESYSTEM_EVENT_TYPE_CREATE)
    seen: list[str] = []
    deadline = asyncio.get_running_loop().time() + WAIT_BUDGET_SECONDS
    while "after.txt" not in seen:
        assert asyncio.get_running_loop().time() < deadline
        seen.extend(event.name for event in await handle.get_new_events())
        await asyncio.sleep(0.02)
    assert handle.is_running is True
    assert handle.reconnects == 1
    assert exits == []
    assert len(fake_rayd.filesystem.watch_calls) == 2
    assert sandbox.resume_generation == generation_before
    await handle.stop()


@pytest.mark.usefixtures("fast_state_checks")
async def test_run_code_reattaches_through_a_stale_suspend_when_the_vm_stays_running(
    sandbox: AsyncSandbox, fake_rayd: RaydEndpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    always_running(monkeypatch, sandbox, fake_rayd.host)
    task: asyncio.Task[Execution] = asyncio.get_running_loop().create_task(
        sandbox.run_code("slow 2", timeout=None)
    )
    await wait_until(lambda: bool(fake_rayd.code.runs))
    run_record = next(iter(fake_rayd.code.runs.values()))
    await wait_until(lambda: len(run_record.ring) >= 2)
    generation_before = sandbox.resume_generation
    fake_rayd.suspend()
    gate = GateThatReopensOnTheThirdAttempt(fake_rayd.code)
    monkeypatch.setattr(fake_rayd.code, "_gate_phase", gate.gate)
    execution = await asyncio.wait_for(task, timeout=30.0)
    assert execution.error is None
    assert execution.text == "'slow'"
    assert gate.refused == 2
    assert len(fake_rayd.code.reattach_requests) == 3
    assert len(fake_rayd.code.execute_requests) == 1
    assert sandbox.resume_generation == generation_before
