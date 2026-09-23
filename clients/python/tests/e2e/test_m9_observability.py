"""M9 `m9-sandbox-observability` contra AWS real (design D14): el historial
de métricas de 5 s con su hueco durante una pausa, el muestreador que no
impide la auto-suspensión por idle, el listado paginado (`limit`,
`next_token`, `order`, filtros de cliente) y los hechos del guest de
`get_info()` frente al `minimumMemoryInMiB` de la versión de imagen.

Necesita una versión de `rayito-base` publicada desde este árbol (imagen
M9) en `RAYITO_TEMPLATE`/`RAYITO_TEMPLATE_VERSION`; contra una imagen
anterior el historial responde `UNIMPLEMENTED` y los hechos del guest son
`None`. Imprime `samples`, `gap_s`, `history_rpc_s`, `idle_suspend_s`, `idle_gap_s`,
`pages`, `walk_s` y los números de la fila §16 de `AWS_API_NOTES.md`.
"""

from __future__ import annotations

import contextlib
import itertools
import os
import time
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

import boto3
import pytest

import rayito.e2b
from rayito import IdlePolicy, ListOrder, Sandbox, SandboxListPaginator, SandboxMetrics
from rayito._aws import LambdaMicrovmsControlPlane
from rayito._sandbox_base import ACCESS_TOKEN_ENV_VAR
from rayito.e2b import SandboxQuery, SandboxState
from rayito.exceptions import SandboxNotFoundException, UnimplementedError

from .conftest import BootTimings, E2ESettings, create_test_sandbox

pytestmark = pytest.mark.e2e

SAMPLE_INTERVAL_S = 5.0
SAMPLE_TOLERANCE_S = 1.5
REGULAR_DELTA_SHARE = 0.9
PAUSE_AT_S = 90.0
PAUSED_FOR_S = 30.0
WINDOW_S = 300.0
MIN_WINDOW_SAMPLES = 45
GAP_MIN_S = 30.0
CLOCK_TOLERANCE_S = 2.0
PAGE_CACHE_BYTES = 16 * 1024 * 1024
DOWNSAMPLED_POINTS = 10
IDLE_SECONDS = 60
IDLE_SUSPEND_BUDGET_S = IDLE_SECONDS + 120
STATE_POLL_S = 5.0
CREATION_SPACING_S = 2.0
LISTED_SANDBOXES = 3
POST_RESUME_SETTLE_S = 4 * SAMPLE_INTERVAL_S
SUSPENDED_HOLD_S = GAP_MIN_S + 2 * SAMPLE_INTERVAL_S


def report(label: str, value: object) -> None:
    print(f"\n[m9-observability] {label}: {value}", flush=True)


def now() -> datetime:
    return datetime.now(UTC)


def sleep_until(moment: datetime) -> None:
    remaining = (moment - now()).total_seconds()
    if remaining > 0:
        time.sleep(remaining)


def deltas(samples: list[SandboxMetrics]) -> list[float]:
    return [
        (later.timestamp - earlier.timestamp).total_seconds()
        for earlier, later in itertools.pairwise(samples)
    ]


@dataclass(frozen=True)
class Gap:
    start: datetime
    end: datetime

    @property
    def seconds(self) -> float:
        return (self.end - self.start).total_seconds()

    def covers(self, first: datetime, last: datetime) -> bool:
        tolerance = timedelta(seconds=CLOCK_TOLERANCE_S)
        return self.start <= first + tolerance and self.end >= last - tolerance


def gaps(samples: list[SandboxMetrics]) -> list[Gap]:
    return [
        Gap(earlier.timestamp, later.timestamp)
        for earlier, later in itertools.pairwise(samples)
        if (later.timestamp - earlier.timestamp).total_seconds() >= GAP_MIN_S
    ]


def assert_regular_series(samples: list[SandboxMetrics], start: datetime, end: datetime) -> None:
    stamps = [sample.timestamp for sample in samples]
    assert all(start <= stamp <= end for stamp in stamps)
    assert all(earlier < later for earlier, later in itertools.pairwise(stamps))
    regular = [
        delta for delta in deltas(samples) if abs(delta - SAMPLE_INTERVAL_S) <= SAMPLE_TOLERANCE_S
    ]
    assert len(regular) >= REGULAR_DELTA_SHARE * (len(samples) - 1 - len(gaps(samples)))


