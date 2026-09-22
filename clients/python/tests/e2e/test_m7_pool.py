"""M7 Track 3 (`m7-suspended-pool`, ADR-008): el pool de sandboxes
suspendidos contra AWS real, con la regla de aceptación fijada antes de
medir (design D17): p95 de 20 `take()` → `run_code("1+1")` **< 1,5 s**,
`hits == 20`, `misses == 0`; un reciclado antes del muro; una recuperación
desde el backend JSON. Cada VM vive segundos y todo se termina al salir
(≈ 46 lanzamientos, ≈ $0,25). Los tiempos se imprimen con el nombre que usan
`MILESTONES.md` y `AWS_API_NOTES.md` Q53."""

from __future__ import annotations

import contextlib
import math
import socket
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from rayito import JsonFilePoolBackend, PoolConfig, Sandbox, SandboxPool
from rayito._aws import LambdaMicrovmsControlPlane
from rayito._models import IdlePolicy
from rayito.exceptions import SandboxNotFoundException

from .conftest import E2ESettings

pytestmark = pytest.mark.e2e

TAKES = 20
CREATES = 20
POOL_SIZE = 3
POOL_TIMEOUT_SECONDS = 900
POOL_MIN_REMAINING_SECONDS = 300
POOL_SWEEP_SECONDS = 10
POOL_FILL_CONCURRENCY = 3
POOL_IDLE = IdlePolicy(max_idle_seconds=120, auto_resume=True)
TAKE_P95_BUDGET_SECONDS = 1.5
READY_BUDGET_SECONDS = 90.0
CELL_TIMEOUT_SECONDS = 60
RECYCLE_TIMEOUT_SECONDS = 720
RECYCLE_MIN_REMAINING_SECONDS = 600
RECYCLE_BUDGET_SECONDS = 240.0
REPLACEMENT_BUDGET_SECONDS = 60.0
TERMINAL_BUDGET_SECONDS = 60.0
LIVE_STATES = frozenset({"PENDING", "RUNNING", "SUSPENDING", "SUSPENDED"})
POLL_SECONDS = 0.1
RTT_PROBES = 5


def report(label: str, value: str) -> None:
    print(f"\n[m7-pool] {label}: {value}", flush=True)


def percentile(samples: list[float], fraction: float) -> float:
    """Nearest-rank como en `scripts/bench_cold_start.py`: `sorted[ceil(f * n) - 1]`."""
    ordered = sorted(samples)
    rank = max(1, math.ceil(fraction * len(ordered)))
    return ordered[rank - 1]


def summary(label: str, samples: list[float]) -> float:
    p95 = percentile(samples, 0.95)
    report(
        label,
        f"p50 {percentile(samples, 0.5):.3f} s / p95 {p95:.3f} s / min {min(samples):.3f} s / "
        f"max {max(samples):.3f} s (n={len(samples)})",
    )
    return p95


def wait_until(predicate: Callable[[], bool], budget: float, what: str) -> float:
    started = time.perf_counter()
    while not predicate():
        elapsed = time.perf_counter() - started
        assert elapsed < budget, f"{what} no ocurrió en {budget:g} s"
        time.sleep(POLL_SECONDS)
    return time.perf_counter() - started


def client_rtt_ms(endpoint_host: str) -> float:
    """RTT TCP al proxy del MicroVM (443): el contexto de `T_take`."""
    samples: list[float] = []
    for _ in range(RTT_PROBES):
        started = time.perf_counter()
        with contextlib.suppress(OSError), socket.create_connection((endpoint_host, 443), 5):
            pass
        samples.append((time.perf_counter() - started) * 1000)
    return percentile(samples, 0.5)


def pool_config(e2e_settings: E2ESettings, template_arn: str, **overrides: object) -> PoolConfig:
    fields: dict[str, object] = {
        "size": POOL_SIZE,
        "template": template_arn,
        "template_version": e2e_settings.template_version,
        "timeout": POOL_TIMEOUT_SECONDS,
        "idle": POOL_IDLE,
        "min_remaining_seconds": POOL_MIN_REMAINING_SECONDS,
        "fill_concurrency": POOL_FILL_CONCURRENCY,
        "sweep_interval_seconds": POOL_SWEEP_SECONDS,
        "execution_role_arn": e2e_settings.execution_role_arn,
        "ingress": ["ALL_INGRESS"],
        "logging": e2e_settings.logging,
    }
    fields.update(overrides)
    return PoolConfig(**fields)  # type: ignore[arg-type]


def first_cell(sandbox: Sandbox) -> None:
    execution = sandbox.run_code("1+1", timeout=CELL_TIMEOUT_SECONDS)
    assert execution.text == "2", execution


def live_ids(plane: LambdaMicrovmsControlPlane, template_arn: str) -> set[str]:
    return {
        item.sandbox_id
        for item in plane.list_microvms(image_arn=template_arn)
        if item.state in LIVE_STATES
    }


