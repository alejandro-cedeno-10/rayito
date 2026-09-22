"""M6 hardening seen from the sync client (design D2, D5, D7, D11, D12):
`SandboxHealth.imds_blocked`/`hook_anomalies` and their warnings (the IMDS
one only once `rayd`'s verification window has elapsed), `cpu_time_limit`
in the payload, `SandboxInfo.ingress`/`egress` from `get-microvm`,
`DiskFullException`, and the reconnection of every handle kind (command,
watch, `run_code`) through a `/suspend` nobody checkpointed (the VM stays
`RUNNING`, the gate reopens on the third attempt without a new
generation)."""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Iterator
from typing import Any

import grpc
import pytest

from rayito import DiskFullException, Execution, Sandbox
from rayito._aws import sandbox_info_from_response
from rayito._payload import build_run_hook_payload
from rayito._sandbox_base import (
    HOOK_ANOMALIES_WARNING,
    IMDS_VERIFY_BUDGET_MS,
    build_launch_plan,
    health_from_proto,
    health_reconnected,
    is_gate_refusal,
)
from rayito._transport import translate_rpc_error
from rayito.exceptions import InvalidArgumentException, RateLimitException, SandboxStateException
from rayito.v1 import filesystem_pb2, health_pb2

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
from .test_reconnect_sync import HOME, Collector, fast_state_checks, stub_launch, wait_until

__all__ = ["fast_state_checks"]

ROLE_ARN = "arn:aws:iam::123456789012:role/rayito-execution"
CONNECTOR_ARN = "arn:aws:lambda:us-east-1:123456789012:network-connector:rayito-egress"
ALL_INGRESS = "arn:aws:lambda:us-east-1:aws:network-connector:aws-network-connector:ALL_INGRESS"


def payload_dict(**kwargs: Any) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(
        build_run_hook_payload(access_token=ACCESS_TOKEN, **kwargs)
    )
    return payload


def test_cpu_time_limit_is_serialized_only_when_set() -> None:
    assert "limits" not in payload_dict()
    assert payload_dict(cpu_time_limit=2)["limits"] == {"cpu_seconds": 2}
    assert payload_dict(cpu_time_limit=28_800)["limits"] == {"cpu_seconds": 28_800}
    for bad in (0, 28_801, -1, True, 2.5, "2"):
        with pytest.raises(InvalidArgumentException, match="cpu_time_limit"):
            build_run_hook_payload(access_token=ACCESS_TOKEN, cpu_time_limit=bad)  # type: ignore[arg-type]


def test_launch_plan_carries_the_cpu_limit_in_the_payload() -> None:
    plan = build_launch_plan(
        image_arn=IMAGE_ARN,
        region="us-east-1",
        template_version=None,
        timeout=600,
        idle=None,
        envs=None,
        execution_role_arn=None,
        allowed_ports=None,
        ingress=None,
        egress=None,
        logging="disabled",
        access_token=ACCESS_TOKEN,
        cpu_time_limit=5,
    )
    assert json.loads(plan.request.run_hook_payload)["limits"] == {"cpu_seconds": 5}


def test_health_from_proto_reads_the_new_fields_and_defaults_them() -> None:
    full = health_from_proto(
        health_pb2.HealthResponse(
            agent_ready=True, kernel_ready=True, imds_blocked=True, hook_anomalies=4
        )
    )
    assert full.imds_blocked is True
    assert full.hook_anomalies == 4
    older_agent = health_from_proto(health_pb2.HealthResponse(agent_ready=True, kernel_ready=True))
    assert older_agent.imds_blocked is False
    assert older_agent.hook_anomalies == 0


def test_sandbox_info_reads_the_connectors_in_order() -> None:
    response = microvm_response(state="RUNNING")
    response["ingressNetworkConnectors"] = [ALL_INGRESS]
    response["egressNetworkConnectors"] = [CONNECTOR_ARN, ALL_INGRESS]
    info = sandbox_info_from_response(response)
    assert info.ingress == (ALL_INGRESS,)
    assert info.egress == (CONNECTOR_ARN, ALL_INGRESS)
    bare = sandbox_info_from_response(microvm_response(state="RUNNING"))
    assert bare.ingress == () and bare.egress == ()


def test_resource_exhausted_details_select_the_exception() -> None:
    for detail in ("disk_reserve", "disk_full"):
        error = translate_rpc_error(
            FakeRpcError(grpc.StatusCode.RESOURCE_EXHAUSTED, details=detail), filesystem=True
        )
        assert isinstance(error, DiskFullException), detail
        assert error.grpc_code is grpc.StatusCode.RESOURCE_EXHAUSTED
    other = translate_rpc_error(FakeRpcError(grpc.StatusCode.RESOURCE_EXHAUSTED, details="max 64"))
    assert isinstance(other, RateLimitException)