def fill_page_cache(sbx: Sandbox) -> None:
    sbx.files.write("/home/user/m9-page-cache.bin", os.urandom(PAGE_CACHE_BYTES))


def pause_and_resume(sbx: Sandbox, t0: datetime) -> tuple[datetime, datetime]:
    """Devuelve el instante en que `pause()` volvió (VM congelada) y el
    instante justo antes de `resume()`: el hueco tiene que cubrir ambos."""
    sleep_until(t0 + timedelta(seconds=PAUSE_AT_S))
    assert sbx.pause() is True
    paused_at = now()
    time.sleep(PAUSED_FOR_S)
    resume_called_at = now()
    sbx.resume()
    return paused_at, resume_called_at


def timed_history(
    sbx: Sandbox, start: datetime, end: datetime
) -> tuple[list[SandboxMetrics], float]:
    started = time.perf_counter()
    samples = sbx.get_metrics_history(start=start, end=end)
    return samples, time.perf_counter() - started


def check_shim_metrics(
    sbx: Sandbox, e2e_settings: E2ESettings, window: tuple[datetime, datetime], expected: int
) -> None:
    """La forma E2B: estática con el access token (y sin él
    `UnimplementedError` nombrándolo) y de instancia con `start`/`end`."""
    start, end = window
    static = rayito.e2b.Sandbox.get_metrics(
        sbx.sandbox_id, access_token=sbx.access_token, region=e2e_settings.region
    )
    assert isinstance(static, list) and static
    with pytest.MonkeyPatch.context() as patch:
        patch.delenv(ACCESS_TOKEN_ENV_VAR, raising=False)
        with pytest.raises(UnimplementedError, match="access token"):
            rayito.e2b.Sandbox.get_metrics(sbx.sandbox_id, region=e2e_settings.region)
    with rayito.e2b.Sandbox.connect(
        sbx.sandbox_id, access_token=sbx.access_token, region=e2e_settings.region
    ) as shim:
        ranged = shim.get_metrics(start=start, end=end)
    assert len(ranged) == expected
    assert max(asdict(sample)["mem_cache"] for sample in ranged) > 0


def test_metrics_history_across_pause(sandbox: Sandbox, e2e_settings: E2ESettings) -> None:
    t0 = now()
    fill_page_cache(sandbox)
    paused_at, resume_called_at = pause_and_resume(sandbox, t0)
    end = t0 + timedelta(seconds=WINDOW_S)
    sleep_until(end + timedelta(seconds=CLOCK_TOLERANCE_S))

    history, history_rpc_s = timed_history(sandbox, t0, end)
    report("samples", len(history))
    report("history_rpc_s", f"{history_rpc_s:.3f}")
    assert len(history) >= MIN_WINDOW_SAMPLES
    assert_regular_series(history, t0, end)
    found = gaps(history)
    assert len(found) == 1
    report("gap_s", f"{found[0].seconds:.1f}")
    assert found[0].covers(paused_at, resume_called_at)
    assert max(sample.mem_cache_bytes for sample in history) > 0

    window_start, window_end = t0 + timedelta(seconds=120), t0 + timedelta(seconds=180)
    windowed = sandbox.get_metrics_history(start=window_start, end=window_end)
    assert windowed and all(window_start <= s.timestamp <= window_end for s in windowed)
    reduced = sandbox.get_metrics_history(max_points=DOWNSAMPLED_POINTS)
    assert len(reduced) == DOWNSAMPLED_POINTS
    assert all(a.timestamp < b.timestamp for a, b in itertools.pairwise(reduced))

    static = Sandbox.get_metrics_history(
        sandbox.sandbox_id,
        access_token=sandbox.access_token,
        start=t0,
        end=end,
        region=e2e_settings.region,
    )
    assert len(static) == len(history)
    check_shim_metrics(sandbox, e2e_settings, (t0, end), len(history))