def test_pool_take_latency(
    e2e_settings: E2ESettings, control_plane: LambdaMicrovmsControlPlane, template_arn: str
) -> None:
    """Regla D17: 20 `take()` con plaza lista frente a 20 `create()`."""
    used_ids: list[str] = []
    take_samples: list[float] = []
    create_samples: list[float] = []
    endpoint_host = ""
    with SandboxPool(pool_config(e2e_settings, template_arn), control_plane=control_plane) as pool:
        for index in range(TAKES):
            wait_until(lambda: pool.stats().ready >= 1, READY_BUDGET_SECONDS, "plaza lista")
            started = time.perf_counter()
            sandbox = pool.take()
            first_cell(sandbox)
            elapsed = time.perf_counter() - started
            take_samples.append(elapsed)
            used_ids.append(sandbox.sandbox_id)
            endpoint_host = sandbox.endpoint
            print(f"\n[m7-pool] take {index + 1:02d}: {elapsed:.3f} s ({sandbox.sandbox_id})")
            sandbox.kill()
        stats = pool.stats()
        report("stats tras 20 tomas", repr(stats))
        for index in range(CREATES):
            started = time.perf_counter()
            sandbox = Sandbox.create(
                template_arn,
                template_version=e2e_settings.template_version,
                timeout=POOL_TIMEOUT_SECONDS,
                idle=POOL_IDLE,
                execution_role_arn=e2e_settings.execution_role_arn,
                ingress=["ALL_INGRESS"],
                logging=e2e_settings.logging,
                control_plane=control_plane,
            )
            first_cell(sandbox)
            elapsed = time.perf_counter() - started
            create_samples.append(elapsed)
            used_ids.append(sandbox.sandbox_id)
            print(f"\n[m7-pool] create {index + 1:02d}: {elapsed:.3f} s ({sandbox.sandbox_id})")
            sandbox.kill()
        used_ids.extend(slot.sandbox_id for slot in pool.stats().slots)
    take_p95 = summary("T_take (take() -> primera celda)", take_samples)
    create_p95 = summary("T_create (create() -> primera celda)", create_samples)
    report("RTT del cliente al proxy (p50)", f"{client_rtt_ms(endpoint_host):.0f} ms")
    report("stats finales", repr(stats))

    assert stats.hits == TAKES, stats
    assert stats.misses == 0, stats
    assert take_p95 < TAKE_P95_BUDGET_SECONDS, f"T_take p95 {take_p95:.3f} s >= 1.5 s"
    assert create_p95 > take_p95
    wait_until(
        lambda: live_ids(control_plane, template_arn).isdisjoint(used_ids),
        TERMINAL_BUDGET_SECONDS,
        "ningún VM vivo de los usados",
    )


def test_pool_recycles_before_the_wall(
    e2e_settings: E2ESettings, control_plane: LambdaMicrovmsControlPlane, template_arn: str
) -> None:
    """Una plaza con `timeout=720` y `min_remaining_seconds=600` cruza la
    línea ≈ 110 s después de aparcar y el sweeper la recicla."""
    config = pool_config(
        e2e_settings,
        template_arn,
        size=1,
        timeout=RECYCLE_TIMEOUT_SECONDS,
        min_remaining_seconds=RECYCLE_MIN_REMAINING_SECONDS,
        fill_concurrency=1,
    )
    with SandboxPool(config, control_plane=control_plane) as pool:
        wait_until(lambda: pool.stats().ready == 1, READY_BUDGET_SECONDS, "plaza lista")
        first = pool.stats().slots[0].sandbox_id
        report("primera plaza", first)
        recycled_after = wait_until(
            lambda: pool.stats().recycled == 1, RECYCLE_BUDGET_SECONDS, "reciclado"
        )
        report("reciclado tras", f"{recycled_after:.1f} s desde ready")
        wait_until(
            lambda: any(
                slot.state == "ready" and slot.sandbox_id != first for slot in pool.stats().slots
            ),
            REPLACEMENT_BUDGET_SECONDS,
            "plaza de reemplazo",
        )
        replacement = next(s.sandbox_id for s in pool.stats().slots if s.state == "ready")
        report("plaza de reemplazo", replacement)
        wait_until(
            lambda: first_is_terminal(control_plane, first), TERMINAL_BUDGET_SECONDS, "terminal"
        )
        report("stats", repr(pool.stats()))
    assert replacement != first


def first_is_terminal(plane: LambdaMicrovmsControlPlane, sandbox_id: str) -> bool:
    try:
        return plane.get_microvm(sandbox_id).state in ("TERMINATING", "TERMINATED")
    except SandboxNotFoundException:
        return True


def test_pool_json_backend_recovery(
    e2e_settings: E2ESettings,
    control_plane: LambdaMicrovmsControlPlane,
    template_arn: str,
    tmp_path: Path,
) -> None:
    """`close(drain=False)` sobre un backend JSON y un pool nuevo que
    recupera la misma plaza sin lanzar nada."""
    path = tmp_path / "pool.json"
    config = pool_config(e2e_settings, template_arn, size=1, fill_concurrency=1)
    first = SandboxPool(config, backend=JsonFilePoolBackend(path), control_plane=control_plane)
    first.start()
    wait_until(lambda: first.stats().ready == 1, READY_BUDGET_SECONDS, "plaza lista")
    parked = first.stats().slots[0].sandbox_id
    first.close(drain=False)
    report("plaza aparcada y dejada en el JSON", parked)

    with SandboxPool(
        config, backend=JsonFilePoolBackend(path), control_plane=control_plane
    ) as second:
        stats = second.stats()
        assert stats.ready == 1 and stats.launched == 0, stats
        assert stats.slots[0].sandbox_id == parked
        started = time.perf_counter()
        sandbox = second.take()
        first_cell(sandbox)
        report("take() recuperado -> primera celda", f"{time.perf_counter() - started:.3f} s")
        assert sandbox.sandbox_id == parked
        sandbox.kill()
        report("stats", repr(second.stats()))
    wait_until(
        lambda: parked not in live_ids(control_plane, template_arn),
        TERMINAL_BUDGET_SECONDS,
        "la plaza recuperada terminada",
    )
