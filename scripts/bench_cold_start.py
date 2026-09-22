"""Cold-start benchmark of Lambda MicroVMs through the Rayito SDK (M6, Track D).

One run, one JSON file (``docs/benchmarks/raw/2026-09-cold-start-<runid>.json``,
schema ``rayito.bench.cold-start/1``) with every number the pool decision of
``openspec/changes/m6-benchmark-pool/design.md`` needs:

- phase a: ``--sequential`` launches of the full image, one at a time, timing
  ``run-microvm`` -> ``Health.agent_ready`` -> ``Health.kernel_ready`` with a
  bench-owned 100 ms poll (100 ms reconnect backoff, 0.5 s per RPC; every
  readiness point carries the gap since the previous poll returned, i.e. its
  measured resolution), then the first ``1+1`` cell and one pandas +
  matplotlib "stack" cell;
- phase b: concurrent bursts (``--bursts`` x ``--burst-modes``) through the
  SDK control plane (5 TPS ``RunMicrovm`` bucket) and through a raw one-attempt
  boto3 client, to see whether AWS throttles or slows down above 5 TPS;
- phase c: ``--resume-cycles`` explicit ``pause()``/``resume()`` cycles on one
  VM and ``pause()``/auto-resume cycles on another, concurrently;
- phase e: phase a again against ``--template-slim`` (kernel warm-up disabled)
  to correlate ``memorySnapshotSizeInBytes`` with seconds.

Guardrails: every VM is launched with ``maximumDurationInSeconds=600``, lives
under 300 s (watchdog at phase boundaries and exit), is terminated by the run
and swept on exit or Ctrl-C; the run refuses to start over ``--budget-usd``
(estimate printed by ``--dry-run``) or with more than 10 live VMs of either
image. Nothing here prints a JWE, an access token or a ``runHookPayload``.

Every AWS call is one already listed in ``AWS_API_NOTES.md`` (§2, §3, §4, §5,
§6). Run it from the SDK environment::

    cd clients/python && uv run python ../../scripts/bench_cold_start.py \\
        --template <arn|name> [--template-slim <arn|name>] [--dry-run] ...

``--report <json>`` renders the Markdown tables of an existing file without
touching AWS.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import platform
import statistics
import sys
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import boto3
import grpc
from botocore.config import Config
from botocore.exceptions import ClientError
from rayito import IdlePolicy, Sandbox
from rayito import __version__ as sdk_version
from rayito._aws import (
    LambdaMicrovmsControlPlane,
    LaunchRequest,
    PortSpec,
    sandbox_info_from_response,
)
from rayito._models import SandboxInfo
from rayito._sandbox_base import LaunchPlan, build_launch_plan
from rayito._transport import (
    CHANNEL_OPTIONS,
    ProxyAuthPlugin,
    ProxyToken,
    TokenStore,
    TransportSettings,
    is_not_yet_reachable,
    rpc_status,
)
from rayito.exceptions import SandboxException
from rayito.v1 import health_pb2, health_pb2_grpc

SCHEMA = "rayito.bench.cold-start/1"
DEFAULT_PORT = 8080
VM_TIMEOUT_SECONDS = 600
VM_WALL_BUDGET_SECONDS = 300.0
READY_TIMEOUT_SECONDS = 120.0
RESUME_TIMEOUT_SECONDS = 60.0
POLL_INTERVAL_SECONDS = 0.1
THROTTLED_POLL_INTERVAL_SECONDS = 0.5
HEALTH_RPC_TIMEOUT_SECONDS = 0.5
PROBE_RECONNECT_BACKOFF_MS = 100
PROBE_RECONNECT_BACKOFF_OPTIONS = (
    "grpc.initial_reconnect_backoff_ms",
    "grpc.max_reconnect_backoff_ms",
)
PROBE_CHANNEL_OPTIONS: tuple[tuple[str, int | str], ...] = tuple(
    (
        name,
        PROBE_RECONNECT_BACKOFF_MS
        if name in PROBE_RECONNECT_BACKOFF_OPTIONS
        else value,
    )
    for name, value in CHANNEL_OPTIONS
)
RTT_SAMPLES = 5
BATCH_GAP_SECONDS = 30.0
DWELL_SECONDS = 5.0
CELL_TIMEOUT_SECONDS = 60.0
MAX_LIVE_VMS = 10
PHASES = ("a", "b", "c", "e")
BURST_MODES = ("sdk", "raw")
INGRESS = ("ALL_INGRESS",)
AUTO_IDLE_POLICY = IdlePolicy(
    max_idle_seconds=60, suspended_duration_seconds=600, auto_resume=True
)

FIRST_CELL = "1+1"
FIRST_CELL_EXPECTED = "2"
STATE_CELL = "x = 42"
RESUME_CELL = "x"
RESUME_CELL_EXPECTED = "42"
STACK_CELL = (
    "import io; import numpy as np; import pandas as pd; "
    "import matplotlib.pyplot as plt; "
    'df = pd.DataFrame({"a": [1.0, 2.0]}); fig, ax = plt.subplots(); '
    'ax.plot(df["a"]); buf = io.BytesIO(); fig.savefig(buf, format="png"); '
    "plt.close(fig); len(buf.getvalue())"
)
AUTO_RESUME_COMMAND = "echo back"
AUTO_RESUME_EXPECTED = "back"

PRICE_VCPU_SECOND = 0.0000276944
PRICE_GB_SECOND = 0.0000036667
PRICE_SNAPSHOT_READ_GB = 0.00155
PRICE_SNAPSHOT_WRITE_GB = 0.0038
PRICE_STORAGE_GB_MONTH = 0.08
VCPU_PER_VM = 1.0
GB_PER_VM = 2.0
ESTIMATE_MEMORY_SNAPSHOT_GB = 0.92
ESTIMATE_SLIM_STORAGE_GB = 2.0
ESTIMATE_STORAGE_DAYS = 7
LAUNCH_SUMMARY_FIELDS = (
    "api_s",
    "token_s",
    "agent_ready_s",
    "agent_ready_gap_s",
    "kernel_ready_s",
    "kernel_ready_gap_s",
    "kernel_ready_from_batch_s",
    "agent_uptime_ms_at_ready",
    "health_polls",
    "throttled_polls",
    "first_cell_s",
    "stack_cell_s",
    "vm_alive_s",
)
CYCLE_SUMMARY_FIELDS = (
    "pause_s",
    "dwell_s",
    "resume_api_s",
    "resume_s",
    "resume_gap_s",
    "auto_resume_s",
    "first_cell_after_resume_s",
    "resume_generation",
)
IMAGE_SIZE_KEYS = (
    "memorySnapshotSizeInBytes",
    "codeInstallSizeInBytes",
    "diskSnapshotSizeInBytes",
)

log = logging.getLogger("bench")


# ----------------------------------------------------------------- records


@dataclass
class LaunchSample:
    phase: str
    variant: str
    mode: str
    batch: int
    index: int
    microvm_id: str | None = None
    image_version: str | None = None
    started_at: str = ""
    api_s: float | None = None
    token_s: float | None = None
    agent_ready_s: float | None = None
    agent_ready_gap_s: float | None = None
    kernel_ready_s: float | None = None
    kernel_ready_gap_s: float | None = None
    kernel_ready_from_batch_s: float | None = None
    agent_uptime_ms_at_ready: int | None = None
    health_polls: int = 0
    throttled_polls: int = 0
    first_cell_s: float | None = None
    stack_cell_s: float | None = None
    vm_alive_s: float | None = None
    error: str | None = None
    retry_after: float | None = None
    state: str | None = None
    state_reason: str | None = None
    over_budget: bool = False


@dataclass
class CycleSample:
    vm: str
    cycle: int
    pause_s: float | None = None
    dwell_s: float = DWELL_SECONDS
    resume_api_s: float | None = None
    resume_s: float | None = None
    resume_gap_s: float | None = None
    auto_resume_s: float | None = None
    first_cell_after_resume_s: float | None = None
    resume_generation: int | None = None
    kernel_alive: bool | None = None
    error: str | None = None


@dataclass
class ReadyTimings:
    """``*_gap_s`` is the time between the return of the poll before the one
    that first showed the flag and the return of that poll: the window inside
    which the transition happened (design D4, amendment 10)."""

    agent_ready_s: float
    agent_ready_gap_s: float
    kernel_ready_s: float
    kernel_ready_gap_s: float
    agent_uptime_ms_at_ready: int
    agent_version: str
    resume_generation: int


@dataclass
class GenerationTimings:
    generation: int
    gap_s: float


@dataclass(frozen=True)
class AgentReadyPoint:
    at_s: float
    gap_s: float
    uptime_ms: int


@dataclass(frozen=True)
class Batch:
    mode: str
    size: int


@dataclass(frozen=True)
class Plan:
    phases: tuple[str, ...]
    sequential: int
    batches: tuple[Batch, ...]
    resume_cycles: int
    slim: bool

    @property
    def burst_launches(self) -> int:
        return sum(batch.size for batch in self.batches) if "b" in self.phases else 0

    @property
    def launches(self) -> int:
        count = self.burst_launches
        if "a" in self.phases:
            count += self.sequential
        if "c" in self.phases:
            count += 2
        if "e" in self.phases and self.slim:
            count += self.sequential
        return count

    @property
    def cycles(self) -> int:
        return 2 * self.resume_cycles if "c" in self.phases else 0


class BenchError(Exception):
    """A bench-level failure that ends one sample, never the run."""


class NotReadyError(BenchError):
    pass


# ---------------------------------------------------------------- helpers


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def run_id_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def percentile(values: Sequence[float], fraction: float) -> float:
    """Nearest-rank percentile: ``sorted[ceil(fraction * n) - 1]``."""
    if not values:
        raise ValueError("percentile of an empty sequence")
    ordered = sorted(values)
    rank = max(1, math.ceil(fraction * len(ordered)))
    return ordered[rank - 1]


def summary_fields(samples: Sequence[dict[str, Any]]) -> tuple[str, ...]:
    """Cycle records carry ``cycle``; everything else is a launch sample."""
    if not samples:
        return ()
    return CYCLE_SUMMARY_FIELDS if "cycle" in samples[0] else LAUNCH_SUMMARY_FIELDS


def summarize(samples: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """``{metric: {n, p50, p95, min, max}}`` over samples without ``error``;
    a metric nobody recorded summarises to ``n = 0`` and nulls."""
    clean = [sample for sample in samples if not sample.get("error")]
    summary: dict[str, dict[str, Any]] = {}
    for name in summary_fields(samples):
        values = [
            float(sample[name])
            for sample in clean
            if isinstance(sample.get(name), int | float)
            and not isinstance(sample.get(name), bool)
        ]
        summary[name] = metric_summary(values)
    return summary


def metric_summary(values: Sequence[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0, "p50": None, "p95": None, "min": None, "max": None}
    return {
        "n": len(values),
        "p50": round(statistics.median(values), 4),
        "p95": round(percentile(values, 0.95), 4),
        "min": round(min(values), 4),
        "max": round(max(values), 4),
    }


def estimate_cost(
    launches: int, cycles: int, memory_snapshot_gb: float = ESTIMATE_MEMORY_SNAPSHOT_GB
) -> float:
    """Design D8: §12 unit prices, a pessimistic 300 s life per VM, one
    snapshot read per launch, one write plus one read per cycle, and a week
    of storage for the slim image version."""
    per_launch = (
        memory_snapshot_gb * PRICE_SNAPSHOT_READ_GB
        + VM_WALL_BUDGET_SECONDS
        * (VCPU_PER_VM * PRICE_VCPU_SECOND + GB_PER_VM * PRICE_GB_SECOND)
    )
    per_cycle = memory_snapshot_gb * (PRICE_SNAPSHOT_WRITE_GB + PRICE_SNAPSHOT_READ_GB)
    storage = (
        ESTIMATE_SLIM_STORAGE_GB * PRICE_STORAGE_GB_MONTH * ESTIMATE_STORAGE_DAYS / 30
    )
    return launches * per_launch + cycles * per_cycle + storage


def select_over_budget(
    ages: dict[str, float], budget_seconds: float = VM_WALL_BUDGET_SECONDS
) -> list[str]:
    """The tracked VMs (id -> age in seconds) the watchdog terminates."""
    return [vm_id for vm_id, age in ages.items() if age > budget_seconds]


def launch_error_name(exc: BaseException) -> tuple[str, float | None]:
    """The AWS exception name (``ThrottlingException`` ...) and its
    ``retryAfterSeconds`` for a failed ``run-microvm``, whichever client
    raised it; other errors report their type name."""
    if isinstance(exc, ClientError):
        code = str(exc.response.get("Error", {}).get("Code") or type(exc).__name__)
        retry_after = exc.response.get("retryAfterSeconds")
        return code, float(retry_after) if retry_after is not None else None
    aws_code = getattr(exc, "aws_code", None)
    retry_after = getattr(exc, "retry_after", None)
    return str(aws_code or type(exc).__name__), retry_after


def quiet_loggers() -> None:
    for name in ("boto3", "botocore", "urllib3", "grpc", "rayito"):
        logging.getLogger(name).setLevel(logging.WARNING)


# ------------------------------------------------------------- health probe


Clock = Callable[[], float]
Sleeper = Callable[[float], None]


def probe_transport() -> TransportSettings:
    """The SDK channel options with the reconnect spacing (``initial`` and
    ``max`` backoff) at 100 ms, so a refused connection costs one poll
    interval instead of the SDK's 0.5-2 s (design D4, amendment 10).
    ``grpc.min_reconnect_backoff_ms`` stays at the SDK's 500 ms: grpc-core
    also uses it as the connect timeout, and at 100 ms no TLS handshake
    completes through the proxy (measured 2026-09-16: every launch
    ``not_ready``)."""
    return TransportSettings(options=PROBE_CHANNEL_OPTIONS)


class HealthProbe:
    """Fixed 100 ms ``Health`` poll on a bench-owned channel (design D4).

    ``UNAVAILABLE``/``DEADLINE_EXCEEDED`` mean "not yet", ``RESOURCE_EXHAUSTED``
    (proxy 429) is counted and followed by a 500 ms sleep, anything else
    ends the sample. Every poll records when it returned, so a readiness
    point carries the gap since the previous poll returned: the measured
    resolution of that number. The channel never sends ``x-access-token``.
    """

    def __init__(
        self,
        host: str,
        jwe: str,
        transport: TransportSettings | None = None,
        *,
        stub: Any | None = None,
        clock: Clock = time.perf_counter,
        sleep: Sleeper = time.sleep,
    ):
        self._clock = clock
        self._sleep = sleep
        self._channel: grpc.Channel | None = None
        if stub is None:
            self._channel = (transport or probe_transport()).open_channel(
                host, self._auth_plugin(jwe)
            )
            stub = health_pb2_grpc.HealthServiceStub(self._channel)
        self._stub = stub
        self.polls = 0
        self.throttled_polls = 0
        self.last_generation = 0
        self.last_return_s: float | None = None
        self.last_gap_s: float = 0.0

    @staticmethod
    def _auth_plugin(jwe: str) -> ProxyAuthPlugin:
        store = TokenStore()
        store.put(
            ProxyToken(
                jwe=jwe, ports=(PortSpec.single(DEFAULT_PORT),), minted_at=time.time()
            )
        )
        return ProxyAuthPlugin(store, port=DEFAULT_PORT, access_token=None)

    def close(self) -> None:
        if self._channel is not None:
            self._channel.close()

    def poll(self) -> health_pb2.HealthResponse | None:
        self.polls += 1
        sent = self._clock()
        previous = sent if self.last_return_s is None else self.last_return_s
        try:
            response = self._stub.Health(
                health_pb2.HealthRequest(), timeout=HEALTH_RPC_TIMEOUT_SECONDS
            )
        except grpc.RpcError as exc:
            self._note_return(previous)
            self._back_off(exc)
            return None
        self._note_return(previous)
        self.last_generation = int(response.resume_generation)
        return response

    def _note_return(self, previous: float) -> None:
        self.last_return_s = self._clock()
        self.last_gap_s = self.last_return_s - previous

    def _back_off(self, exc: grpc.RpcError) -> None:
        if is_not_yet_reachable(exc):
            self._sleep(POLL_INTERVAL_SECONDS)
            return
        if rpc_status(exc) is grpc.StatusCode.RESOURCE_EXHAUSTED:
            self.throttled_polls += 1
            self._sleep(THROTTLED_POLL_INTERVAL_SECONDS)
            return
        raise BenchError(f"Health {rpc_status(exc)}") from exc

    def wait_ready(
        self, t0: float, timeout: float = READY_TIMEOUT_SECONDS
    ) -> ReadyTimings:
        agent: AgentReadyPoint | None = None
        while self._clock() - t0 < timeout:
            response = self.poll()
            if response is None:
                continue
            returned = self._returned_at() - t0
            if agent is None and response.agent_ready:
                agent = AgentReadyPoint(
                    at_s=returned,
                    gap_s=self.last_gap_s,
                    uptime_ms=int(response.uptime_ms),
                )
            if agent is not None and response.kernel_ready:
                return ReadyTimings(
                    agent_ready_s=agent.at_s,
                    agent_ready_gap_s=agent.gap_s,
                    kernel_ready_s=returned,
                    kernel_ready_gap_s=self.last_gap_s,
                    agent_uptime_ms_at_ready=agent.uptime_ms,
                    agent_version=str(response.agent_version),
                    resume_generation=int(response.resume_generation),
                )
            self._sleep(POLL_INTERVAL_SECONDS)
        raise NotReadyError(f"not ready after {timeout:g} s")

    def _returned_at(self) -> float:
        return self._clock() if self.last_return_s is None else self.last_return_s

    def wait_generation_above(
        self, seen: int, timeout: float = RESUME_TIMEOUT_SECONDS
    ) -> GenerationTimings:
        started = self._clock()
        while self._clock() - started < timeout:
            response = self.poll()
            if response is None:
                continue
            generation = int(response.resume_generation)
            if generation > seen and response.agent_ready and response.kernel_ready:
                return GenerationTimings(generation=generation, gap_s=self.last_gap_s)
            self._sleep(POLL_INTERVAL_SECONDS)
        raise NotReadyError(f"no generation above {seen} after {timeout:g} s")

    def rtt_ms(self) -> float:
        samples: list[float] = []
        for _ in range(RTT_SAMPLES):
            started = self._clock()
            self._stub.Health(
                health_pb2.HealthRequest(), timeout=HEALTH_RPC_TIMEOUT_SECONDS * 10
            )
            samples.append((self._clock() - started) * 1000.0)
        return round(statistics.median(samples), 1)


# ---------------------------------------------------------------- registry


@dataclass
class TrackedVm:
    t0: float
    sample: LaunchSample
    terminated: bool = False
    terminating: bool = False
    terminate_failures: int = 0


class VmRegistry:
    """Every VM the run launched, for the watchdog and the exit sweep.

    A VM counts as terminated only once ``terminate-microvm`` returned; a
    failed call (any exception, logged, never raised) leaves it pending so
    the watchdog and the sweep retry it, and ``unterminated()`` names what
    the operator must finish by hand.
    """

    def __init__(self, plane: LambdaMicrovmsControlPlane) -> None:
        self._plane = plane
        self._lock = threading.Lock()
        self._vms: dict[str, TrackedVm] = {}

    def track(self, vm_id: str, t0: float, sample: LaunchSample) -> None:
        with self._lock:
            self._vms[vm_id] = TrackedVm(t0=t0, sample=sample)

    def terminate(self, vm_id: str) -> bool:
        if not self._claim(vm_id):
            return False
        try:
            self._plane.terminate_microvm(vm_id)
        except Exception as exc:  # noqa: BLE001 - retried by the sweep, never raised
            self._release(vm_id, terminated=False)
            log.warning("terminate-microvm %s failed: %s", vm_id, type(exc).__name__)
            return False
        self._release(vm_id, terminated=True)
        return True

    def _claim(self, vm_id: str) -> bool:
        with self._lock:
            tracked = self._vms.get(vm_id)
            if tracked is None or tracked.terminated or tracked.terminating:
                return False
            tracked.terminating = True
            return True

    def _release(self, vm_id: str, *, terminated: bool) -> None:
        with self._lock:
            tracked = self._vms[vm_id]
            tracked.terminating = False
            tracked.terminated = terminated
            if not terminated:
                tracked.terminate_failures += 1

    def ages(self, now: float | None = None) -> dict[str, float]:
        current = time.perf_counter() if now is None else now
        with self._lock:
            return {
                vm_id: current - tracked.t0
                for vm_id, tracked in self._vms.items()
                if not tracked.terminated
            }

    def enforce_budget(self, now: float | None = None) -> list[str]:
        culprits = select_over_budget(self.ages(now))
        for vm_id in culprits:
            log.warning(
                "watchdog: %s older than %g s, terminating",
                vm_id,
                VM_WALL_BUDGET_SECONDS,
            )
            with self._lock:
                self._vms[vm_id].sample.over_budget = True
            self.terminate(vm_id)
        return culprits

    def sweep(self) -> list[str]:
        pending = self.unterminated()
        for vm_id in pending:
            self.terminate(vm_id)
        return pending

    def unterminated(self) -> list[str]:
        with self._lock:
            return [
                vm_id for vm_id, tracked in self._vms.items() if not tracked.terminated
            ]

    def launched(self) -> int:
        with self._lock:
            return len(self._vms)


# ---------------------------------------------------------------- context


Launcher = Callable[[LaunchRequest], SandboxInfo]
ProbeFactory = Callable[[str, str, TransportSettings], HealthProbe]


@dataclass
class RunContext:
    plane: LambdaMicrovmsControlPlane
    raw_client: Any
    registry: VmRegistry
    full_arn: str
    slim_arn: str | None
    execution_role_arn: str | None
    transport: TransportSettings = field(default_factory=probe_transport)
    probe_factory: ProbeFactory = HealthProbe
    client_rtt_ms: float | None = None
    rayd_version: str | None = None
    first_ready_lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def logging_option(self) -> str:
        return "cloudwatch" if self.execution_role_arn else "disabled"

    def image_arn(self, variant: str) -> str:
        if variant == "slim":
            if self.slim_arn is None:
                raise BenchError("--template-slim was not given")
            return self.slim_arn
        return self.full_arn

    def sdk_launch(self, request: LaunchRequest) -> SandboxInfo:
        return self.plane.run_microvm(request)

    def raw_launch(self, request: LaunchRequest) -> SandboxInfo:
        return sandbox_info_from_response(
            self.raw_client.run_microvm(**request.to_api())
        )

    def launcher(self, mode: str) -> Launcher:
        return self.raw_launch if mode == "raw" else self.sdk_launch

    def note_first_ready(self, probe: HealthProbe, timings: ReadyTimings) -> None:
        with self.first_ready_lock:
            if self.client_rtt_ms is not None:
                return
            self.rayd_version = timings.agent_version
            self.client_rtt_ms = probe.rtt_ms()


def raw_lambda_client(region: str) -> Any:
    """One attempt, no bucket: what AWS does above 5 TPS is the data."""
    config = Config(
        retries={"mode": "standard", "total_max_attempts": 1},
        connect_timeout=5,
        read_timeout=60,
        user_agent_extra="rayito-bench",
    )
    return boto3.session.Session(region_name=region).client(
        "lambda-microvms", config=config
    )


# ----------------------------------------------------------------- launch


@dataclass
class LiveVm:
    info: SandboxInfo
    plan: LaunchPlan
    probe: HealthProbe
    sample: LaunchSample
    sandbox: Sandbox
    t0: float
    ready: ReadyTimings


def launch_plan_for(
    ctx: RunContext, variant: str, idle: IdlePolicy | None
) -> LaunchPlan:
    return build_launch_plan(
        image_arn=ctx.image_arn(variant),
        region=ctx.plane.region,
        template_version=None,
        timeout=VM_TIMEOUT_SECONDS,
        idle=idle,
        envs=None,
        execution_role_arn=ctx.execution_role_arn,
        allowed_ports=None,
        ingress=list(INGRESS),
        egress=None,
        logging=ctx.logging_option,
        access_token=None,
    )


def boot_vm(
    ctx: RunContext,
    sample: LaunchSample,
    *,
    idle: IdlePolicy | None = None,
    batch_t0: float | None = None,
) -> LiveVm | None:
    """Design D2 steps 1-5: ``run-microvm`` -> token -> ``Health`` probe ->
    ``Sandbox.connect``. ``None`` (with ``sample.error`` set) when no usable VM
    came out of it; the VM, if any, is already terminated in that case."""
    plan = launch_plan_for(ctx, sample.variant, idle)
    sample.started_at = utc_now()
    t0 = time.perf_counter()
    try:
        info = ctx.launcher(sample.mode)(plan.request)
    except Exception as exc:  # noqa: BLE001 - any failed run-microvm is one sample
        sample.error, sample.retry_after = launch_error_name(exc)
        sample.api_s = round(time.perf_counter() - t0, 4)
        return None
    sample.api_s = round(time.perf_counter() - t0, 4)
    sample.microvm_id = info.sandbox_id
    sample.image_version = info.template_version
    ctx.registry.track(info.sandbox_id, t0, sample)
    probe: HealthProbe | None = None
    try:
        probe = open_probe(ctx, sample, info, plan)
        ready = probe.wait_ready(t0)
        record_ready(sample, probe, ready, batch_t0)
        ctx.note_first_ready(probe, ready)
        sandbox = Sandbox.connect(
            info.sandbox_id, access_token=plan.access_token, control_plane=ctx.plane
        )
    except NotReadyError:
        record_not_ready(ctx, sample, info.sandbox_id, probe)
        finish_failed(ctx, sample, info.sandbox_id, t0, probe)
        return None
    except Exception as exc:  # noqa: BLE001 - the VM is terminated either way
        sample.error = type(exc).__name__
        finish_failed(ctx, sample, info.sandbox_id, t0, probe)
        return None
    return LiveVm(
        info=info,
        plan=plan,
        probe=probe,
        sample=sample,
        sandbox=sandbox,
        t0=t0,
        ready=ready,
    )


def open_probe(
    ctx: RunContext, sample: LaunchSample, info: SandboxInfo, plan: LaunchPlan
) -> HealthProbe:
    """Design D2 steps 3-4: one token for port 8080 (``token_s`` is the call's
    own duration) and the bench-owned channel."""
    token_started = time.perf_counter()
    jwe = ctx.plane.create_auth_token(info.sandbox_id, plan.proxy_ports)
    sample.token_s = round(time.perf_counter() - token_started, 4)
    return ctx.probe_factory(info.endpoint, jwe, ctx.transport)


def record_ready(
    sample: LaunchSample,
    probe: HealthProbe,
    ready: ReadyTimings,
    batch_t0: float | None,
) -> None:
    sample.agent_ready_s = round(ready.agent_ready_s, 4)
    sample.agent_ready_gap_s = round(ready.agent_ready_gap_s, 4)
    sample.kernel_ready_s = round(ready.kernel_ready_s, 4)
    sample.kernel_ready_gap_s = round(ready.kernel_ready_gap_s, 4)
    sample.agent_uptime_ms_at_ready = ready.agent_uptime_ms_at_ready
    sample.health_polls = probe.polls
    sample.throttled_polls = probe.throttled_polls
    if batch_t0 is not None:
        sample.kernel_ready_from_batch_s = round(time.perf_counter() - batch_t0, 4)


def record_not_ready(
    ctx: RunContext, sample: LaunchSample, vm_id: str, probe: HealthProbe | None
) -> None:
    sample.error = "not_ready"
    if probe is not None:
        sample.health_polls = probe.polls
        sample.throttled_polls = probe.throttled_polls
    try:
        info = ctx.plane.get_microvm(vm_id)
        sample.state, sample.state_reason = info.state, info.state_reason
    except SandboxException as exc:
        sample.state_reason = type(exc).__name__


def finish_failed(
    ctx: RunContext,
    sample: LaunchSample,
    vm_id: str,
    t0: float,
    probe: HealthProbe | None,
) -> None:
    if probe is not None:
        probe.close()
    ctx.registry.terminate(vm_id)
    sample.vm_alive_s = round(time.perf_counter() - t0, 4)


def finish_vm(ctx: RunContext, vm: LiveVm) -> None:
    """Design D2 step 6: close the client side, ``terminate-microvm``."""
    vm.sandbox.close()
    vm.probe.close()
    ctx.registry.terminate(vm.info.sandbox_id)
    vm.sample.vm_alive_s = round(time.perf_counter() - vm.t0, 4)


def timed_cell(sandbox: Sandbox, code: str) -> tuple[float, str | None]:
    started = time.perf_counter()
    execution = sandbox.run_code(code, timeout=CELL_TIMEOUT_SECONDS)
    elapsed = time.perf_counter() - started
    if execution.error is not None:
        raise BenchError(f"cell failed: {execution.error.name}")
    return round(elapsed, 4), execution.text


def run_first_cell(vm: LiveVm) -> None:
    elapsed, text = timed_cell(vm.sandbox, FIRST_CELL)
    if text != FIRST_CELL_EXPECTED:
        raise BenchError(f"first cell returned {text!r}")
    vm.sample.first_cell_s = elapsed


def run_stack_cell(vm: LiveVm) -> None:
    elapsed, text = timed_cell(vm.sandbox, STACK_CELL)
    if not text or int(text) <= 0:
        raise BenchError(f"stack cell returned {text!r}")
    vm.sample.stack_cell_s = elapsed


def launch_and_measure(
    ctx: RunContext,
    sample: LaunchSample,
    *,
    stack: bool,
    batch_t0: float | None = None,
) -> LaunchSample:
    """One launch of phases a, b or e: boot, the cells, terminate."""
    vm = boot_vm(ctx, sample, batch_t0=batch_t0)
    if vm is None:
        log_sample(sample)
        return sample
    try:
        run_first_cell(vm)
        if stack:
            run_stack_cell(vm)
    except Exception as exc:  # noqa: BLE001 - one sample, never the run
        sample.error = f"{type(exc).__name__}: {exc}"[:200]
    finally:
        finish_vm(ctx, vm)
    log_sample(sample)
    return sample


def log_sample(sample: LaunchSample) -> None:
    log.info(
        "%s/%s/%s#%d %s api=%s agent=%s kernel=%s cell=%s stack=%s alive=%s%s",
        sample.phase,
        sample.mode,
        sample.variant,
        sample.index,
        sample.microvm_id or "-",
        sample.api_s,
        sample.agent_ready_s,
        sample.kernel_ready_s,
        sample.first_cell_s,
        sample.stack_cell_s,
        sample.vm_alive_s,
        f" error={sample.error}" if sample.error else "",
    )


# ------------------------------------------------------------------ phases


def phase_sequential(ctx: RunContext, count: int, variant: str) -> dict[str, Any]:
    phase = "a" if variant == "full" else "e"
    samples: list[dict[str, Any]] = []
    for index in range(count):
        sample = LaunchSample(
            phase=phase, variant=variant, mode="sdk", batch=0, index=index
        )
        samples.append(asdict(launch_and_measure(ctx, sample, stack=True)))
    return {"variant": variant, "samples": samples, "summary": summarize(samples)}


def phase_bursts(ctx: RunContext, batches: Sequence[Batch]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for position, batch in enumerate(batches):
        if position:
            ctx.registry.enforce_budget()
            log.info("sleeping %g s before the next batch", BATCH_GAP_SECONDS)
            time.sleep(BATCH_GAP_SECONDS)
        records.append(run_batch(ctx, batch))
    return records


def run_batch(ctx: RunContext, batch: Batch) -> dict[str, Any]:
    log.info("burst %s x%d", batch.mode, batch.size)
    samples = [
        LaunchSample(
            phase="b", variant="full", mode=batch.mode, batch=batch.size, index=index
        )
        for index in range(batch.size)
    ]
    batch_started_at = utc_now()
    batch_t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=batch.size) as pool:
        futures = [
            pool.submit(launch_and_measure, ctx, sample, stack=False, batch_t0=batch_t0)
            for sample in samples
        ]
        results = [asdict(future.result()) for future in futures]
    return batch_record(batch, batch_started_at, results)


def batch_record(
    batch: Batch, started_at: str, samples: list[dict[str, Any]]
) -> dict[str, Any]:
    ready = [
        s["kernel_ready_from_batch_s"]
        for s in samples
        if s["kernel_ready_from_batch_s"]
    ]
    return {
        "variant": "full",
        "mode": batch.mode,
        "size": batch.size,
        "batch_t0": started_at,
        "batch_wall_s": round(max(ready), 4) if ready else None,
        "throttled": sum(1 for s in samples if s["error"] == "ThrottlingException"),
        "failed": sum(1 for s in samples if s["error"]),
        "throttled_polls": sum(s["throttled_polls"] for s in samples),
        "samples": samples,
        "summary": summarize(samples),
    }


def phase_resume(ctx: RunContext, cycles: int) -> dict[str, Any]:
    with ThreadPoolExecutor(max_workers=2) as pool:
        explicit = pool.submit(resume_cycles, ctx, "explicit", cycles)
        auto = pool.submit(resume_cycles, ctx, "auto", cycles)
        return {"explicit": explicit.result(), "auto": auto.result()}


def resume_cycles(ctx: RunContext, kind: str, cycles: int) -> dict[str, Any]:
    idle = AUTO_IDLE_POLICY if kind == "auto" else None
    sample = LaunchSample(phase="c", variant="full", mode="sdk", batch=0, index=0)
    vm = boot_vm(ctx, sample, idle=idle)
    if vm is None:
        log_sample(sample)
        return {"launch": asdict(sample), "samples": [], "summary": summarize([])}
    records: list[dict[str, Any]] = []
    try:
        run_first_cell(vm)
        vm.sandbox.run_code(STATE_CELL, timeout=CELL_TIMEOUT_SECONDS)
        for cycle in range(cycles):
            records.append(asdict(run_cycle(vm, kind, cycle)))
    except Exception as exc:  # noqa: BLE001 - the VM is terminated either way
        sample.error = f"{type(exc).__name__}: {exc}"[:200]
    finally:
        finish_vm(ctx, vm)
    log_sample(sample)
    return {"launch": asdict(sample), "samples": records, "summary": summarize(records)}


def run_cycle(vm: LiveVm, kind: str, cycle: int) -> CycleSample:
    record = CycleSample(vm=kind, cycle=cycle)
    seen = vm.probe.last_generation
    try:
        pause_vm(vm, record)
        time.sleep(DWELL_SECONDS)
        if kind == "explicit":
            explicit_resume(vm, record, seen)
        else:
            auto_resume(vm, record)
        cell_after_resume(vm, record)
    except Exception as exc:  # noqa: BLE001 - one cycle, never the run
        record.error = record.error or f"{type(exc).__name__}: {exc}"[:200]
    log.info("cycle %s#%d %s", kind, cycle, asdict(record))
    return record


def pause_vm(vm: LiveVm, record: CycleSample) -> None:
    started = time.perf_counter()
    suspended = vm.sandbox.pause()
    record.pause_s = round(time.perf_counter() - started, 4)
    if not suspended:
        raise BenchError("pause() found the VM already suspended")


def require_suspended(vm: LiveVm, record: CycleSample) -> None:
    if vm.sandbox.get_info().state != "SUSPENDED":
        record.error = "not_suspended"
        raise BenchError("not_suspended")


def explicit_resume(vm: LiveVm, record: CycleSample, seen: int) -> None:
    require_suspended(vm, record)
    started = time.perf_counter()
    vm.sandbox.resume(wait=False)
    record.resume_api_s = round(time.perf_counter() - started, 4)
    resumed = vm.probe.wait_generation_above(seen)
    record.resume_s = round(time.perf_counter() - started, 4)
    record.resume_generation = resumed.generation
    record.resume_gap_s = round(resumed.gap_s, 4)
    vm.sandbox.get_health()


def auto_resume(vm: LiveVm, record: CycleSample) -> None:
    require_suspended(vm, record)
    started = time.perf_counter()
    result = vm.sandbox.commands.run(AUTO_RESUME_COMMAND, timeout=CELL_TIMEOUT_SECONDS)
    record.auto_resume_s = round(time.perf_counter() - started, 4)
    if result.stdout.strip() != AUTO_RESUME_EXPECTED:
        raise BenchError(f"auto-resume command returned {result.stdout!r}")
    record.resume_generation = vm.sandbox.get_health().resume_generation
    vm.probe.last_generation = record.resume_generation


def cell_after_resume(vm: LiveVm, record: CycleSample) -> None:
    elapsed, text = timed_cell(vm.sandbox, RESUME_CELL)
    record.first_cell_after_resume_s = elapsed
    record.kernel_alive = text == RESUME_CELL_EXPECTED


# -------------------------------------------------------------- pre-flight


def live_vms(ctx: RunContext, image_arn: str) -> list[dict[str, Any]]:
    return [
        {
            "microvm_id": item.sandbox_id,
            "state": item.state,
            "started_at": str(item.started_at),
        }
        for item in ctx.plane.list_microvms(image_arn=image_arn)
    ]


def preflight(ctx: RunContext) -> None:
    for arn in image_arns(ctx):
        live = live_vms(ctx, arn)
        if len(live) > MAX_LIVE_VMS:
            raise SystemExit(
                f"pre-flight: {len(live)} live MicroVMs of {arn}; clean up first"
            )
        log.info("pre-flight: %d live MicroVMs of %s", len(live), arn)


def image_arns(ctx: RunContext) -> list[str]:
    return [arn for arn in (ctx.full_arn, ctx.slim_arn) if arn]


def survivors(ctx: RunContext) -> dict[str, list[dict[str, Any]]]:
    """The exit listing of design D8: every live VM of either image and,
    first, the ids this run launched but could not terminate (the operator
    finishes those by hand)."""
    for vm_id in ctx.registry.unterminated():
        log.error("NOT terminated by this run, terminate by hand: %s", vm_id)
    found = {arn: live_vms(ctx, arn) for arn in image_arns(ctx)}
    for arn, vms in found.items():
        for vm in vms:
            log.warning(
                "survivor of %s: %s %s startedAt=%s",
                arn,
                vm["microvm_id"],
                vm["state"],
                vm["started_at"],
            )
    return found


def image_block(ctx: RunContext, arn: str, version: str | None) -> dict[str, Any]:
    """``get-microvm-image-version`` + ``list-microvm-image-builds`` +
    ``get-microvm-image-build`` for the launched version. A failure of those
    calls is recorded in the block (``error``), never raised: the JSON of a
    paid run must be written whatever the image lookup does."""
    block: dict[str, Any] = {"arn": arn, "version": version}
    if version is None:
        return block
    try:
        block.update(image_details(ctx, arn, version))
    except Exception as exc:  # noqa: BLE001 - the run data outrank the image block
        log.warning(
            "image lookup of %s %s failed: %s", arn, version, type(exc).__name__
        )
        block["error"] = type(exc).__name__
    return block


def image_details(ctx: RunContext, arn: str, version: str) -> dict[str, Any]:
    block: dict[str, Any] = {}
    client = ctx.raw_client
    detail = client.get_microvm_image_version(imageIdentifier=arn, imageVersion=version)
    block["state"], block["status"] = detail.get("state"), detail.get("status")
    builds = client.list_microvm_image_builds(imageIdentifier=arn, imageVersion=version)
    items = builds.get("items", [])
    if not items:
        return block
    build = client.get_microvm_image_build(
        imageIdentifier=arn, imageVersion=version, buildId=items[0]["buildId"]
    )
    snapshot = build.get("snapshotBuild", {})
    block.update({key: snapshot.get(key) for key in IMAGE_SIZE_KEYS})
    block["chipsetGeneration"] = build.get("chipsetGeneration")
    return block


def launched_version(phases: dict[str, Any], key: str) -> str | None:
    phase = phases.get(key)
    if not phase:
        return None
    for sample in phase.get("samples", []):
        if sample.get("image_version"):
            return str(sample["image_version"])
    return None


# ------------------------------------------------------------------ output


def all_launch_samples(phases: dict[str, Any]) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    for key in ("sequential", "sequential_slim"):
        if phases.get(key):
            samples.extend(phases[key]["samples"])
    for batch in phases.get("bursts", []):
        samples.extend(batch["samples"])
    resume = phases.get("resume")
    if resume:
        samples.extend(resume[kind]["launch"] for kind in ("explicit", "auto"))
    return samples


def cycle_samples(phases: dict[str, Any]) -> list[dict[str, Any]]:
    resume = phases.get("resume")
    if not resume:
        return []
    return [*resume["explicit"]["samples"], *resume["auto"]["samples"]]


def cost_block(phases: dict[str, Any], images: dict[str, Any]) -> dict[str, Any]:
    launches = all_launch_samples(phases)
    cycles = cycle_samples(phases)
    launched = [s for s in launches if s.get("microvm_id")]
    suspends = [c for c in cycles if c.get("pause_s") is not None]
    resumes = [c for c in cycles if c.get("resume_generation") is not None]
    vm_seconds = sum(s.get("vm_alive_s") or 0.0 for s in launched)
    read_gb = sum(memory_gb(images, s["variant"]) for s in launched)
    read_gb += len(resumes) * memory_gb(images, "full")
    write_gb = len(suspends) * memory_gb(images, "full")
    compute = vm_seconds * (
        VCPU_PER_VM * PRICE_VCPU_SECOND + GB_PER_VM * PRICE_GB_SECOND
    )
    return {
        "launches": len(launched),
        "throttled_launches": sum(
            1 for s in launches if s.get("error") == "ThrottlingException"
        ),
        "failed_launches": sum(1 for s in launches if s.get("error")),
        "suspends": len(suspends),
        "resumes": len(resumes),
        "vm_seconds": round(vm_seconds, 1),
        "snapshot_read_gb_expected": round(read_gb, 3),
        "snapshot_write_gb_expected": round(write_gb, 3),
        "estimated_usd": round(
            compute
            + read_gb * PRICE_SNAPSHOT_READ_GB
            + write_gb * PRICE_SNAPSHOT_WRITE_GB,
            4,
        ),
    }


def memory_gb(images: dict[str, Any], variant: str) -> float:
    size = (images.get(variant) or {}).get("memorySnapshotSizeInBytes")
    return float(size) / 1e9 if size else ESTIMATE_MEMORY_SNAPSHOT_GB


def render_summary_table(
    summary: dict[str, dict[str, Any]], metrics: Sequence[str]
) -> str:
    lines = ["| metric | n | p50 | p95 | min | max |", "|---|---|---|---|---|---|"]
    for metric in metrics:
        row = summary.get(metric)
        if row is None:
            continue
        lines.append(
            f"| `{metric}` | {row['n']} | {fmt(row['p50'])} | {fmt(row['p95'])} | "
            f"{fmt(row['min'])} | {fmt(row['max'])} |"
        )
    return "\n".join(lines)


def fmt(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.3f}" if abs(value) < 1000 else f"{value:.0f}"
    return str(value)


LAUNCH_METRICS = (
    "api_s",
    "token_s",
    "agent_ready_s",
    "agent_ready_gap_s",
    "kernel_ready_s",
    "kernel_ready_gap_s",
    "agent_uptime_ms_at_ready",
    "health_polls",
    "throttled_polls",
    "first_cell_s",
    "stack_cell_s",
    "vm_alive_s",
)
CYCLE_METRICS = (
    "pause_s",
    "resume_api_s",
    "resume_s",
    "resume_gap_s",
    "auto_resume_s",
    "first_cell_after_resume_s",
    "resume_generation",
)


def render_bursts_table(bursts: list[dict[str, Any]]) -> str:
    lines = [
        (
            "| mode | size | ok | throttled | failed | batch_wall_s | api_s p50/p95 | "
            "agent_ready_s p50/p95 | kernel_ready_s p50/p95 | from_batch max | "
            "throttled_polls |"
        ),
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for batch in bursts:
        summary = batch["summary"]
        ok = batch["size"] - batch["failed"]
        lines.append(
            f"| {batch['mode']} | {batch['size']} | {ok} | {batch['throttled']} | {batch['failed']} | "
            f"{fmt(batch['batch_wall_s'])} | {pair(summary, 'api_s')} | "
            f"{pair(summary, 'agent_ready_s')} | {pair(summary, 'kernel_ready_s')} | "
            f"{fmt(summary.get('kernel_ready_from_batch_s', {}).get('max'))} | "
            f"{batch['throttled_polls']} |"
        )
    return "\n".join(lines)


def pair(summary: dict[str, Any], metric: str) -> str:
    row = summary.get(metric) or {}
    return f"{fmt(row.get('p50'))} / {fmt(row.get('p95'))}"


def render_tables(run: dict[str, Any]) -> str:
    phases = run["phases"]
    parts: list[str] = []
    for key, title in (
        ("sequential", "(a) sequential, full"),
        ("sequential_slim", "(e) sequential, slim"),
    ):
        if phases.get(key):
            parts.append(
                f"### {title}\n\n{render_summary_table(phases[key]['summary'], LAUNCH_METRICS)}"
            )
    if phases.get("bursts"):
        parts.append(f"### (b) bursts\n\n{render_bursts_table(phases['bursts'])}")
    resume = phases.get("resume")
    if resume:
        for kind in ("explicit", "auto"):
            alive = sum(1 for c in resume[kind]["samples"] if c.get("kernel_alive"))
            parts.append(
                f"### (c) resume, {kind} (kernel_alive {alive}/{len(resume[kind]['samples'])})\n\n"
                f"{render_summary_table(resume[kind]['summary'], CYCLE_METRICS)}"
            )
    parts.append(f"### cost\n\n```json\n{json.dumps(run['cost'], indent=2)}\n```")
    return "\n\n".join(parts)


def write_run(path: Path, run: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(run, indent=2, default=str) + "\n", encoding="utf-8")


# --------------------------------------------------------------------- cli


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--template", help="full image (ARN or name)")
    parser.add_argument(
        "--template-slim", default=None, help="slim image (ARN or name)"
    )
    parser.add_argument("--sequential", type=int, default=20)
    parser.add_argument("--bursts", default="5,10,20")
    parser.add_argument("--burst-modes", default="sdk,raw")
    parser.add_argument("--resume-cycles", type=int, default=10)
    parser.add_argument("--phases", default="a,b,c,e")
    parser.add_argument("--out", type=Path, default=Path("docs/benchmarks/raw"))
    parser.add_argument("--execution-role-arn", default=None)
    parser.add_argument("--budget-usd", type=float, default=5.0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--report", type=Path, default=None, help="render an existing JSON"
    )
    return parser.parse_args(argv)


def csv_items(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def plan_from_args(args: argparse.Namespace) -> Plan:
    phases = tuple(phase for phase in csv_items(args.phases) if phase in PHASES)
    sizes = [int(size) for size in csv_items(args.bursts)]
    modes = [mode for mode in csv_items(args.burst_modes) if mode in BURST_MODES]
    batches = tuple(Batch(mode=mode, size=size) for size in sizes for mode in modes)
    slim = bool(args.template_slim)
    if not slim:
        phases = tuple(phase for phase in phases if phase != "e")
    return Plan(
        phases=phases,
        sequential=args.sequential,
        batches=batches,
        resume_cycles=args.resume_cycles,
        slim=slim,
    )


def describe_plan(plan: Plan, estimate: float, budget: float) -> str:
    batches = (
        ", ".join(f"{b.mode} {b.size}" for b in plan.batches)
        if "b" in plan.phases
        else "-"
    )
    return (
        f"phases={','.join(plan.phases)} sequential={plan.sequential} batches=[{batches}] "
        f"resume_cycles={plan.resume_cycles} slim={plan.slim}\n"
        f"launches={plan.launches} cycles={plan.cycles} "
        f"estimate=${estimate:.2f} budget=${budget:.2f}"
    )


def args_block(args: argparse.Namespace, plan: Plan) -> dict[str, Any]:
    return {
        "sequential": plan.sequential,
        "bursts": sorted({b.size for b in plan.batches}),
        "burst_modes": [
            m for m in BURST_MODES if any(b.mode == m for b in plan.batches)
        ],
        "resume_cycles": plan.resume_cycles,
        "phases": list(plan.phases),
        "execution_role": bool(args.execution_role_arn),
        "template": args.template,
        "template_slim": args.template_slim,
    }


def build_context(args: argparse.Namespace) -> RunContext:
    plane = LambdaMicrovmsControlPlane.from_session()
    full_arn = plane.resolve_template_arn(args.template)
    slim_arn = (
        plane.resolve_template_arn(args.template_slim) if args.template_slim else None
    )
    return RunContext(
        plane=plane,
        raw_client=raw_lambda_client(plane.region),
        registry=VmRegistry(plane),
        full_arn=full_arn,
        slim_arn=slim_arn,
        execution_role_arn=args.execution_role_arn,
    )


def execute_phases(ctx: RunContext, plan: Plan) -> dict[str, Any]:
    phases: dict[str, Any] = {}
    if "a" in plan.phases:
        log.info("phase a: %d sequential launches (full)", plan.sequential)
        phases["sequential"] = phase_sequential(ctx, plan.sequential, "full")
        ctx.registry.enforce_budget()
    if "b" in plan.phases:
        log.info("phase b: %d batches", len(plan.batches))
        phases["bursts"] = phase_bursts(ctx, plan.batches)
        ctx.registry.enforce_budget()
    if "c" in plan.phases:
        log.info(
            "phase c: %d + %d resume cycles", plan.resume_cycles, plan.resume_cycles
        )
        phases["resume"] = phase_resume(ctx, plan.resume_cycles)
        ctx.registry.enforce_budget()
    if "e" in plan.phases:
        log.info("phase e: %d sequential launches (slim)", plan.sequential)
        phases["sequential_slim"] = phase_sequential(ctx, plan.sequential, "slim")
        ctx.registry.enforce_budget()
    return phases


def assemble_run(
    ctx: RunContext,
    args: argparse.Namespace,
    plan: Plan,
    run_id: str,
    phases: dict[str, Any],
    aborted: bool,
) -> dict[str, Any]:
    images = {
        "full": image_block(
            ctx,
            ctx.full_arn,
            launched_version(phases, "sequential") or first_burst_version(phases),
        ),
        "slim": image_block(
            ctx, ctx.slim_arn, launched_version(phases, "sequential_slim")
        )
        if ctx.slim_arn
        else None,
    }
    return {
        "schema": SCHEMA,
        "meta": {
            "generated_at": utc_now(),
            "run_id": run_id,
            "aborted": aborted,
            "region": ctx.plane.region,
            "client_rtt_ms": ctx.client_rtt_ms,
            "sdk_version": sdk_version,
            "boto3_version": boto3.__version__,
            "python": platform.python_version(),
            "rayd_version": ctx.rayd_version,
            "args": args_block(args, plan),
            "images": images,
        },
        "phases": phases,
        "cost": cost_block(phases, images),
    }


def first_burst_version(phases: dict[str, Any]) -> str | None:
    for batch in phases.get("bursts", []):
        for sample in batch["samples"]:
            if sample.get("image_version"):
                return str(sample["image_version"])
    resume = phases.get("resume")
    if resume:
        return resume["explicit"]["launch"].get("image_version")
    return None


def run_benchmark(args: argparse.Namespace, plan: Plan) -> int:
    """Whatever leaves ``execute_phases`` (Ctrl-C, a bug, a lost network),
    the sweep runs and the JSON is written with ``meta.aborted: true`` before
    the exception propagates; only ``KeyboardInterrupt`` is swallowed."""
    ctx = build_context(args)
    preflight(ctx)
    run_id = run_id_now()
    out = args.out / f"2026-09-cold-start-{run_id}.json"
    phases: dict[str, Any] = {}
    aborted = False
    try:
        phases = execute_phases(ctx, plan)
    except KeyboardInterrupt:
        aborted = True
        log.warning("interrupted: sweeping and writing what was collected")
    except BaseException:
        aborted = True
        raise
    finally:
        finish_run(ctx, args, plan, run_id, out, phases, aborted)
    return 130 if aborted else 0


def finish_run(
    ctx: RunContext,
    args: argparse.Namespace,
    plan: Plan,
    run_id: str,
    out: Path,
    phases: dict[str, Any],
    aborted: bool,
) -> None:
    """Sweep, write, then list: the file lands before any further AWS call
    can fail, and the survivor listing cannot mask what the run collected."""
    ctx.registry.enforce_budget()
    swept = ctx.registry.sweep()
    if swept:
        log.warning("exit sweep terminated %d VMs", len(swept))
    run = assemble_run(ctx, args, plan, run_id, phases, aborted)
    write_run(out, run)
    log.info("wrote %s", out)
    try:
        survivors(ctx)
    except Exception as exc:  # noqa: BLE001 - the listing is advisory
        log.warning("survivor listing failed: %s", type(exc).__name__)
    print(render_tables(run))


def utf8_stdout() -> None:
    """The tables use an em dash; a cp1252 console must not mangle them."""
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8")


def main(argv: Sequence[str]) -> int:
    utf8_stdout()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    quiet_loggers()
    args = parse_args(argv)
    if args.report is not None:
        print(render_tables(json.loads(args.report.read_text(encoding="utf-8"))))
        return 0
    if not args.template:
        raise SystemExit("--template is required (or --report <json>)")
    plan = plan_from_args(args)
    estimate = estimate_cost(plan.launches, plan.cycles)
    print(describe_plan(plan, estimate, args.budget_usd))
    if estimate > args.budget_usd:
        print(
            f"estimate ${estimate:.2f} exceeds --budget-usd {args.budget_usd:.2f}; refusing"
        )
        return 2
    if args.dry_run:
        return 0
    return run_benchmark(args, plan)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