def wait_for_idle_suspend(control_plane: LambdaMicrovmsControlPlane, sandbox_id: str) -> float:
    """Sondea `get-microvm` (plano de control, nunca el endpoint) hasta
    `SUSPENDED`; devuelve los segundos desde la llamada."""
    started = time.perf_counter()
    deadline = started + IDLE_SUSPEND_BUDGET_S
    while time.perf_counter() < deadline:
        if control_plane.get_microvm(sandbox_id).state == "SUSPENDED":
            return time.perf_counter() - started
        time.sleep(STATE_POLL_S)
    pytest.fail(f"{sandbox_id} no se auto-suspendió en {IDLE_SUSPEND_BUDGET_S} s")


def test_sampler_does_not_block_idle_suspend(
    e2e_settings: E2ESettings,
    control_plane: LambdaMicrovmsControlPlane,
    template_arn: str,
    boot_timings: BootTimings,
) -> None:
    """El sandbox se queda `SUSPENDED` `SUSPENDED_HOLD_S` antes de
    reanudarlo: reanudado nada más verlo, la suspensión dura segundos
    (`suspended_ms` ≈ 4 s en `rayd`) y el hueco no llega a `GAP_MIN_S`."""
    created = create_test_sandbox(
        e2e_settings,
        control_plane,
        template_arn,
        boot_timings,
        idle=IdlePolicy(max_idle_seconds=IDLE_SECONDS),
    )
    sandbox_id, token = created.sandbox_id, created.access_token
    created.close()
    try:
        idle_started = now()
        idle_suspend_s = wait_for_idle_suspend(control_plane, sandbox_id)
        suspended_seen = now()
        report("idle_suspend_s", f"{idle_suspend_s:.1f}")
        time.sleep(SUSPENDED_HOLD_S)
        resume_called_at = now()
        resumed = Sandbox.connect(sandbox_id, access_token=token, control_plane=control_plane)
        try:
            time.sleep(POST_RESUME_SETTLE_S)
            history = resumed.get_metrics_history(start=idle_started - timedelta(seconds=30))
        finally:
            resumed.close()
        covering = [gap for gap in gaps(history) if gap.covers(suspended_seen, resume_called_at)]
        assert covering
        report("idle_gap_s", f"{covering[0].seconds:.1f}")
    finally:
        with contextlib.suppress(SandboxNotFoundException):
            control_plane.terminate_microvm(sandbox_id)


@dataclass(frozen=True)
class ListedFleet:
    ids: tuple[str, ...]
    started: tuple[datetime, ...]

    @property
    def before_first(self) -> datetime:
        return self.started[0] - timedelta(seconds=1)

    @property
    def between_first_and_second(self) -> datetime:
        return self.started[0] + (self.started[1] - self.started[0]) / 2


@pytest.fixture
def listed_fleet(
    e2e_settings: E2ESettings,
    control_plane: LambdaMicrovmsControlPlane,
    template_arn: str,
    boot_timings: BootTimings,
) -> Iterator[ListedFleet]:
    """Tres sandboxes creados con 2 s de separación; el tercero, pausado.
    Los límites de tiempo salen del `startedAt` de AWS, no del reloj local."""
    created: list[Sandbox] = []
    try:
        for _ in range(LISTED_SANDBOXES):
            if created:
                time.sleep(CREATION_SPACING_S)
            created.append(
                create_test_sandbox(e2e_settings, control_plane, template_arn, boot_timings)
            )
        assert created[-1].pause() is True
        yield ListedFleet(
            ids=tuple(sbx.sandbox_id for sbx in created),
            started=tuple(sbx.launch_info.started_at for sbx in created),
        )
    finally:
        for sbx in created:
            with contextlib.suppress(SandboxNotFoundException):
                sbx.kill()


def drain(paginator: SandboxListPaginator) -> tuple[list[str], int]:
    served: list[str] = []
    pages = 0
    while paginator.has_next:
        served.extend(item.sandbox_id for item in paginator.next_items())
        pages += 1
    return served, pages


def only(ids: list[str], fleet: ListedFleet) -> list[str]:
    return [sandbox_id for sandbox_id in ids if sandbox_id in fleet.ids]


