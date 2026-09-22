"""M6 "Endurecimiento", pista A, contra AWS real (design "Acceptance test
list" de `openspec/changes/m6-hardening`): hooks forjados por el proxy con
un JWE `allPorts` acuñado fuera del SDK (un `/run` es no-op, un `/suspend`
sin checkpoint lo recupera el watchdog sin perder nada, un `/resume`
después es el real, y un par `/suspend` + `/resume` forjado seguido de otro
`/suspend` inmediato nunca deja a `rayd` sin ejecutar el checklist: ninguna
transición se rechaza), `cpu_time_limit`, el presupuesto de salida y la reserva de
disco, el bloqueo de IMDS en la variante con capabilities (opcional), la
allowlist de egress (opcional), tres pausas durante celdas largas sin
reinicio del sidecar y las medidas del snapshot (Q50). Cada tiempo se
imprime con su nombre para `MILESTONES.md`/`AWS_API_NOTES.md` (Q46-Q51).

Los hooks forjados se envían con `urllib` (HTTP/1.1, lo que AWS usa) a
`https://<endpoint>/aws/lambda-microvms/runtime/v1/<hook>` con
`x-aws-proxy-auth` y `x-aws-proxy-port: 9000`; el SDK nunca acuña
`allPorts`, por eso el token sale de boto3 directamente."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
import time
import urllib.request
from collections.abc import Callable, Iterator
from typing import Any

import boto3
import pytest

from rayito import CommandHandle, IdlePolicy, Sandbox
from rayito._aws import LambdaMicrovmsControlPlane
from rayito.exceptions import (
    CommandExitException,
    NotFoundException,
    SandboxNotFoundException,
)

from .conftest import BootTimings, E2ESettings

pytestmark = pytest.mark.e2e

HOOK_PATH_PREFIX = "/aws/lambda-microvms/runtime/v1"
HOOKS_PORT = 9000
CAPS_TEMPLATE_VAR = "RAYITO_TEMPLATE_CAPS"
EGRESS_CONNECTOR_VAR = "RAYITO_EGRESS_CONNECTOR_ARN"
EXECUTION_ROLE_VAR = "RAYITO_EXECUTION_ROLE_ARN"
FORGED_SANDBOX_TIMEOUT_SECONDS = 1200
FORGED_IDLE_SECONDS = 600
RECONNECT_TIMEOUT_SECONDS = 90.0
TICK_BUDGET_SECONDS = 60.0
STALE_GATE_SECONDS = 20.0
CPU_LIMIT_SECONDS = 2
CPU_KILL_BUDGET_SECONDS = 8.0
IMDS_BUDGET_SECONDS = 10.0
FORGED_PAIR_GAP_SECONDS = 1.0
REATTACH_CELL_SECONDS = 25
REATTACH_PAUSE_AFTER_SECONDS = 3.0
REATTACH_PAUSED_FOR_SECONDS = 5.0
POLL_SECONDS = 0.2
MIB = 1024 * 1024
DISK_RESERVE_BYTES = 256 * MIB
OUTPUT_BUDGET_BYTES = 128 * MIB
RSS_LADDER = r"""
import resource, time
from io import BytesIO
def rss():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
steps = [("baseline", rss(), 0.0)]
t = time.perf_counter(); import numpy; steps.append(("numpy", rss(), time.perf_counter() - t))
t = time.perf_counter(); import pandas; steps.append(("pandas", rss(), time.perf_counter() - t))
t = time.perf_counter()
import matplotlib.pyplot as plt
fig, ax = plt.subplots(); ax.plot([0, 1]); fig.savefig(BytesIO(), format="png"); plt.close(fig)
steps.append(("matplotlib.pyplot+savefig", rss(), time.perf_counter() - t))
t = time.perf_counter(); import scipy.stats
steps.append(("scipy.stats", rss(), time.perf_counter() - t))
t = time.perf_counter(); import sklearn.linear_model
steps.append(("sklearn.linear_model", rss(), time.perf_counter() - t))
prev = 0.0
for name, mb, secs in steps:
    print(f"{name:26s} rss_mb={mb:8.1f} delta_mb={mb - prev:8.1f} import_s={secs:6.2f}")
    prev = mb
