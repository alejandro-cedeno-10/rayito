"""Dominio puro del panel de gestión (ADR-026, cambio OpenSpec `dashboard`):
cuotas de Lambda MicroVMs con sus avisos, coste facturado y estimado, vistas
de sandboxes e imágenes y planificación de acciones masivas. Nada aquí
importa `boto3`, una librería HTTP ni el código generado del proto: los
adaptadores traducen a estos modelos (`_aws.py`) y los servicios los
componen (`_quotas.py`, `_cost.py`, `_fleet.py`).

Los umbrales, el TTL de la caché de coste, el catálogo de cuotas y los
precios son constantes de `limits.json` (`_limits.py`); el mismo dominio
existe en TypeScript (`src/dashboard/domain.ts`) y ambos pasan los vectores
de `testdata/dashboard/`.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Final, Generic, Literal, TypeVar

from rayito._limits import (
    API_TPS,
    BASELINE_MIB_PER_VCPU,
    COMPUTE_PRICE_USD_PER_GB_SECOND,
    COMPUTE_PRICE_USD_PER_VCPU_SECOND,
    LARGE_QUOTA_REGIONS,
    QUOTA_CRITICAL_FRACTION,
    QUOTA_DEFAULTS,
    QUOTA_KINDS,
    QUOTA_LARGE_REGION_DEFAULTS,
    QUOTA_NAMES,
    QUOTA_OPERATIONS,
    QUOTA_SERVICE_CODE,
    QUOTA_WARNING_FRACTION,
    SUSPENDED_STATES,
)

QuotaLevel = Literal["ok", "warning", "critical"]
WarningKind = Literal["memory_high", "memory_critical", "below_default", "not_listed"]
BulkAction = Literal["kill", "suspend", "resume"]
BulkOutcome = Literal["done", "unchanged", "failed"]
Granularity = Literal["DAILY", "MONTHLY"]

#: Estados cuya memoria cuenta contra la cuota por región: `RUNNING` más los
#: suspendidos (`SUSPENDING` y `SUSPENDED`), AWS_API_NOTES.md §11.
MEMORY_COUNTED_STATES: Final = frozenset({"RUNNING"}) | SUSPENDED_STATES
RUNNING_STATE: Final = "RUNNING"
MIB_PER_GB: Final = 1024
PERCENT: Final = 100

MEMORY_KIND: Final = "memory-gb"
RATE_KIND: Final = "rate"
BURST_KIND: Final = "burst"
TPS_KINDS: Final = frozenset({RATE_KIND, BURST_KIND})

#: Formato de la URL de la consola de Service Quotas para pedir un aumento
#: (no es un parámetro de la API; sólo la partición `aws`). Pendiente de
#: confirmar en la aceptación de AWS (tarea 1.1 del cambio `dashboard`).
INCREASE_URL_TEMPLATE: Final = (
    "https://{region}.console.aws.amazon.com/servicequotas/home/services/{service}/quotas/{code}"
)

#: Cada `BulkAction` y la operación de AWS cuya cuota de TPS la limita
#: (AWS_API_NOTES.md §5, `limits.json` `apiTps`).
BULK_OPERATIONS: Final[dict[str, str]] = {
    "kill": "TerminateMicrovm",
    "suspend": "SuspendMicrovm",
    "resume": "ResumeMicrovm",
}

BILLED_LABEL: Final = "billed"
ESTIMATED_LABEL: Final = "estimated"
ESTIMATE_BASIS: Final = (
    "estimación: tiempo transcurrido desde el arranque de cada sandbox RUNNING por el precio "
    "de cómputo baseline; no incluye snapshots ni consumo sobre el baseline y no sustituye "
    "a la factura"
)
#: Región de los precios documentados que usa la estimación (AWS_API_NOTES.md §12).
PRICING_REGION: Final = "us-east-1"

#: Sufijo del usage type (`Lambda-MicroVM-<sufijo>`) de Cost Explorer y el
#: componente que factura, verificado el 2026-09-16 (AWS_API_NOTES.md §12).
COMPONENT_BY_USAGE_SUFFIX: Final[dict[str, str]] = {
    "Memory-GB-Second-ARM": "memory",
    "vCPU-Second-ARM": "vcpu",
    "Snapshot-Read-GB": "snapshot-read",
    "Snapshot-Write-GB": "snapshot-write",
    "Snapshot-Storage-GB-Hour": "snapshot-storage",
}
OTHER_COMPONENT: Final = "other"
#: Los usage types de otras regiones llevan un prefijo (`EUW1-Lambda-…`);
#: sin medir todavía fuera de us-east-1 (tarea 1.1).
MICROVM_USAGE_TYPE_PATTERN: Final = re.compile(r"^(?:[A-Z0-9]+-)?Lambda-MicroVM-(?P<suffix>.+)$")

K = TypeVar("K")
V = TypeVar("V")


# --------------------------------------------------------------------- quotas


@dataclass(frozen=True)
class AppliedQuota:
    """Una cuota tal como la devuelve Service Quotas (`ListServiceQuotas`)."""

    code: str
    name: str
    value: float
    adjustable: bool


def quota_default(code: str, region: str | None) -> int:
    """El valor por defecto de la cuota `code` en `region` (las cuatro
    regiones grandes parten de más memoria y builds, AWS_API_NOTES.md §11)."""
    if region in LARGE_QUOTA_REGIONS and code in QUOTA_LARGE_REGION_DEFAULTS:
        return QUOTA_LARGE_REGION_DEFAULTS[code]
    return QUOTA_DEFAULTS[code]


def increase_url(region: str, code: str) -> str:
    """El enlace de la consola para pedir un aumento de la cuota `code`."""
    return INCREASE_URL_TEMPLATE.format(region=region, service=QUOTA_SERVICE_CODE, code=code)


@dataclass(frozen=True)
class QuotaEntry:
    """Una cuota del catálogo con su valor aplicado (`None` si Service Quotas
    no la lista), el valor por defecto de la región y el enlace de aumento."""

    code: str
    name: str
    kind: str
    operation: str | None
    default: int
    value: float | None
    adjustable: bool
    increase_url: str

    @property
    def below_default(self) -> bool:
        return self.value is not None and self.value < self.default


def build_quota_entries(applied: Iterable[AppliedQuota], region: str) -> tuple[QuotaEntry, ...]:
    """Una entrada por cuota del catálogo, en el orden del catálogo."""
    by_code = {quota.code: quota for quota in applied}
    entries: list[QuotaEntry] = []
    for code in QUOTA_NAMES:
        quota = by_code.get(code)
        entries.append(
            QuotaEntry(
                code=code,
                name=QUOTA_NAMES[code],
                kind=QUOTA_KINDS[code],
                operation=QUOTA_OPERATIONS.get(code),
                default=quota_default(code, region),
                value=None if quota is None else quota.value,
                adjustable=False if quota is None else quota.adjustable,
                increase_url=increase_url(region, code),
            )
        )
    return tuple(entries)


@dataclass(frozen=True)
class TpsLimit:
    """La tasa y la ráfaga aplicadas de una operación (`None` si no se listan)."""

    operation: str
    rate: float | None
    burst: float | None


def tps_limits(entries: Iterable[QuotaEntry]) -> tuple[TpsLimit, ...]:
    """Las cuotas de TPS agrupadas por operación, en el orden del catálogo."""
    rates: dict[str, float | None] = {}
    bursts: dict[str, float | None] = {}
    for entry in entries:
        if entry.kind == RATE_KIND and entry.operation is not None:
            rates[entry.operation] = entry.value
        elif entry.kind == BURST_KIND and entry.operation is not None:
            bursts[entry.operation] = entry.value
    return tuple(
        TpsLimit(operation, rates.get(operation), bursts.get(operation))
        for operation in dict.fromkeys([*rates, *bursts])
    )


def classify_fraction(fraction: float | None) -> QuotaLevel:
    """`critical` y `warning` desde los umbrales de `limits.json`; sin cuota
    conocida no hay nivel que avisar."""
    if fraction is None:
        return "ok"
    if fraction >= QUOTA_CRITICAL_FRACTION:
        return "critical"
    if fraction >= QUOTA_WARNING_FRACTION:
        return "warning"
    return "ok"


@dataclass(frozen=True)
class MemoryUsage:
    """Memoria asignada (RUNNING más suspendidos) contra la cuota de la
    región. `unresolved` cuenta los sandboxes cuya memoria de imagen no se
    pudo leer: el uso real puede ser mayor."""

    allocated_gb: float
    quota_gb: float | None
    fraction: float | None
    level: QuotaLevel
    sandboxes: int
    unresolved: int


def memory_usage(memory_mib: Iterable[int | None], quota_gb: float | None) -> MemoryUsage:
    """`memory_mib` trae un elemento por sandbox contado (`None` si no se
    resolvió su memoria)."""
    samples = list(memory_mib)
    allocated_gb = sum(mib for mib in samples if mib is not None) / MIB_PER_GB
    fraction = None if not quota_gb else allocated_gb / quota_gb
    return MemoryUsage(
        allocated_gb=allocated_gb,
        quota_gb=quota_gb,
        fraction=fraction,
        level=classify_fraction(fraction),
        sandboxes=len(samples),
        unresolved=sum(1 for mib in samples if mib is None),
    )


@dataclass(frozen=True)
class QuotaWarning:
    """Un aviso con el enlace para pedir el aumento de `code`."""

    kind: WarningKind
    code: str
    message: str
    increase_url: str


def quota_warnings(entries: Iterable[QuotaEntry], memory: MemoryUsage) -> tuple[QuotaWarning, ...]:
    """Avisos de memoria cerca de la cuota, de cuotas por debajo del valor por
    defecto (las cuentas nuevas parten de cuotas reducidas) y de cuotas que
    Service Quotas no lista."""
    warnings: list[QuotaWarning] = []
    for entry in entries:
        if entry.kind == MEMORY_KIND and memory.level != "ok" and memory.fraction is not None:
            warnings.append(_memory_warning(entry, memory))
        if entry.value is None:
            warnings.append(
                QuotaWarning(
                    "not_listed",
                    entry.code,
                    f"{entry.name}: Service Quotas no la lista",
                    entry.increase_url,
                )
            )
        elif entry.below_default:
            warnings.append(
                QuotaWarning(
                    "below_default",
                    entry.code,
                    f"{entry.name}: {entry.value:g}, por debajo del valor por defecto "
                    f"({entry.default:g}); las cuentas nuevas parten de cuotas reducidas",
                    entry.increase_url,
                )
            )
    return tuple(warnings)


def _memory_warning(entry: QuotaEntry, memory: MemoryUsage) -> QuotaWarning:
    kind: WarningKind = "memory_critical" if memory.level == "critical" else "memory_high"
    percent = (memory.fraction or 0.0) * PERCENT
    return QuotaWarning(
        kind,
        entry.code,
        f"Memoria asignada al {percent:.0f} % de la cuota "
        f"({memory.allocated_gb:g} de {memory.quota_gb:g} GB): pide un aumento antes de "
        "lanzar más sandboxes",
        entry.increase_url,
    )


@dataclass(frozen=True)
class QuotaReport:
    """Cuotas aplicadas, uso de memoria y avisos de una región."""

    region: str
    entries: tuple[QuotaEntry, ...]
    memory: MemoryUsage
    warnings: tuple[QuotaWarning, ...]

    @property
    def tps(self) -> tuple[TpsLimit, ...]:
        return tps_limits(self.entries)

    def applied_rate(self, operation: str) -> float | None:
        """La tasa aplicada de `operation` (`SuspendMicrovm`…), o `None` si
        Service Quotas no la lista."""
        return next((limit.rate for limit in self.tps if limit.operation == operation), None)


def build_quota_report(
    region: str, applied: Iterable[AppliedQuota], memory_mib: Iterable[int | None]
) -> QuotaReport:
    entries = build_quota_entries(applied, region)
    memory_quota = next((entry.value for entry in entries if entry.kind == MEMORY_KIND), None)
    memory = memory_usage(memory_mib, memory_quota)
    return QuotaReport(region, entries, memory, quota_warnings(entries, memory))


# ----------------------------------------------------------------------- cost


@dataclass(frozen=True)
class CostGroup:
    """Un grupo `USAGE_TYPE` de un periodo de Cost Explorer."""

    usage_type: str
    cost_usd: float
    quantity: float
    unit: str


@dataclass(frozen=True)
class CostPeriod:
    """Un `ResultsByTime` de Cost Explorer; `estimated` es el indicador
    `Estimated` de AWS (el periodo aún puede cambiar)."""

    start: date
    end: date
    estimated: bool
    groups: tuple[CostGroup, ...]


@dataclass(frozen=True)
class CostQueryResult:
    """Los periodos de una consulta y las peticiones que costó (cada página
    de `GetCostAndUsage` se factura)."""

    periods: tuple[CostPeriod, ...]
    requests: int


@dataclass(frozen=True)
class BilledLine:
    usage_type: str
    component: str
    quantity: float
    unit: str
    cost_usd: float


@dataclass(frozen=True)
class BilledCost:
    """Coste facturado de MicroVMs según Cost Explorer, por usage type.
    `partial` es `True` si algún periodo aún es estimado por AWS (retraso de
    unas 24 h, AWS_API_NOTES.md §12); `requests` son las peticiones de pago
    que costó; `fetched_at` cuándo se leyó (la caché lo conserva)."""

    start: date
    end: date
    region: str | None
    lines: tuple[BilledLine, ...]
    total_usd: float
    partial: bool
    requests: int
    fetched_at: datetime
    label: Literal["billed"] = BILLED_LABEL


def microvm_component(usage_type: str) -> str | None:
    """El componente de un usage type de MicroVMs, o `None` si el usage type
    no es de MicroVMs (el servicio `AWS Lambda` factura más cosas)."""
    match = MICROVM_USAGE_TYPE_PATTERN.match(usage_type)
    if match is None:
        return None
    return COMPONENT_BY_USAGE_SUFFIX.get(match.group("suffix"), OTHER_COMPONENT)


def fold_billed_cost(
    result: CostQueryResult,
    *,
    start: date,
    end: date,
    region: str | None,
    fetched_at: datetime,
) -> BilledCost:
    """Suma por usage type de MicroVMs los periodos de `result`, sin tocar los
    de otros usage types de Lambda."""
    totals: dict[str, tuple[str, float, str, float]] = {}
    for period in result.periods:
        for group in period.groups:
            component = microvm_component(group.usage_type)
            if component is None:
                continue
            _, quantity, _, cost = totals.get(group.usage_type, (component, 0.0, group.unit, 0.0))
            totals[group.usage_type] = (
                component,
                quantity + group.quantity,
                group.unit,
                cost + group.cost_usd,
            )
    lines = tuple(
        BilledLine(usage_type, component, quantity, unit, cost)
        for usage_type, (component, quantity, unit, cost) in sorted(totals.items())
    )
    return BilledCost(
        start=start,
        end=end,
        region=region,
        lines=lines,
        total_usd=sum(line.cost_usd for line in lines),
        partial=any(period.estimated for period in result.periods),
        requests=result.requests,
        fetched_at=fetched_at,
    )


def estimate_compute_usd(memory_mib: int, active_seconds: float) -> float:
    """Cómputo baseline de un sandbox de `memory_mib` activo `active_seconds`:
    un vCPU por cada `BASELINE_MIB_PER_VCPU` MiB más los GB-s de memoria, a
    los precios de `limits.json` (AWS_API_NOTES.md §12: 2 GB cuestan
    $0,1261/h)."""
    vcpus = memory_mib / BASELINE_MIB_PER_VCPU
    gigabytes = memory_mib / MIB_PER_GB
    return active_seconds * (
        vcpus * COMPUTE_PRICE_USD_PER_VCPU_SECOND + gigabytes * COMPUTE_PRICE_USD_PER_GB_SECOND
    )


@dataclass(frozen=True)
class SandboxCostEstimate:
    """Coste estimado de un sandbox. Sólo los `RUNNING` con memoria conocida
    llevan importe: de uno suspendido no se conoce el tiempo activo."""

    sandbox_id: str
    state: str
    memory_mib: int | None
    active_seconds: float | None
    cost_usd: float | None
    label: Literal["estimated"] = ESTIMATED_LABEL


@dataclass(frozen=True)
class CostEstimate:
    """La estimación por sandbox y su total, siempre rotulada `estimated`."""

    items: tuple[SandboxCostEstimate, ...]
    total_usd: float
    pricing_region: str = PRICING_REGION
    basis: str = ESTIMATE_BASIS
    label: Literal["estimated"] = ESTIMATED_LABEL


def estimate_sandbox_cost(
    sandbox_id: str,
    state: str,
    started_at: datetime,
    memory_mib: int | None,
    now: datetime,
) -> SandboxCostEstimate:
    if state != RUNNING_STATE or memory_mib is None:
        return SandboxCostEstimate(sandbox_id, state, memory_mib, None, None)
    active_seconds = max(0.0, (now - started_at).total_seconds())
    return SandboxCostEstimate(
        sandbox_id,
        state,
        memory_mib,
        active_seconds,
        estimate_compute_usd(memory_mib, active_seconds),
    )


def build_cost_estimate(items: Iterable[SandboxCostEstimate]) -> CostEstimate:
    listed = tuple(items)
    return CostEstimate(listed, sum(item.cost_usd or 0.0 for item in listed))


class TtlCache(Generic[K, V]):
    """Caché en memoria con caducidad, con reloj inyectable. Segura entre
    hilos: los servicios síncronos pueden compartirla."""

    def __init__(self, ttl_seconds: float, clock: Callable[[], float]) -> None:
        self._ttl = ttl_seconds
        self._clock = clock
        self._entries: dict[K, tuple[float, V]] = {}
        self._lock = threading.Lock()

    def get(self, key: K) -> V | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            stored_at, value = entry
            if self._clock() - stored_at >= self._ttl:
                del self._entries[key]
                return None
            return value

    def put(self, key: K, value: V) -> None:
        with self._lock:
            self._entries[key] = (self._clock(), value)


# ------------------------------------------------------------- views and bulk


@dataclass(frozen=True)
class SandboxView:
    """Un sandbox del inventario con la memoria de su imagen (`None` si no se
    pudo leer). Sin `envs`, `runHookPayload` ni tokens: no existen en el
    modelo."""

    sandbox_id: str
    state: str
    template: str
    template_name: str
    template_version: str
    started_at: datetime
    memory_mib: int | None


@dataclass(frozen=True)
class ImageSummary:
    name: str
    image_arn: str
    state: str
    latest_active_version: str | None
    latest_failed_version: str | None
    created_at: datetime


@dataclass(frozen=True)
class ImageVersionSummary:
    """Una versión de imagen; `artifact` es sólo el nombre del fichero del
    artefacto (nunca el bucket ni la ruta) y `memory_mib` sólo si la lista
    lo trae."""

    image_version: str
    state: str
    status: str
    base_image_version: str | None
    artifact: str | None
    memory_mib: int | None
    created_at: datetime


@dataclass(frozen=True)
class BulkPlan:
    """Lo que tardará una acción masiva con la cuota de TPS de su operación."""

    action: BulkAction
    operation: str
    count: int
    tps: float
    estimated_seconds: float


def bulk_rate(action: BulkAction, applied_rate: float | None) -> float:
    """El ritmo de la cola: la cuota aplicada si es menor que la de `apiTps`
    (una cuenta nueva parte reducida); si es mayor, la de `apiTps`, que es lo
    que respeta el plano de control del SDK."""
    default = float(API_TPS[BULK_OPERATIONS[action]])
    return default if applied_rate is None else min(default, applied_rate)


def plan_bulk(action: BulkAction, count: int, applied_rate: float | None = None) -> BulkPlan:
    rate = bulk_rate(action, applied_rate)
    return BulkPlan(action, BULK_OPERATIONS[action], count, rate, count / rate)


@dataclass(frozen=True)
class BulkItemResult:
    """El resultado de una acción sobre un sandbox. `code` es el código de
    AWS de un fallo, nunca su mensaje."""

    sandbox_id: str
    outcome: BulkOutcome
    code: str | None = None


@dataclass(frozen=True)
class BulkProgress:
    done: int
    total: int
    last: BulkItemResult


@dataclass(frozen=True)
class BulkResult:
    plan: BulkPlan
    items: tuple[BulkItemResult, ...] = field(default_factory=tuple)

    def count(self, outcome: BulkOutcome) -> int:
        return sum(1 for item in self.items if item.outcome == outcome)