def test_health_reconnected_accepts_a_running_vm_without_a_new_generation() -> None:
    same = health_pb2.HealthResponse(agent_ready=True, kernel_ready=True, resume_generation=3)
    assert not health_reconnected(same, seen_generation=3, suspending=True)
    assert health_reconnected(same, seen_generation=3, suspending=True, running=True)
    assert health_reconnected(same, seen_generation=3, suspending=False)
    not_ready = health_pb2.HealthResponse(agent_ready=True, kernel_ready=False, resume_generation=3)
    assert not health_reconnected(not_ready, seen_generation=3, suspending=True, running=True)


def test_gate_refusal_is_the_translated_phase_gate() -> None:
    gate = translate_rpc_error(FakeRpcError(grpc.StatusCode.UNAVAILABLE, details="suspending"))
    assert isinstance(gate, SandboxStateException)
    assert is_gate_refusal(gate)
    assert not is_gate_refusal(SandboxStateException("suspended sin auto-resume"))
    exhausted = RateLimitException("x", grpc_code=grpc.StatusCode.RESOURCE_EXHAUSTED)
    assert not is_gate_refusal(exhausted)


@pytest.fixture
def sandbox_with_role(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint, caplog: pytest.LogCaptureFixture
) -> Iterator[Sandbox]:
    caplog.set_level(logging.WARNING, logger="rayito.sandbox")
    response = microvm_response(endpoint=fake_rayd.host)
    response["executionRoleArn"] = ROLE_ARN
    control_plane.microvms.add_response("run_microvm", response)
    control_plane.microvms.add_response("create_microvm_auth_token", auth_token_response())
    created = Sandbox.create(
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
        created.kill()


@pytest.fixture
def sandbox(control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint) -> Iterator[Sandbox]:
    stub_launch(control_plane, fake_rayd)
    created = Sandbox.create(
        IMAGE_ARN,
        idle=None,
        access_token=ACCESS_TOKEN,
        control_plane=control_plane.plane,
        transport=fake_rayd.transport,
        reconnect_timeout=5.0,
    )
    try:
        yield created
    finally:
        control_plane.microvms.add_response(
            "terminate_microvm", {}, expected_params={"microvmIdentifier": SANDBOX_ID}
        )
        created.kill()


def anomaly_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING and "hook_anomalies" in record.getMessage()
    ]


def imds_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    records = [*caplog.get_records("setup"), *caplog.records]
    return [
        record.getMessage()
        for record in records
        if record.levelno == logging.WARNING and "imds_blocked" in record.getMessage()
    ]


def test_hook_anomalies_warn_once_per_generation(
    sandbox: Sandbox, fake_rayd: RaydEndpoint, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="rayito.sandbox")
    assert sandbox.get_health().hook_anomalies == 0
    assert anomaly_warnings(caplog) == []
    with fake_rayd.servicer.lock:
        fake_rayd.servicer.hook_anomalies = 3
    for _ in range(3):
        assert sandbox.get_health().hook_anomalies == 3
    assert len(anomaly_warnings(caplog)) == 1
    assert anomaly_warnings(caplog)[0] == HOOK_ANOMALIES_WARNING % (SANDBOX_ID, 3)
    with fake_rayd.servicer.lock:
        fake_rayd.servicer.resume_generation = 1
        fake_rayd.servicer.hook_anomalies = 4
    sandbox.get_health()
    sandbox.get_health()
    assert len(anomaly_warnings(caplog)) == 2
    assert imds_warnings(caplog) == [], "no execution role: IMDS is nobody's credentials"


def advance_uptime(fake_rayd: RaydEndpoint, by_ms: int) -> None:
    with fake_rayd.servicer.lock:
        fake_rayd.servicer.uptime_ms += by_ms


def test_imds_open_warns_once_only_with_a_role_and_after_the_verification_window(
    sandbox_with_role: Sandbox, fake_rayd: RaydEndpoint, caplog: pytest.LogCaptureFixture
) -> None:
    assert imds_warnings(caplog) == [], "readiness lands before rayd verified the block"
    for _ in range(2):
        assert sandbox_with_role.get_health().imds_blocked is False
    advance_uptime(fake_rayd, IMDS_VERIFY_BUDGET_MS - 1)
    assert sandbox_with_role.get_health().imds_blocked is False
    assert imds_warnings(caplog) == [], "still inside the 10 s verification budget"
    advance_uptime(fake_rayd, 1)
    for _ in range(3):
        assert sandbox_with_role.get_health().imds_blocked is False
    assert len(imds_warnings(caplog)) == 1, "one verdict, one warning"
    with fake_rayd.servicer.lock:
        fake_rayd.servicer.imds_blocked = True
    assert sandbox_with_role.get_health().imds_blocked is True
    assert len(imds_warnings(caplog)) == 1