"""
IMDS_PROBE = (
    "python3 -c 'import urllib.request as u; "
    'r = u.Request("http://169.254.169.254/latest/api/token", method="PUT", '
    'headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"}); '
    'u.urlopen(r, timeout=3); print("imds ok")\''
)
EGRESS_PROBE = (
    "python3 -c 'import urllib.request as u; "
    'u.urlopen("https://example.com", timeout=5); print("egress ok")\''
)


def report(label: str, value: object) -> None:
    print(f"\n[m6] {label}: {value}", flush=True)


def wait_until(predicate: Callable[[], bool], budget: float, what: str) -> float:
    started = time.perf_counter()
    while not predicate():
        elapsed = time.perf_counter() - started
        assert elapsed < budget, f"{what} no ocurrió en {budget:g} s"
        time.sleep(POLL_SECONDS)
    return time.perf_counter() - started


class TickCollector:
    """Itera un handle en background en un hilo daemon: cuenta las líneas y
    guarda el instante de cada una y la excepción final."""

    def __init__(self, handle: CommandHandle) -> None:
        self.handle = handle
        self.lines: list[str] = []
        self.seen_at: list[float] = []
        self.error: Exception | None = None
        self.thread = threading.Thread(target=self._run, name="m6-ticks", daemon=True)
        self.thread.start()

    def _run(self) -> None:
        try:
            for stdout, _, _ in self.handle:
                if stdout is not None:
                    self.lines.append(stdout)
                    self.seen_at.append(time.perf_counter())
        except Exception as exc:
            self.error = exc


class WarningCounter(logging.Handler):
    def __init__(self, needle: str) -> None:
        super().__init__(level=logging.WARNING)
        self.needle = needle
        self.count = 0

    def emit(self, record: logging.LogRecord) -> None:
        if self.needle in record.getMessage():
            self.count += 1


@contextlib.contextmanager
def counting_warnings(needle: str) -> Iterator[WarningCounter]:
    logger = logging.getLogger("rayito.sandbox")
    counter = WarningCounter(needle)
    logger.addHandler(counter)
    try:
        yield counter
    finally:
        logger.removeHandler(counter)


class HookForger:
    """El atacante del modelo de amenazas T2: un JWE `allPorts` acuñado con
    boto3 (nunca a través del SDK) y POSTs HTTP/1.1 por el proxy al puerto
    de hooks."""

    def __init__(self, sandbox: Sandbox, region: str | None) -> None:
        self.sandbox = sandbox
        self.endpoint = sandbox.endpoint
        client = boto3.session.Session(region_name=region).client("lambda-microvms")
        started = time.perf_counter()
        minted = client.create_microvm_auth_token(
            microvmIdentifier=sandbox.sandbox_id,
            expirationInMinutes=5,
            allowedPorts=[{"allPorts": {}}],
        )
        self.token: str = minted["authToken"]["X-aws-proxy-auth"]
        report("forge: allPorts token minted (s)", f"{time.perf_counter() - started:.2f}")

    def post(
        self, hook: str, body: dict[str, Any] | None = None
    ) -> tuple[int, dict[str, Any], float]:
        request = urllib.request.Request(
            f"https://{self.endpoint}{HOOK_PATH_PREFIX}/{hook}",
            data=json.dumps(body).encode() if body is not None else b"",
            method="POST",
            headers={
                "content-type": "application/json",
                "x-aws-proxy-auth": self.token,
                "x-aws-proxy-port": str(HOOKS_PORT),
            },
        )
        started = time.perf_counter()
        with urllib.request.urlopen(request, timeout=35) as response:
            status = int(response.status)
            reply = json.loads(response.read().decode() or "{}")
        elapsed = time.perf_counter() - started
        report(f"forge: /{hook}", f"{status} {reply.get('outcome')} ({elapsed:.2f} s)")
        return status, reply, elapsed

    def forged_run(self) -> tuple[int, dict[str, Any], float]:
        body = {
            "microvmId": self.sandbox.sandbox_id,
            "runHookPayload": json.dumps({"v": 1, "token_sha256": "f" * 64}),
        }
        return self.post("run", body)


def forge_hook(
    sandbox: Sandbox, hook: str, body: dict[str, Any] | None, region: str | None
) -> tuple[int, dict[str, Any], float]:
    return HookForger(sandbox, region).post(hook, body)


@pytest.fixture
def forged_sandbox(
    e2e_settings: E2ESettings,
    control_plane: LambdaMicrovmsControlPlane,
    template_arn: str,
) -> Iterator[Sandbox]:
    started = time.perf_counter()
    created = Sandbox.create(
        template_arn,
        template_version=e2e_settings.template_version,
        timeout=FORGED_SANDBOX_TIMEOUT_SECONDS,
        idle=IdlePolicy(
            max_idle_seconds=FORGED_IDLE_SECONDS,
            suspended_duration_seconds=FORGED_IDLE_SECONDS,
            auto_resume=True,
        ),
        execution_role_arn=e2e_settings.execution_role_arn,
        ingress=["ALL_INGRESS"],
        logging=e2e_settings.logging,
        control_plane=control_plane,
        reconnect_timeout=RECONNECT_TIMEOUT_SECONDS,
    )
    report("kernel_ready_s", f"{time.perf_counter() - started:.2f}")
    try:
        yield created
    finally:
        with contextlib.suppress(SandboxNotFoundException):
            created.kill()


def expect_hook(forger: HookForger, hook: str, outcome: str) -> dict[str, Any]:
    status, reply, _ = forger.post(hook)
    assert status == 200, (hook, status)
    assert reply["outcome"] == outcome, (hook, reply)
    return reply


def test_forged_hooks(forged_sandbox: Sandbox, e2e_settings: E2ESettings) -> None:
    sbx = forged_sandbox
    sbx.commands.run("echo before > /home/user/m6.txt", timeout=30)
    ticker = sbx.commands.run(
        "i=0; while true; do echo tick $i; i=$((i+1)); sleep 1; done",
        background=True,
        timeout=None,
    )
    ticks = TickCollector(ticker)
    pty = sbx.pty.create()
    pty.send_input(b"echo h''ola\n")
    assert sbx.run_code("x = 42").error is None
    wait_until(lambda: len(ticks.lines) >= 2, TICK_BUDGET_SECONDS, "los primeros ticks")
    h0 = sbx.get_health()
    assert h0.hook_anomalies == 0
    generation_0 = h0.resume_generation

    forger = HookForger(sbx, e2e_settings.region)
    with counting_warnings("hook_anomalies") as warned:
        status, reply, _ = forger.forged_run()
        assert status == 200 and reply["outcome"] == "already_ran"
        assert sbx.commands.run("echo still", timeout=30).stdout.strip() == "still"
        assert sbx.get_health().hook_anomalies == 1, "the forged /run is the first anomaly"
        assert warned.count == 1

        outcomes = [forger.post("suspend")[1]["outcome"] for _ in range(3)]
        assert outcomes == ["changed", "unchanged", "unchanged"], outcomes
        cut_at = time.perf_counter()
        assert sbx.get_info().state == "RUNNING", "nobody checkpointed: the VM stays RUNNING"
        ticks_before = len(ticks.lines)
        wait_until(
            lambda: len(ticks.lines) > ticks_before + 1,
            RECONNECT_TIMEOUT_SECONDS,
            "los ticks tras la recuperación del suspend forjado",
        )
        first_tick_after_cut = ticks.seen_at[ticks_before] - cut_at
        report("forged suspend: first tick after the cut (s)", f"{first_tick_after_cut:.2f}")
        assert first_tick_after_cut <= STALE_GATE_SECONDS + 30
        assert ticks.error is None
        assert ticker.reconnects == 1, ticker.reconnects
        assert sbx.files.read("/home/user/m6.txt") == "before\n"
        assert sbx.run_code("x").text == "42"
        live = {info.pid for info in sbx.commands.list()}
        assert ticker.pid in live and pty.pid in live, "nothing was killed"
        h1 = sbx.get_health()
        assert h1.resume_generation == generation_0, "no /resume: no generation bump"
        assert h1.hook_anomalies == 2, "forged /run + stale-suspend recovery"
        assert warned.count == 1, "one warning per generation"

        resumed = expect_hook(forger, "resume", "changed")
        assert resumed["resume_generation"] == generation_0 + 1, "the real resume after a recovery"
        forged_suspend = expect_hook(forger, "suspend", "changed")
        forged_resume = expect_hook(forger, "resume", "changed")
        assert forged_resume["resume_generation"] == generation_0 + 2
        time.sleep(FORGED_PAIR_GAP_SECONDS)
        suspend_after_the_pair = expect_hook(forger, "suspend", "changed")
        assert suspend_after_the_pair["streams_closed"] is not None, "the checklist ran"
        assert (
            suspend_after_the_pair["suspend_generation"] == forged_suspend["suspend_generation"] + 1
        )
        resume_after_the_pair = expect_hook(forger, "resume", "changed")
        assert resume_after_the_pair["resume_generation"] == generation_0 + 3
        expect_hook(forger, "resume", "unchanged")
        wait_until(
            lambda: sbx.get_health().resume_generation == generation_0 + 3,
            RECONNECT_TIMEOUT_SECONDS,
            "la generación tras los /resume forjados",
        )
        h2 = sbx.get_health()
        assert h2.hook_anomalies == 2, "still run + recovery: no transition was refused"
        report("forged pair then /suspend at +1 s", "changed / changed (never refused)")
        wait_until(lambda: warned.count >= 2, 30.0, "el aviso de la generación nueva")

    ticks_before = len(ticks.lines)
    wait_until(lambda: len(ticks.lines) > ticks_before, RECONNECT_TIMEOUT_SECONDS, "ticks")
    assert ticks.error is None
    assert sbx.run_code("x").text == "42"
    time.sleep(2.5)
    assert sbx.pause(wait=True)
    resumed_at = time.perf_counter()
    sbx.resume(wait=True)
    report("real pause/resume after the forgeries (s)", f"{time.perf_counter() - resumed_at:.2f}")
    h3 = sbx.get_health()
    assert h3.resume_generation == generation_0 + 4
    assert sbx.run_code("x").text == "42"
    assert sbx.commands.run("sleep 2", timeout=10).exit_code == 0
    ticker.kill()
    pty.kill()
    assert sbx.kill()


@pytest.fixture
def cpu_limited_sandbox(
    e2e_settings: E2ESettings,
    control_plane: LambdaMicrovmsControlPlane,
    template_arn: str,
) -> Iterator[Sandbox]:
    created = Sandbox.create(
        template_arn,
        template_version=e2e_settings.template_version,
        timeout=900,
        idle=None,
        execution_role_arn=e2e_settings.execution_role_arn,
        ingress=["ALL_INGRESS"],
        logging=e2e_settings.logging,
        control_plane=control_plane,
        cpu_time_limit=CPU_LIMIT_SECONDS,
    )
    try:
        yield created
    finally:
        with contextlib.suppress(SandboxNotFoundException):
            created.kill()


def test_cpu_time_limit(cpu_limited_sandbox: Sandbox) -> None:
    sbx = cpu_limited_sandbox
    assert sbx.commands.run("ulimit -t; ulimit -Ht", timeout=30).stdout.split() == [
        str(CPU_LIMIT_SECONDS),
        str(CPU_LIMIT_SECONDS + 5),
    ]
    started = time.perf_counter()
    with pytest.raises(CommandExitException) as excinfo:
        sbx.commands.run("python3 -c 'while True: pass'", timeout=30)
    elapsed = time.perf_counter() - started
    report(
        "cpu_time_limit: busy loop killed (s)", f"{elapsed:.2f} exit_code={excinfo.value.exit_code}"
    )
    assert excinfo.value.exit_code in (128 + 24, 137)
    assert elapsed <= CPU_KILL_BUDGET_SECONDS
    assert sbx.commands.run("sleep 3", timeout=30).exit_code == 0, "sleeping is not CPU"
    assert sbx.run_code("sum(range(10**7))").text == str(sum(range(10**7))), (
        "the kernel is unlimited"
    )


def test_output_budget_and_disk_reserve(sandbox: Sandbox) -> None:
    """Two 20 MB streams are delivered whole while each ring keeps its last
    1 MiB (so a replay from the start is out of range); the 128 MiB
    sandbox-wide budget itself is exercised by the scaled integration test
    (`crates/rayd/tests/m6_limits.rs`), not here: at ~4 MB/s through the
    proxy filling it would cost minutes, not knowledge."""
    script = "head -c 20000000 /dev/zero | tr '\\0' x"
    started = time.perf_counter()
    handles = [sandbox.commands.run(script, background=True, timeout=None) for _ in range(2)]
    totals = [len(handle.wait().stdout) for handle in handles]
    report("output: two 20 MB streams delivered live (s)", f"{time.perf_counter() - started:.2f}")
    assert totals == [20_000_000, 20_000_000], totals
    with pytest.raises(NotFoundException):
        sandbox.commands.connect(handles[0].pid, from_seq=1)
    report("output: Connect(from_seq=1) on the first stream", "OUT_OF_RANGE -> NotFoundException")
    fresh = sandbox.commands.connect(handles[0].pid)
    assert fresh.wait().exit_code == 0
    sandbox.files.write("/home/user/one-mib.bin", b"\x01" * MIB)
    metrics = sandbox.get_metrics()
    free = metrics.disk_total_bytes - metrics.disk_used_bytes
    report("disk free after a 1 MiB write (MiB)", f"{free / MIB:.0f}")
    assert free > DISK_RESERVE_BYTES


def caps_template() -> str | None:
    return os.environ.get(CAPS_TEMPLATE_VAR) or None


@pytest.fixture
def caps_sandbox(
    e2e_settings: E2ESettings, control_plane: LambdaMicrovmsControlPlane
) -> Iterator[Sandbox]:
    template = caps_template()
    if not template or not e2e_settings.execution_role_arn:
        pytest.skip(
            f"exporta {CAPS_TEMPLATE_VAR} y {EXECUTION_ROLE_VAR} para medir el bloqueo de IMDS"
        )
    started = time.perf_counter()
    created = Sandbox.create(
        control_plane.resolve_template_arn(template),
        timeout=900,
        idle=None,
        execution_role_arn=e2e_settings.execution_role_arn,
        ingress=["ALL_INGRESS"],
        logging=e2e_settings.logging,
        control_plane=control_plane,
    )
    report("caps image kernel_ready_s", f"{time.perf_counter() - started:.2f}")
    try:
        yield created
    finally:
        with contextlib.suppress(SandboxNotFoundException):
            created.kill()


def test_imds_block(caps_sandbox: Sandbox, sandbox: Sandbox) -> None:
    blocked_after = wait_until(
        lambda: caps_sandbox.get_health().imds_blocked, IMDS_BUDGET_SECONDS, "imds_blocked"
    )
    report("caps image: imds_blocked after readiness (s)", f"{blocked_after:.2f}")
    report(
        "caps image: CapEff as uid 1000",
        caps_sandbox.commands.run("grep CapEff /proc/self/status", timeout=30).stdout.strip(),
    )
    started = time.perf_counter()
    with pytest.raises(CommandExitException) as excinfo:
        caps_sandbox.commands.run(IMDS_PROBE, timeout=30)
    elapsed = time.perf_counter() - started
    report(
        "caps image: PUT /latest/api/token as uid 1000",
        f"exit_code={excinfo.value.exit_code} in {elapsed:.2f} s",
    )
    assert elapsed <= 5.0
    control = sandbox.commands.run(IMDS_PROBE, timeout=30)
    assert control.stdout.strip() == "imds ok", "the default image is fail-open"
    assert sandbox.get_health().imds_blocked is False


def test_egress_allowlist(
    e2e_settings: E2ESettings,
    control_plane: LambdaMicrovmsControlPlane,
    template_arn: str,
    sandbox: Sandbox,
) -> None:
    connector = os.environ.get(EGRESS_CONNECTOR_VAR) or None
    if not connector:
        pytest.skip(
            f"exporta {EGRESS_CONNECTOR_VAR} (infra/egress-connector.yaml) para medir la allowlist"
        )
    created = Sandbox.create(
        template_arn,
        timeout=900,
        idle=None,
        execution_role_arn=e2e_settings.execution_role_arn,
        ingress=["ALL_INGRESS"],
        egress=[connector],
        logging=e2e_settings.logging,
        control_plane=control_plane,
    )
    try:
        info = created.get_info()
        report("egress: get_info().egress", info.egress)
        report("egress: get_info().ingress", info.ingress)
        assert info.egress == (connector,)
        started = time.perf_counter()
        with pytest.raises(CommandExitException) as excinfo:
            created.commands.run(EGRESS_PROBE, timeout=30)
        elapsed = time.perf_counter() - started
        report(
            "egress: example.com through the allowlist",
            f"exit_code={excinfo.value.exit_code} in {elapsed:.2f} s",
        )
        assert elapsed <= 10.0
        assert sandbox.commands.run(EGRESS_PROBE, timeout=30).stdout.strip() == "egress ok"
    finally:
        with contextlib.suppress(SandboxNotFoundException):
            created.kill()


def run_long_cell(sbx: Sandbox, cycle: int, result: dict[str, Any]) -> None:
    try:
        result["execution"] = sbx.run_code(
            f"import time; time.sleep({REATTACH_CELL_SECONDS}); 'slept-{cycle}'", timeout=120
        )
    except Exception as exc:
        result["error"] = exc


def test_reseed_during_long_cell(forged_sandbox: Sandbox) -> None:
    sbx = forged_sandbox
    assert sbx.run_code("import random; y = 7").error is None
    for cycle in range(1, 4):
        result: dict[str, Any] = {}
        worker = threading.Thread(
            target=run_long_cell, args=(sbx, cycle, result), name=f"m6-cell-{cycle}", daemon=True
        )
        worker.start()
        time.sleep(REATTACH_PAUSE_AFTER_SECONDS)
        assert sbx.pause(wait=True)
        time.sleep(REATTACH_PAUSED_FOR_SECONDS)
        started = time.perf_counter()
        sbx.resume(wait=True)
        report(f"reseed cycle {cycle}: resume_s", f"{time.perf_counter() - started:.2f}")
        worker.join(timeout=120)
        assert not worker.is_alive(), "the cell never finished"
        assert "error" not in result, result.get("error")
        assert result["execution"].text == f"'slept-{cycle}'"
        health = sbx.get_health()
        assert health.kernel_ready and not health.kernel_state_lost
        assert sbx.run_code("y").text == "7", "the kernel kept its state: no sidecar restart"
    assert sbx.run_code("import random; random.random()").error is None
    report("reseed: three pauses during 25 s cells", "kernel alive, no restart")


def test_snapshot_measurements(sandbox: Sandbox, boot_timings: BootTimings) -> None:
    health = sandbox.get_health()
    report("snapshot: Health after readiness", health)
    report("snapshot: kernel_ready_s", f"{boot_timings.get(sandbox.sandbox_id, float('nan')):.2f}")
    ladder = sandbox.commands.run(
        'python3 -c "$RAYITO_LADDER"', envs={"RAYITO_LADDER": RSS_LADDER}, timeout=300
    )
    print("\n[m6] snapshot: warm-up RSS ladder (Q50)\n" + ladder.stdout, flush=True)
    assert "sklearn.linear_model" in ladder.stdout
