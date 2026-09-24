"""M9 `m9-server-timeout` contra AWS real (design D12): el plazo lógico que
impone `rayd` (ADR-011) sobre la imagen M9 de `RAYITO_TEMPLATE`.

Cada test imprime lo que su fila de `AWS_API_NOTES.md` §16 necesita
(`stateReason`, sobrepasos, tiempos de suspensión y reanudación: Q63, Q64 y
Q65), siempre con el id del MicroVM y nunca con tokens ni JWE. Todo sandbox
nace con `max_lifetime <= 900`, sin rol de ejecución, y se termina en el
teardown. `test_older_agent_gate` necesita además
`RAYITO_E2E_PRE_M9_TEMPLATE_VERSION` (la versión publicada anterior a M9 de
la misma imagen; sólo en el entorno local, nunca en un fichero versionado).
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import grpc
import pytest

import rayito.e2b
from rayito import IdlePolicy, Sandbox, SandboxInfo
from rayito._aws import LambdaMicrovmsControlPlane, PortSpec
from rayito._limits import DEFAULT_PORT, TERMINAL_STATES
from rayito._process_base import build_start_request
from rayito._transport import ProxyAuthPlugin, TokenRefresher, TokenStore, TransportSettings
from rayito.exceptions import (
    InvalidArgumentException,
    SandboxNotFoundException,
    TimeoutException,
    UnimplementedError,
)
from rayito.v1 import health_pb2, health_pb2_grpc, process_pb2_grpc

from .conftest import E2ESettings

pytestmark = pytest.mark.e2e

PRE_M9_VERSION_VAR = "RAYITO_E2E_PRE_M9_TEMPLATE_VERSION"
MAX_E2E_LIFETIME_SECONDS = 900
TERMINATION_BUDGET_SECONDS = 45
SUSPEND_AFTER_DEADLINE_SECONDS = 15
RESUME_GRACE_SECONDS = 30
AUTO_RESUME_MIN_SECONDS = 300
DEFAULT_MAX_IDLE_SECONDS = 300
SHORT_MAX_IDLE_SECONDS = 60
IDLE_SLACK_SECONDS = 60
DEADLINE_TOLERANCE_SECONDS = 10.0
TRACKING_TOLERANCE_SECONDS = 2.0
POLL_SECONDS = 1.0
CHILD_START_BUDGET_SECONDS = 180
RPC_TIMEOUT_SECONDS = 30.0
PROBE_PATH = "/home/user/m9-timeout-probe.txt"

CHILD_CREATE = """
import sys, time
from rayito import IdlePolicy, Sandbox
template, version, timeout, on_timeout, idle = sys.argv[1:6]
sandbox = Sandbox.create(
    template,
    template_version=version or None,
    timeout=int(timeout),
    max_lifetime=900,
    on_timeout=on_timeout,
    idle=IdlePolicy(auto_resume=True) if idle == "idle" else None,
)
print(sandbox.sandbox_id, flush=True)
time.sleep(3600)
"""


def report(label: str, value: object) -> None:
    print(f"\n[m9-timeout] {label}: {value}", flush=True)


def seconds_since(instant: datetime) -> float:
    return (datetime.now(UTC) - instant).total_seconds()


def create_timed(e2e_settings: E2ESettings, template_arn: str, **kwargs: Any) -> Sandbox:
    """`create()` con plazo lógico dentro del guardrail de e2e."""
    max_lifetime = int(kwargs.pop("max_lifetime", MAX_E2E_LIFETIME_SECONDS))
    assert max_lifetime <= MAX_E2E_LIFETIME_SECONDS, "guardrail: max_lifetime <= 900 s"
    kwargs.setdefault("idle", None)
    return Sandbox.create(
        template_arn,
        template_version=e2e_settings.template_version,
        max_lifetime=max_lifetime,
        ingress=["ALL_INGRESS"],
        **kwargs,
    )


@pytest.fixture
def cleanup(control_plane: LambdaMicrovmsControlPlane) -> Iterator[list[str]]:
    """Los ids que el test añade se terminan al final, pase lo que pase."""
    created: list[str] = []
    yield created
    for sandbox_id in created:
        with contextlib.suppress(SandboxNotFoundException):
            control_plane.terminate_microvm(sandbox_id)


def wait_for(
    control_plane: LambdaMicrovmsControlPlane,
    sandbox_id: str,
    accept: Callable[[SandboxInfo], bool],
    *,
    budget: float,
) -> SandboxInfo:
    """Sondea `get-microvm` cada segundo hasta que `accept` o se agota el
    presupuesto (entonces falla con el último estado visto)."""
    deadline = time.monotonic() + budget
    info = control_plane.get_microvm(sandbox_id)
    while not accept(info):
        assert time.monotonic() < deadline, f"{sandbox_id} sigue {info.state} tras {budget:.0f} s"
        time.sleep(POLL_SECONDS)
        info = control_plane.get_microvm(sandbox_id)
    return info


def is_terminal(info: SandboxInfo) -> bool:
    return info.state in TERMINAL_STATES


def is_terminated(info: SandboxInfo) -> bool:
    return info.state == "TERMINATED"


def is_suspended(info: SandboxInfo) -> bool:
    return info.state == "SUSPENDED"


def report_termination(info: SandboxInfo, deadline: datetime) -> None:
    report(f"{info.sandbox_id} stateReason", info.state_reason)
    if info.terminated_at is not None:
        overrun = (info.terminated_at - deadline).total_seconds()
        report(f"{info.sandbox_id} terminatedAt - deadline (s)", f"{overrun:.1f}")


def deadline_of(sandbox: Sandbox) -> datetime:
    lifecycle = sandbox.get_health().lifecycle
    assert lifecycle is not None and lifecycle.deadline is not None
    return lifecycle.deadline


def remaining_seconds(sandbox: Sandbox) -> float:
    return (deadline_of(sandbox) - datetime.now(UTC)).total_seconds()


def spawn_creator(
    e2e_settings: E2ESettings, template_arn: str, *, timeout: int, on_timeout: str, idle: bool
) -> tuple[subprocess.Popen[str], str]:
    """Un cliente en otro proceso que crea el sandbox, imprime su id y se
    queda vivo hasta que el test lo mata (la muerte del cliente)."""
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            CHILD_CREATE,
            template_arn,
            e2e_settings.template_version or "",
            str(timeout),
            on_timeout,
            "idle" if idle else "none",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert child.stdout is not None
    started = time.monotonic()
    line = child.stdout.readline().strip()
    assert line.startswith("microvm-"), f"el cliente hijo no imprimió un id: {line!r}"
    assert time.monotonic() - started < CHILD_START_BUDGET_SECONDS
    return child, line


def kill_client(child: subprocess.Popen[str]) -> None:
    child.kill()
    child.wait(timeout=30)


def minted_channel(
    control_plane: LambdaMicrovmsControlPlane, info: SandboxInfo, access_token: str | None
) -> grpc.Channel:
    """Un canal propio con un JWE recién acuñado: el tráfico de un cliente
    ajeno al handle del SDK (no reanuda ni reabre nada por su cuenta)."""
    refresher = TokenRefresher(
        TokenStore(), lambda ports: control_plane.create_auth_token(info.sandbox_id, ports)
    )
    refresher.mint((PortSpec.single(DEFAULT_PORT),))
    plugin = ProxyAuthPlugin(refresher.store, port=DEFAULT_PORT, access_token=access_token)
    return TransportSettings().open_channel(info.endpoint, plugin)


def raw_health(control_plane: LambdaMicrovmsControlPlane, sandbox_id: str) -> None:
    info = control_plane.get_microvm(sandbox_id)
    channel = minted_channel(control_plane, info, None)
    try:
        health_pb2_grpc.HealthServiceStub(channel).Health(
            health_pb2.HealthRequest(), timeout=RPC_TIMEOUT_SECONDS
        )
    finally:
        channel.close()


def raw_start_error(
    control_plane: LambdaMicrovmsControlPlane, sandbox_id: str, access_token: str
) -> grpc.RpcError:
    info = control_plane.get_microvm(sandbox_id)
    channel = minted_channel(control_plane, info, access_token)
    try:
        stream = process_pb2_grpc.ProcessServiceStub(channel).Start(
            build_start_request("echo stray"), timeout=RPC_TIMEOUT_SECONDS
        )
        with pytest.raises(grpc.RpcError) as raised:
            next(iter(stream))
        return raised.value
    finally:
        channel.close()


def test_kill_mode_ends_the_vm_after_client_death(
    e2e_settings: E2ESettings,
    control_plane: LambdaMicrovmsControlPlane,
    template_arn: str,
    cleanup: list[str],
) -> None:
    child, sandbox_id = spawn_creator(
        e2e_settings, template_arn, timeout=60, on_timeout="kill", idle=False
    )
    cleanup.append(sandbox_id)
    kill_client(child)
    started_at = control_plane.get_microvm(sandbox_id).started_at
    deadline = started_at + timedelta(seconds=60)
    info = wait_for(
        control_plane,
        sandbox_id,
        is_terminated,
        budget=60 + TERMINATION_BUDGET_SECONDS - seconds_since(started_at) + POLL_SECONDS,
    )
    report_termination(info, deadline)


def test_set_timeout_extends_and_shortens(
    e2e_settings: E2ESettings,
    control_plane: LambdaMicrovmsControlPlane,
    template_arn: str,
    cleanup: list[str],
) -> None:
    extended = create_timed(e2e_settings, template_arn, timeout=60)
    cleanup.append(extended.sandbox_id)
    created = extended.launch_info.started_at
    extended.set_timeout(150)
    extended_at = datetime.now(UTC)
    time.sleep(max(0.0, 90 - seconds_since(created)))
    assert extended.commands.run("echo alive").stdout == "alive\n"
    extended.close()
    info = wait_for(
        control_plane,
        extended.sandbox_id,
        is_terminated,
        budget=150 + TERMINATION_BUDGET_SECONDS + 5 - seconds_since(extended_at),
    )
    report_termination(info, extended_at + timedelta(seconds=150))

    shortened = create_timed(e2e_settings, template_arn, timeout=60)
    cleanup.append(shortened.sandbox_id)
    shortened.set_timeout(10)
    shortened_at = datetime.now(UTC)
    shortened.close()
    info = wait_for(
        control_plane,
        shortened.sandbox_id,
        is_terminated,
        budget=10 + TERMINATION_BUDGET_SECONDS,
    )
    report_termination(info, shortened_at + timedelta(seconds=10))


def test_set_timeout_beyond_cap(
    e2e_settings: E2ESettings, template_arn: str, cleanup: list[str]
) -> None:
    with create_timed(e2e_settings, template_arn, timeout=120) as sandbox:
        cleanup.append(sandbox.sandbox_id)
        before = deadline_of(sandbox)
        with pytest.raises(InvalidArgumentException) as raised:
            sandbox.set_timeout(2000)
        assert "max_lifetime" in str(raised.value)
        assert "28800" in str(raised.value)
        assert deadline_of(sandbox) == before


def test_connect_timeout_is_at_least(
    e2e_settings: E2ESettings,
    control_plane: LambdaMicrovmsControlPlane,
    template_arn: str,
    cleanup: list[str],
) -> None:
    with create_timed(e2e_settings, template_arn, timeout=600) as sandbox:
        cleanup.append(sandbox.sandbox_id)
        long_remaining = remaining_seconds(sandbox)
        with contextlib.closing(
            Sandbox.connect(
                sandbox.sandbox_id,
                timeout=300,
                access_token=sandbox.access_token,
                control_plane=control_plane,
            )
        ) as connected:
            kept = remaining_seconds(connected)
        report(
            "remaining before/after connect(timeout=300) with ~600 left (s)", (long_remaining, kept)
        )
        assert abs(kept - long_remaining) < DEADLINE_TOLERANCE_SECONDS

        sandbox.set_timeout(30)
        with contextlib.closing(
            Sandbox.connect(
                sandbox.sandbox_id,
                timeout=300,
                access_token=sandbox.access_token,
                control_plane=control_plane,
            )
        ) as connected:
            raised_to = remaining_seconds(connected)
        report("remaining after connect(timeout=300) with ~30 left (s)", raised_to)
        assert abs(raised_to - 300) < DEADLINE_TOLERANCE_SECONDS


def test_open_streams_end_with_sandbox_timeout(
    e2e_settings: E2ESettings, template_arn: str, cleanup: list[str]
) -> None:
    sandbox = create_timed(e2e_settings, template_arn, timeout=45)
    cleanup.append(sandbox.sandbox_id)
    try:
        background = sandbox.commands.run("sleep 600", background=True, timeout=None)
        pty = sandbox.pty.create(timeout=None)
        with pytest.raises(TimeoutException):
            background.wait()
        with pytest.raises(TimeoutException):
            pty.wait()
        report(
            f"{sandbox.sandbox_id} stream end after create (s)",
            seconds_since(sandbox.launch_info.started_at),
        )
    finally:
        sandbox.close()


def test_resume_grace_and_stray_auto_resume(
    e2e_settings: E2ESettings,
    control_plane: LambdaMicrovmsControlPlane,
    template_arn: str,
    cleanup: list[str],
) -> None:
    graced = create_timed(
        e2e_settings,
        template_arn,
        timeout=60,
        idle=IdlePolicy(max_idle_seconds=DEFAULT_MAX_IDLE_SECONDS, auto_resume=False),
    )
    cleanup.append(graced.sandbox_id)
    token = graced.access_token
    graced.pause()
    graced.close()
    time.sleep(max(0.0, 75 - seconds_since(graced.launch_info.started_at)))
    with Sandbox.connect(
        graced.sandbox_id, timeout=120, access_token=token, control_plane=control_plane
    ) as reopened:
        assert reopened.run_code("1 + 1").text == "2"
        lifecycle = reopened.get_health().lifecycle
        assert lifecycle is not None and lifecycle.phase == "active"

    stray = create_timed(
        e2e_settings,
        template_arn,
        timeout=60,
        idle=IdlePolicy(max_idle_seconds=DEFAULT_MAX_IDLE_SECONDS, auto_resume=True),
    )
    cleanup.append(stray.sandbox_id)
    stray.pause()
    stray.close()
    time.sleep(max(0.0, 75 - seconds_since(stray.launch_info.started_at)))
    raw_health(control_plane, stray.sandbox_id)
    resumed_at = datetime.now(UTC)
    info = wait_for(
        control_plane,
        stray.sandbox_id,
        is_terminated,
        budget=RESUME_GRACE_SECONDS + TERMINATION_BUDGET_SECONDS,
    )
    report("Q65 stray auto-resume -> TERMINATED (s)", seconds_since(resumed_at))
    report_termination(info, resumed_at + timedelta(seconds=RESUME_GRACE_SECONDS))


def test_pause_mode_live_client_auto_resume(
    e2e_settings: E2ESettings,
    control_plane: LambdaMicrovmsControlPlane,
    template_arn: str,
    cleanup: list[str],
) -> None:
    sandbox = create_timed(
        e2e_settings,
        template_arn,
        timeout=60,
        on_timeout="pause",
        idle=IdlePolicy(max_idle_seconds=DEFAULT_MAX_IDLE_SECONDS, auto_resume=True),
    )
    cleanup.append(sandbox.sandbox_id)
    try:
        sandbox.run_code("x = 42")
        sandbox.files.write(PROBE_PATH, "probe")
        deadline = deadline_of(sandbox)
        wait_for(
            control_plane,
            sandbox.sandbox_id,
            is_suspended,
            budget=(deadline - datetime.now(UTC)).total_seconds() + SUSPEND_AFTER_DEADLINE_SECONDS,
        )
        report("Q64 deadline -> SUSPENDED with a live client (s)", seconds_since(deadline))
        resumed_at = datetime.now(UTC)
        assert sandbox.files.read(PROBE_PATH) == "probe"
        report("Q64 auto-resume after a deadline pause (s)", seconds_since(resumed_at))
        assert sandbox.run_code("x").text == "42"
        new_deadline = deadline_of(sandbox)
        expected = resumed_at + timedelta(seconds=max(60, AUTO_RESUME_MIN_SECONDS))
        assert abs((new_deadline - expected).total_seconds()) < DEADLINE_TOLERANCE_SECONDS
    finally:
        sandbox.close()


def test_pause_mode_dead_client(
    e2e_settings: E2ESettings,
    control_plane: LambdaMicrovmsControlPlane,
    template_arn: str,
    cleanup: list[str],
) -> None:
    child, sandbox_id = spawn_creator(
        e2e_settings, template_arn, timeout=60, on_timeout="pause", idle=True
    )
    cleanup.append(sandbox_id)
    kill_client(child)
    started_at = control_plane.get_microvm(sandbox_id).started_at
    deadline = started_at + timedelta(seconds=60)
    wait_for(
        control_plane,
        sandbox_id,
        is_suspended,
        budget=(deadline - datetime.now(UTC)).total_seconds()
        + DEFAULT_MAX_IDLE_SECONDS
        + IDLE_SLACK_SECONDS,
    )
    report("Q64 deadline -> SUSPENDED with a dead client (s)", seconds_since(deadline))


def test_pause_mode_without_auto_resume(
    e2e_settings: E2ESettings,
    control_plane: LambdaMicrovmsControlPlane,
    template_arn: str,
    cleanup: list[str],
) -> None:
    sandbox = create_timed(
        e2e_settings,
        template_arn,
        timeout=60,
        on_timeout="pause",
        idle=IdlePolicy(max_idle_seconds=SHORT_MAX_IDLE_SECONDS, auto_resume=False),
    )
    cleanup.append(sandbox.sandbox_id)
    token = sandbox.access_token
    deadline = deadline_of(sandbox)
    wait_for(
        control_plane,
        sandbox.sandbox_id,
        is_suspended,
        budget=(deadline - datetime.now(UTC)).total_seconds() + SUSPEND_AFTER_DEADLINE_SECONDS,
    )
    sandbox.close()
    error = raw_start_error(control_plane, sandbox.sandbox_id, token)
    assert error.code() is grpc.StatusCode.FAILED_PRECONDITION
    assert error.details() == "sandbox_timeout"
    stray_at = datetime.now(UTC)
    wait_for(
        control_plane,
        sandbox.sandbox_id,
        is_suspended,
        budget=SHORT_MAX_IDLE_SECONDS + IDLE_SLACK_SECONDS,
    )
    report("stray request -> SUSPENDED again (s)", seconds_since(stray_at))
    with Sandbox.connect(
        sandbox.sandbox_id, access_token=token, control_plane=control_plane
    ) as reopened:
        assert reopened.run_code("1 + 1").text == "2"


def test_lifecycle_tracks_every_set_timeout(
    e2e_settings: E2ESettings, template_arn: str, cleanup: list[str]
) -> None:
    with create_timed(e2e_settings, template_arn, timeout=120) as sandbox:
        cleanup.append(sandbox.sandbox_id)
        for seconds in (200, 90, 400):
            sandbox.set_timeout(seconds)
            expected = datetime.now(UTC) + timedelta(seconds=seconds)
            health = sandbox.get_health().lifecycle
            assert health is not None and health.deadline is not None
            assert abs((health.deadline - expected).total_seconds()) < TRACKING_TOLERANCE_SECONDS
            info = sandbox.get_info()
            assert abs((info.expires_at - expected).total_seconds()) < TRACKING_TOLERANCE_SECONDS
        lifecycle = sandbox.get_health().lifecycle
        assert lifecycle is not None and lifecycle.extensions == 3


def test_older_agent_gate(
    e2e_settings: E2ESettings,
    control_plane: LambdaMicrovmsControlPlane,
    template_arn: str,
    cleanup: list[str],
) -> None:
    pre_m9 = os.environ.get(PRE_M9_VERSION_VAR)
    if not pre_m9:
        pytest.skip(f"exporta {PRE_M9_VERSION_VAR}=<versión anterior a M9> para la puerta")
    before = {item.sandbox_id for item in control_plane.list_microvms(image_arn=template_arn)}
    with pytest.raises(UnimplementedError):
        rayito.e2b.Sandbox.create(template_arn, template_version=pre_m9, max_lifetime=900)
    leftover = {
        item.sandbox_id for item in control_plane.list_microvms(image_arn=template_arn)
    } - before
    for sandbox_id in leftover:
        cleanup.append(sandbox_id)
        wait_for(control_plane, sandbox_id, is_terminal, budget=TERMINATION_BUDGET_SECONDS)
    with Sandbox.create(template_arn, template_version=pre_m9, timeout=300, idle=None) as legacy:
        cleanup.append(legacy.sandbox_id)
        assert legacy.commands.run("echo ok").stdout == "ok\n"
        assert legacy.get_health().lifecycle is None


def test_e2b_shim_timeout(
    e2e_settings: E2ESettings,
    control_plane: LambdaMicrovmsControlPlane,
    template_arn: str,
    cleanup: list[str],
) -> None:
    shim = rayito.e2b.Sandbox.create(
        template_arn, timeout=30, max_lifetime=900, template_version=e2e_settings.template_version
    )
    cleanup.append(shim.sandbox_id)
    created = shim.native.launch_info.started_at
    time.sleep(max(0.0, 5 - seconds_since(created)))
    shim.set_timeout(90)
    extended_at = datetime.now(UTC)
    time.sleep(max(0.0, 60 - seconds_since(created)))
    assert shim.commands.run("echo alive").stdout == "alive\n"
    shim.native.close()
    info = wait_for(
        control_plane,
        shim.sandbox_id,
        is_terminated,
        budget=90 + TERMINATION_BUDGET_SECONDS + 5 - seconds_since(extended_at),
    )
    report_termination(info, extended_at + timedelta(seconds=90))

    paused = rayito.e2b.Sandbox.create(
        template_arn,
        timeout=60,
        max_lifetime=900,
        template_version=e2e_settings.template_version,
        lifecycle={"on_timeout": "pause", "auto_resume": True},
    )
    cleanup.append(paused.sandbox_id)
    try:
        lifecycle = paused.native.get_health().lifecycle
        assert lifecycle is not None
        assert (lifecycle.on_timeout, lifecycle.auto_resume) == ("pause", True)
        idle = paused.native.launch_info.idle
        assert idle is not None and idle.auto_resume is True
    finally:
        paused.kill()

    beta = rayito.e2b.Sandbox.beta_create(
        template_arn,
        timeout=60,
        max_lifetime=900,
        template_version=e2e_settings.template_version,
        auto_pause=True,
    )
    cleanup.append(beta.sandbox_id)
    try:
        lifecycle = beta.native.get_health().lifecycle
        assert lifecycle is not None and lifecycle.on_timeout == "pause"
    finally:
        beta.kill()