def test_imds_verified_inside_the_window_never_warns(
    sandbox_with_role: Sandbox, fake_rayd: RaydEndpoint, caplog: pytest.LogCaptureFixture
) -> None:
    """The capabilities image as measured: `imds_blocked` flips to `True`
    a fraction of a second after `kernel_ready`, well inside the budget."""
    advance_uptime(fake_rayd, 100)
    with fake_rayd.servicer.lock:
        fake_rayd.servicer.imds_blocked = True
    assert sandbox_with_role.get_health().imds_blocked is True
    advance_uptime(fake_rayd, IMDS_VERIFY_BUDGET_MS)
    assert sandbox_with_role.get_health().imds_blocked is True
    assert imds_warnings(caplog) == []


class GateThatReopensOnTheThirdAttempt:
    """A re-subscription (`Connect`, `WatchDir`, `Reattach`) refused with
    `UNAVAILABLE suspending` twice and accepted on the third: what the
    client sees while `rayd`'s watchdog has not yet recovered a `/suspend`
    nobody checkpointed."""

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
def test_handle_reconnects_through_a_stale_suspend_when_the_vm_stays_running(
    sandbox: Sandbox,
    fake_rayd: RaydEndpoint,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    always_running(monkeypatch, sandbox, fake_rayd.host)
    handle = sandbox.commands.run("seq 40", background=True, timeout=None)
    collector = Collector(handle)
    wait_until(lambda: len(collector.chunks) >= 2)
    generation_before = sandbox.resume_generation
    fake_rayd.suspend()
    gate = GateThatReopensOnTheThirdAttempt(fake_rayd.process)
    monkeypatch.setattr(fake_rayd.process, "_gate_phase", gate.gate)
    collector.join(budget=30.0)
    assert collector.error is None
    assert "".join(collector.chunks) == "".join(f"{n}\n" for n in range(1, 41))
    assert handle.wait().exit_code == 0
    assert handle.reconnects == 1
    assert gate.refused == 2
    assert sandbox.resume_generation == generation_before, "no new generation was needed"
    assert sandbox.get_health().resume_generation == generation_before


@pytest.mark.usefixtures("fast_state_checks")
def test_watch_reconnects_through_a_stale_suspend_when_the_vm_stays_running(
    sandbox: Sandbox,
    fake_rayd: RaydEndpoint,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    always_running(monkeypatch, sandbox, fake_rayd.host)
    exits: list[Exception] = []
    handle = sandbox.files.watch_dir(HOME, recursive=True, on_exit=exits.append)
    generation_before = sandbox.resume_generation
    fake_rayd.suspend()
    gate = GateThatReopensOnTheThirdAttempt(fake_rayd.filesystem)
    monkeypatch.setattr(fake_rayd.filesystem, "_gate_phase", gate.gate)
    wait_until(lambda: gate.refused == 2 and fake_rayd.filesystem.live_watches == 1, budget=30.0)
    fake_rayd.filesystem.push_event(HOME, "after.txt", filesystem_pb2.FILESYSTEM_EVENT_TYPE_CREATE)
    wait_until(lambda: any(event.name == "after.txt" for event in handle.get_new_events()))
    assert handle.is_running is True
    assert handle.reconnects == 1
    assert exits == []
    assert len(fake_rayd.filesystem.watch_calls) == 2, "the open and the accepted reopen"
    assert sandbox.resume_generation == generation_before
    handle.stop()


@pytest.mark.usefixtures("fast_state_checks")
def test_run_code_reattaches_through_a_stale_suspend_when_the_vm_stays_running(
    sandbox: Sandbox,
    fake_rayd: RaydEndpoint,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    always_running(monkeypatch, sandbox, fake_rayd.host)
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
    generation_before = sandbox.resume_generation
    fake_rayd.suspend()
    gate = GateThatReopensOnTheThirdAttempt(fake_rayd.code)
    monkeypatch.setattr(fake_rayd.code, "_gate_phase", gate.gate)
    thread.join(30.0)
    assert not thread.is_alive()
    assert errors == []
    execution = results[0]
    assert execution.error is None
    assert execution.text == "'slow'"
    assert gate.refused == 2
    assert len(fake_rayd.code.reattach_requests) == 3, "two refusals + the accepted Reattach"
    assert all(
        request.execution_id == run_record.record.execution_id
        for request in fake_rayd.code.reattach_requests
    )
    assert len(fake_rayd.code.execute_requests) == 1, "the cell never ran twice"
    assert sandbox.resume_generation == generation_before
