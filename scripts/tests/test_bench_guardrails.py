"""The guardrail and data paths of ``bench_cold_start.py`` that the spec
scenarios describe, network-free: a fake control plane (the ``ControlPlane``
protocol of ``rayito._aws``), a fake ``HealthServiceStub`` raising chosen
gRPC codes and a fake clock. Covers the probe's status classification and
gap recording, ``wait_ready`` timings and its 120 s cap, ``boot_vm``'s
``not_ready`` and throttled-launch samples, the watchdog, the sweep under a
failing ``terminate-microvm``, the guarded image block and the exit path
that writes ``meta.aborted: true`` on Ctrl-C or any other exception."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import grpc
import pytest
from botocore.exceptions import ClientError

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import bench_cold_start as bench
from rayito._aws import LaunchRequest, PortSpec
from rayito._models import SandboxInfo, SandboxListItem
from rayito.exceptions import SandboxException
from rayito.v1 import health_pb2

RPC_SECONDS = 0.09
IMAGE_ARN = "arn:aws:lambda:us-east-1:1:microvm-image:rayito-base"


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakeRpcError(grpc.RpcError):
    def __init__(self, code: grpc.StatusCode) -> None:
        super().__init__(code.name)
        self._code = code

    def code(self) -> grpc.StatusCode:
        return self._code

    def details(self) -> str:
        return self._code.name


def health(
    *, agent: bool, kernel: bool, generation: int = 0
) -> health_pb2.HealthResponse:
    return health_pb2.HealthResponse(
        agent_ready=agent,
        kernel_ready=kernel,
        uptime_ms=23_000,
        agent_version="0.1.0",
        resume_generation=generation,
    )


class FakeHealthStub:
    """Replays ``outcomes`` (a response or an exception) one per call, each
    taking ``rpc_seconds`` of fake time; the last outcome repeats."""

    def __init__(
        self,
        clock: FakeClock,
        outcomes: Sequence[Any],
        rpc_seconds: float = RPC_SECONDS,
    ) -> None:
        self._clock = clock
        self._outcomes = list(outcomes)
        self._rpc_seconds = rpc_seconds
        self.calls = 0

    def Health(self, request: Any, timeout: float) -> health_pb2.HealthResponse:
        del request, timeout
        self.calls += 1
        outcome = (
            self._outcomes.pop(0) if len(self._outcomes) > 1 else self._outcomes[0]
        )
        self._clock.now += self._rpc_seconds
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def probe(
    clock: FakeClock, outcomes: Sequence[Any]
) -> tuple[bench.HealthProbe, FakeHealthStub]:
    stub = FakeHealthStub(clock, outcomes)
    return bench.HealthProbe(
        "h", "jwe", stub=stub, clock=clock, sleep=clock.sleep
    ), stub


def sandbox_info(vm_id: str, state: str = "RUNNING") -> SandboxInfo:
    return SandboxInfo(
        sandbox_id=vm_id,
        state=state,
        endpoint=f"{vm_id}.example",
        template=IMAGE_ARN,
        template_version="10.0",
        started_at=datetime(2026, 9, 16, tzinfo=timezone.utc),
        maximum_duration_seconds=600,
        state_reason="boot" if state != "RUNNING" else None,
    )


@dataclass
class FakeControlPlane:
    """Records every call; ``terminate_failures`` names ids whose
    ``terminate-microvm`` raises, ``fail_terminate_with`` says how."""

    region: str = "us-east-1"
    launched: list[str] = field(default_factory=list)
    terminated: list[str] = field(default_factory=list)
    tokens: list[str] = field(default_factory=list)
    fetched: list[str] = field(default_factory=list)
    terminate_failures: set[str] = field(default_factory=set)
    fail_terminate_with: type[Exception] = ConnectionError
    launch_state: str = "RUNNING"

    def resolve_template_arn(self, template: str) -> str:
        return template

    def run_microvm(self, request: LaunchRequest) -> SandboxInfo:
        del request
        vm_id = f"microvm-{len(self.launched)}"
        self.launched.append(vm_id)
        return sandbox_info(vm_id)

    def get_microvm(self, sandbox_id: str) -> SandboxInfo:
        self.fetched.append(sandbox_id)
        return sandbox_info(sandbox_id, self.launch_state)

    def list_microvms(
        self,
        *,
        image_arn: str | None = None,
        image_version: str | None = None,
        states: Iterable[str] | None = None,
    ) -> Iterator[SandboxListItem]:
        del image_arn, image_version, states
        return iter(())

    def terminate_microvm(self, sandbox_id: str) -> bool:
        if sandbox_id in self.terminate_failures:
            raise self.fail_terminate_with(sandbox_id)
        self.terminated.append(sandbox_id)
        return True

    def suspend_microvm(self, sandbox_id: str) -> bool:
        return True

    def resume_microvm(self, sandbox_id: str) -> bool:
        return True

    def create_auth_token(self, sandbox_id: str, ports: Sequence[PortSpec]) -> str:
        del ports
        self.tokens.append(sandbox_id)
        return "jwe-never-logged"


class FailingRawClient:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def run_microvm(self, **request: Any) -> dict[str, Any]:
        del request
        raise self._exc

    def get_microvm_image_version(self, **params: Any) -> dict[str, Any]:
        del params
        raise self._exc


def throttling() -> ClientError:
    return ClientError(
        {
            "Error": {"Code": "ThrottlingException", "Message": "slow down"},
            "retryAfterSeconds": 3,
        },
        "RunMicrovm",
    )


def context(
    plane: FakeControlPlane, clock: FakeClock, outcomes: Sequence[Any]
) -> bench.RunContext:
    def factory(host: str, jwe: str, transport: Any) -> bench.HealthProbe:
        del host, jwe, transport
        return probe(clock, outcomes)[0]

    return bench.RunContext(
        plane=plane,  # type: ignore[arg-type]
        raw_client=FailingRawClient(throttling()),
        registry=bench.VmRegistry(plane),  # type: ignore[arg-type]
        full_arn=IMAGE_ARN,
        slim_arn=None,
        execution_role_arn=None,
        probe_factory=factory,
    )


def sample(mode: str = "sdk") -> bench.LaunchSample:
    return bench.LaunchSample(phase="a", variant="full", mode=mode, batch=0, index=0)


# ------------------------------------------------------------- HealthProbe


@pytest.mark.parametrize(
    "code", [grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.DEADLINE_EXCEEDED]
)
def test_poll_not_yet_reachable_sleeps_100ms(code: grpc.StatusCode) -> None:
    clock = FakeClock()
    p, _ = probe(clock, [FakeRpcError(code)])
    assert p.poll() is None
    assert (p.polls, p.throttled_polls) == (1, 0)
    assert clock.sleeps == [bench.POLL_INTERVAL_SECONDS]


def test_poll_resource_exhausted_counts_and_backs_off_500ms() -> None:
    clock = FakeClock()
    p, _ = probe(clock, [FakeRpcError(grpc.StatusCode.RESOURCE_EXHAUSTED)])
    assert p.poll() is None
    assert (p.polls, p.throttled_polls) == (1, 1)
    assert clock.sleeps == [bench.THROTTLED_POLL_INTERVAL_SECONDS]


def test_poll_other_status_is_a_bench_error() -> None:
    clock = FakeClock()
    p, _ = probe(clock, [FakeRpcError(grpc.StatusCode.PERMISSION_DENIED)])
    with pytest.raises(bench.BenchError, match="PERMISSION_DENIED"):
        p.poll()


def test_poll_records_the_gap_since_the_previous_poll_returned() -> None:
    clock = FakeClock()
    p, _ = probe(
        clock,
        [FakeRpcError(grpc.StatusCode.UNAVAILABLE), health(agent=True, kernel=True)],
    )
    p.poll()
    assert p.last_gap_s == pytest.approx(RPC_SECONDS)
    p.poll()
    assert p.last_gap_s == pytest.approx(bench.POLL_INTERVAL_SECONDS + RPC_SECONDS)


def test_wait_ready_times_agent_and_kernel_separately_with_gaps() -> None:
    clock = FakeClock()
    t0 = clock.now
    p, stub = probe(
        clock,
        [
            FakeRpcError(grpc.StatusCode.UNAVAILABLE),
            FakeRpcError(grpc.StatusCode.UNAVAILABLE),
            health(agent=True, kernel=False),
            health(agent=True, kernel=False),
            health(agent=True, kernel=True, generation=0),
        ],
    )
    ready = p.wait_ready(t0)
    step = RPC_SECONDS + bench.POLL_INTERVAL_SECONDS
    assert ready.agent_ready_s == pytest.approx(
        3 * RPC_SECONDS + 2 * bench.POLL_INTERVAL_SECONDS
    )
    assert ready.kernel_ready_s == pytest.approx(ready.agent_ready_s + 2 * step)
    assert ready.agent_ready_gap_s == pytest.approx(step)
    assert ready.kernel_ready_gap_s == pytest.approx(step)
    assert ready.agent_uptime_ms_at_ready == 23_000
    assert ready.agent_version == "0.1.0"
    assert (p.polls, stub.calls) == (5, 5)


def test_wait_ready_gives_up_after_120s_with_not_ready_error() -> None:
    clock = FakeClock()
    t0 = clock.now
    p, _ = probe(clock, [FakeRpcError(grpc.StatusCode.DEADLINE_EXCEEDED)])
    with pytest.raises(bench.NotReadyError, match="120 s"):
        p.wait_ready(t0)
    assert clock.now - t0 >= bench.READY_TIMEOUT_SECONDS
    expected_polls = bench.READY_TIMEOUT_SECONDS / (
        RPC_SECONDS + bench.POLL_INTERVAL_SECONDS
    )
    assert p.polls == pytest.approx(expected_polls, abs=2)


def test_wait_generation_above_returns_the_new_generation_and_its_gap() -> None:
    clock = FakeClock()
    p, _ = probe(
        clock,
        [
            health(agent=True, kernel=True, generation=1),
            FakeRpcError(grpc.StatusCode.UNAVAILABLE),
            health(agent=True, kernel=False, generation=2),
            health(agent=True, kernel=True, generation=2),
        ],
    )
    resumed = p.wait_generation_above(1)
    assert resumed.generation == 2
    assert resumed.gap_s == pytest.approx(RPC_SECONDS + bench.POLL_INTERVAL_SECONDS)
    assert p.last_generation == 2


# ----------------------------------------------------------------- boot_vm


def test_boot_vm_not_ready_records_state_and_terminates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    monkeypatch.setattr(bench.time, "perf_counter", clock)
    plane = FakeControlPlane(launch_state="PENDING")
    ctx = context(plane, clock, [FakeRpcError(grpc.StatusCode.UNAVAILABLE)])
    record = sample()
    assert bench.boot_vm(ctx, record) is None
    assert record.error == "not_ready"
    assert (record.state, record.state_reason) == ("PENDING", "boot")
    assert record.microvm_id == "microvm-0"
    assert record.health_polls > 100
    assert record.token_s is not None and record.vm_alive_s is not None
    assert plane.fetched == ["microvm-0"]
    assert plane.terminated == ["microvm-0"]
    assert ctx.registry.unterminated() == []


def test_throttled_raw_launch_mints_no_token_and_runs_no_probe() -> None:
    clock = FakeClock()
    plane = FakeControlPlane()
    ctx = context(plane, clock, [health(agent=True, kernel=True)])
    record = sample(mode="raw")
    assert bench.boot_vm(ctx, record) is None
    assert record.error == "ThrottlingException"
    assert record.retry_after == 3.0
    assert record.api_s is not None
    assert record.microvm_id is None
    assert plane.tokens == [] and plane.launched == []
    assert ctx.registry.launched() == 0
    batch = bench.batch_record(bench.Batch("raw", 1), "t", [bench.asdict(record)])
    assert (batch["throttled"], batch["failed"]) == (1, 1)


# ---------------------------------------------------------------- registry


def test_enforce_budget_flags_over_budget_and_terminates() -> None:
    plane = FakeControlPlane()
    registry = bench.VmRegistry(plane)  # type: ignore[arg-type]
    old, young = sample(), sample()
    registry.track("old", 0.0, old)
    registry.track("young", 200.0, young)
    assert registry.enforce_budget(now=301.0) == ["old"]
    assert old.over_budget is True and young.over_budget is False
    assert plane.terminated == ["old"]
    assert registry.unterminated() == ["young"]


def test_terminate_failure_keeps_the_vm_pending_and_sweep_retries() -> None:
    plane = FakeControlPlane(terminate_failures={"stuck"})
    registry = bench.VmRegistry(plane)  # type: ignore[arg-type]
    for vm_id in ("first", "stuck", "last"):
        registry.track(vm_id, 0.0, sample())
    assert registry.sweep() == ["first", "stuck", "last"]
    assert plane.terminated == ["first", "last"]
    assert registry.unterminated() == ["stuck"]
    plane.terminate_failures.clear()
    assert registry.sweep() == ["stuck"]
    assert plane.terminated == ["first", "last", "stuck"]
    assert registry.unterminated() == []


def test_terminate_swallows_sandbox_exceptions_too() -> None:
    plane = FakeControlPlane(
        terminate_failures={"vm"}, fail_terminate_with=SandboxException
    )
    registry = bench.VmRegistry(plane)  # type: ignore[arg-type]
    registry.track("vm", 0.0, sample())
    assert registry.terminate("vm") is False
    assert registry.terminate("unknown") is False
    assert registry.unterminated() == ["vm"]


# ------------------------------------------------------------- exit path


def run_args(tmp_path: Path) -> argparse.Namespace:
    return bench.parse_args(["--template", IMAGE_ARN, "--out", str(tmp_path)])


def exit_context(
    monkeypatch: pytest.MonkeyPatch, plane: FakeControlPlane, failure: BaseException
) -> bench.RunContext:
    clock = FakeClock()
    ctx = context(plane, clock, [health(agent=True, kernel=True)])
    monkeypatch.setattr(bench, "build_context", lambda args: ctx)

    def phases_then_fail(ctx: bench.RunContext, plan: bench.Plan) -> dict[str, Any]:
        del plan
        for index in range(2):
            ctx.registry.track(f"microvm-{index}", clock(), sample())
        raise failure

    monkeypatch.setattr(bench, "execute_phases", phases_then_fail)
    return ctx


def written_run(tmp_path: Path) -> dict[str, Any]:
    files = list(tmp_path.glob("2026-09-cold-start-*.json"))
    assert len(files) == 1
    return json.loads(files[0].read_text(encoding="utf-8"))


def test_ctrl_c_sweeps_and_writes_aborted_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plane = FakeControlPlane()
    ctx = exit_context(monkeypatch, plane, KeyboardInterrupt())
    args = run_args(tmp_path)
    assert bench.run_benchmark(args, bench.plan_from_args(args)) == 130
    run = written_run(tmp_path)
    assert run["meta"]["aborted"] is True
    assert run["phases"] == {}
    assert sorted(plane.terminated) == ["microvm-0", "microvm-1"]
    assert ctx.registry.unterminated() == []
    assert "### cost" in capsys.readouterr().out


def test_any_other_exception_still_writes_aborted_json_and_propagates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    plane = FakeControlPlane(terminate_failures={"microvm-1"})
    ctx = exit_context(monkeypatch, plane, RuntimeError("boom"))
    args = run_args(tmp_path)
    with pytest.raises(RuntimeError, match="boom"):
        bench.run_benchmark(args, bench.plan_from_args(args))
    run = written_run(tmp_path)
    assert run["meta"]["aborted"] is True
    assert plane.terminated == ["microvm-0"]
    assert ctx.registry.unterminated() == ["microvm-1"]


def test_image_block_failure_is_recorded_not_raised() -> None:
    plane = FakeControlPlane()
    ctx = context(plane, FakeClock(), [])
    block = bench.image_block(ctx, IMAGE_ARN, "10.0")
    assert block == {"arn": IMAGE_ARN, "version": "10.0", "error": "ClientError"}
    assert bench.image_block(ctx, IMAGE_ARN, None) == {
        "arn": IMAGE_ARN,
        "version": None,
    }


def test_probe_transport_spaces_reconnects_at_100ms_but_keeps_the_connect_timeout() -> (
    None
):
    options = dict(bench.probe_transport().options)
    assert options["grpc.initial_reconnect_backoff_ms"] == 100
    assert options["grpc.max_reconnect_backoff_ms"] == 100
    assert options["grpc.min_reconnect_backoff_ms"] == 500
    assert options["grpc.keepalive_time_ms"] == 30_000
    assert bench.HEALTH_RPC_TIMEOUT_SECONDS == 0.5