def check_resumed_walk(
    template_arn: str, fleet: ListedFleet, control_plane: LambdaMicrovmsControlPlane
) -> None:
    """Una página de un paginador y el resto desde un `paginate()` nuevo con
    su `next_token`: cada sandbox una sola vez."""
    first_page = Sandbox.paginate(
        template=template_arn,
        started_after=fleet.before_first,
        limit=1,
        control_plane=control_plane,
    )
    first = [item.sandbox_id for item in first_page.next_items()]
    rest, _ = drain(
        Sandbox.paginate(
            template=template_arn,
            started_after=fleet.before_first,
            limit=1,
            next_token=first_page.next_token,
            control_plane=control_plane,
        )
    )
    union = first + rest
    assert len(union) == len(set(union))
    assert sorted(only(union, fleet)) == sorted(fleet.ids)


def ordered_ids(
    template_arn: str, fleet: ListedFleet, order: ListOrder, plane: LambdaMicrovmsControlPlane
) -> list[str]:
    items = Sandbox.list(
        template=template_arn, started_after=fleet.before_first, order=order, control_plane=plane
    )
    return only([item.sandbox_id for item in items], fleet)


def test_pagination_order_and_filters(
    listed_fleet: ListedFleet,
    control_plane: LambdaMicrovmsControlPlane,
    template_arn: str,
    e2e_settings: E2ESettings,
) -> None:
    fleet = listed_fleet
    started = time.perf_counter()
    paginator = Sandbox.paginate(
        template=template_arn,
        started_after=fleet.before_first,
        limit=1,
        control_plane=control_plane,
    )
    assert paginator.has_next
    walked, pages = drain(paginator)
    report("pages", pages)
    report("walk_s", f"{time.perf_counter() - started:.2f}")
    assert len(walked) == len(set(walked))
    assert sorted(only(walked, fleet)) == sorted(fleet.ids)
    check_resumed_walk(template_arn, fleet, control_plane)

    assert ordered_ids(template_arn, fleet, "asc", control_plane) == list(fleet.ids)
    assert ordered_ids(template_arn, fleet, "desc", control_plane) == list(reversed(fleet.ids))

    paused = Sandbox.list(
        template=template_arn, states=["SUSPENDING", "SUSPENDED"], control_plane=control_plane
    )
    assert only([item.sandbox_id for item in paused], fleet) == [fleet.ids[-1]]
    shim_paused = rayito.e2b.Sandbox.list(
        SandboxQuery(state=[SandboxState.PAUSED], template=template_arn),
        region=e2e_settings.region,
    )
    shim_ids = [info.sandbox_id for info in shim_paused.next_items()]
    assert only(shim_ids, fleet) == [fleet.ids[-1]]

    recent = Sandbox.list(
        template=template_arn,
        started_after=fleet.between_first_and_second,
        control_plane=control_plane,
    )
    assert sorted(only([item.sandbox_id for item in recent], fleet)) == sorted(fleet.ids[1:])
    every = list(Sandbox.list(template=template_arn, control_plane=control_plane))
    assert all(item.template == template_arn for item in every)


def guest_meminfo_mib(sbx: Sandbox) -> int:
    line = sbx.commands.run("grep '^MemTotal:' /proc/meminfo").stdout.split()
    return int(line[1]) // 1024


def image_minimum_memory_mib(arn: str, version: str, region: str | None) -> int:
    client = boto3.session.Session(region_name=region).client("lambda-microvms")
    response = client.get_microvm_image_version(imageIdentifier=arn, imageVersion=version)
    return int(response["resources"][0]["minimumMemoryInMiB"])


def test_guest_resources_vs_image_tier(
    sandbox: Sandbox, template_arn: str, e2e_settings: E2ESettings
) -> None:
    info = sandbox.get_info()
    nproc = int(sandbox.commands.run("nproc").stdout.strip())
    assert info.agent_version
    assert info.cpu_count is not None and 1 <= info.cpu_count <= nproc
    assert info.memory_mb == guest_meminfo_mib(sandbox)
    minimum = image_minimum_memory_mib(template_arn, info.template_version, e2e_settings.region)
    report("guest_cpu_count", info.cpu_count)
    report("guest_memory_mb", info.memory_mb)
    report("nproc", nproc)
    report("minimumMemoryInMiB", minimum)
