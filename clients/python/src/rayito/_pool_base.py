"""Núcleo puro del pool de sandboxes suspendidos (ADR-008): configuración,
registro de plaza, reglas de selección, reciclado y reconciliación, backoff
del relleno y estadísticas. Sin I/O ni hilos; `sandbox_sync.pool` y
`sandbox_async.pool` ponen la mecánica encima.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal

from rayito._limits import MAX_DURATION_SECONDS
from rayito._models import REDACTED, IdlePolicy, SandboxInfo
from rayito._payload import validated_cpu_time_limit, validated_envs, validated_metadata
from rayito._sandbox_base import (
    DEFAULT_IDLE_POLICY,
    DEFAULT_READY_TIMEOUT_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
    LoggingOption,
    ReadinessPoll,
    resolve_idle_policy,
    validate_timeout,
)
from rayito.exceptions import InvalidArgumentException, PoolClosedException

POOL_SCHEMA: Final = "rayito.pool/1"
POOL_SIZE_MAX: Final = 64
FILL_CONCURRENCY_MAX: Final = 8
MIN_REMAINING_FLOOR_SECONDS: Final = 60
SWEEP_INTERVAL_MIN_SECONDS: Final = 5.0
DEFAULT_MIN_REMAINING_SECONDS: Final = 3600
DEFAULT_FILL_CONCURRENCY: Final = 4
DEFAULT_SWEEP_INTERVAL_SECONDS: Final = 30.0

SlotState = Literal["warming", "ready"]
ReconcileAction = Literal["keep", "repark", "check", "drop"]
LISTED_STATES_TO_KEEP: Final = frozenset({"SUSPENDED", "SUSPENDING", "PENDING"})
LISTED_STATES_TO_DROP: Final = frozenset({"TERMINATING", "TERMINATED"})


@dataclass(frozen=True)
class PoolConfig:
    """Configuración de lanzamiento, inmutable y común a todas las plazas.

    Cada campo se corresponde con el kwarg homónimo de `Sandbox.create()`:
    `envs`, `metadata`, `cpu_time_limit`, la política de idle, la vida máxima
    y los conectores son por pool, nunca por toma. `timeout` es 28 800 por
    defecto (aparcar todo lo que AWS permite) y `min_remaining_seconds` la
    vida mínima con la que se entrega una plaza: por debajo se recicla.
    `idle` es obligatorio y con `auto_resume=True`: sin política de idle el
    comportamiento de un VM suspendido por la API no está documentado y con
    ella una plaza olvidada se termina sola en `timeout`. No hay
    `access_token` (uno por plaza) ni `allowed_ports` (`get_host(port)` acuña
    por puerto tras la toma).
    """

    size: int
    template: str | None = None
    template_version: str | None = None
    timeout: int = MAX_DURATION_SECONDS
    idle: IdlePolicy = DEFAULT_IDLE_POLICY
    envs: Mapping[str, str] | None = None
    metadata: Mapping[str, str] | None = None
    cpu_time_limit: int | None = None
    execution_role_arn: str | None = None
    ingress: Sequence[str] | None = None
    egress: Sequence[str] | None = None
    logging: LoggingOption = "disabled"
    min_remaining_seconds: int = DEFAULT_MIN_REMAINING_SECONDS
    fill_concurrency: int = DEFAULT_FILL_CONCURRENCY
    sweep_interval_seconds: float = DEFAULT_SWEEP_INTERVAL_SECONDS
    ready_timeout: float = DEFAULT_READY_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        validate_pool_size(self.size)
        validate_timeout(self.timeout)
        validate_pool_idle(self.idle, self.timeout)
        validate_min_remaining(self.min_remaining_seconds, self.timeout)
        validate_fill_concurrency(self.fill_concurrency)
        validate_sweep_interval(self.sweep_interval_seconds)
        validate_ready_timeout(self.ready_timeout)
        if self.envs is not None:
            validated_envs(self.envs)
        if self.metadata is not None:
            validated_metadata(self.metadata)
        if self.cpu_time_limit is not None:
            validated_cpu_time_limit(self.cpu_time_limit)


def validate_pool_size(size: object) -> int:
    if isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= POOL_SIZE_MAX:
        raise InvalidArgumentException(
            f"size debe ser un entero en 1..={POOL_SIZE_MAX}, recibido {size!r}"
        )
    return size


def validate_pool_idle(idle: object, timeout: int) -> IdlePolicy:
    if not isinstance(idle, IdlePolicy):
        raise InvalidArgumentException(
            "idle debe ser una IdlePolicy con auto_resume=True: el pool nunca aparca sin "
            f"política de idle, recibido {idle!r}"
        )
    if not idle.auto_resume:
        raise InvalidArgumentException(
            "idle debe tener auto_resume=True: una plaza aparcada debe despertar sola"
        )
    if idle.max_idle_seconds >= timeout:
        raise InvalidArgumentException(
            f"idle.max_idle_seconds={idle.max_idle_seconds} debe ser menor que timeout={timeout}"
        )
    return idle


def validate_min_remaining(min_remaining_seconds: object, timeout: int) -> int:
    ceiling = timeout - MIN_REMAINING_FLOOR_SECONDS
    if (
        isinstance(min_remaining_seconds, bool)
        or not isinstance(min_remaining_seconds, int)
        or not MIN_REMAINING_FLOOR_SECONDS <= min_remaining_seconds <= ceiling
    ):
        raise InvalidArgumentException(
            f"min_remaining_seconds debe ser un entero en {MIN_REMAINING_FLOOR_SECONDS}..="
            f"{ceiling} (timeout - {MIN_REMAINING_FLOOR_SECONDS}), recibido "
            f"{min_remaining_seconds!r}"
        )
    return min_remaining_seconds


def validate_fill_concurrency(fill_concurrency: object) -> int:
    if (
        isinstance(fill_concurrency, bool)
        or not isinstance(fill_concurrency, int)
        or not 1 <= fill_concurrency <= FILL_CONCURRENCY_MAX
    ):
        raise InvalidArgumentException(
            f"fill_concurrency debe ser un entero en 1..={FILL_CONCURRENCY_MAX}, recibido "
            f"{fill_concurrency!r}"
        )
    return fill_concurrency


def validate_sweep_interval(sweep_interval_seconds: object) -> float:
    if (
        isinstance(sweep_interval_seconds, bool)
        or not isinstance(sweep_interval_seconds, int | float)
        or sweep_interval_seconds < SWEEP_INTERVAL_MIN_SECONDS
    ):
        raise InvalidArgumentException(
            f"sweep_interval_seconds debe ser >= {SWEEP_INTERVAL_MIN_SECONDS:g}, recibido "
            f"{sweep_interval_seconds!r}"
        )
    return float(sweep_interval_seconds)


def validate_ready_timeout(ready_timeout: object) -> float:
    if (
        isinstance(ready_timeout, bool)
        or not isinstance(ready_timeout, int | float)
        or ready_timeout <= 0
    ):
        raise InvalidArgumentException(f"ready_timeout debe ser > 0, recibido {ready_timeout!r}")
    return float(ready_timeout)


def launch_kwargs(config: PoolConfig) -> dict[str, Any]:
    """Los kwargs de `Sandbox.create()` de cada plaza; el pool añade el token,
    `keep_on_failure=False`, el plano de control y el transporte."""
    return {
        "template": config.template,
        "template_version": config.template_version,
        "timeout": config.timeout,
        "idle": config.idle,
        "envs": config.envs,
        "metadata": config.metadata,
        "cpu_time_limit": config.cpu_time_limit,
        "execution_role_arn": config.execution_role_arn,
        "ingress": config.ingress,
        "egress": config.egress,
        "logging": config.logging,
        "ready_timeout": config.ready_timeout,
    }


def resolved_pool_idle(config: PoolConfig) -> IdlePolicy:
    """La política con `suspended_duration_seconds` resuelto como lo hará
    `create()`: `timeout - max_idle_seconds` salvo que venga fijado."""
    resolved = resolve_idle_policy(config.idle, config.timeout)
    if resolved is None:
        raise InvalidArgumentException("idle no puede ser None en un pool")
    return resolved


@dataclass(frozen=True, repr=False)
class SlotRecord:
    """Todo lo que `take()` necesita de una plaza sin un `get-microvm`.

    `access_token` es el secreto de la plaza (uno por plaza, acuñado por el
    pool) y se redacta en `repr`/`str`. `state` es `warming` desde que
    `run-microvm` responde hasta que el aparcado termina, y `ready` después.
    """

    sandbox_id: str
    access_token: str
    endpoint: str
    template: str
    template_version: str
    started_at: datetime
    maximum_duration_seconds: int
    region: str
    state: SlotState
    idle: IdlePolicy | None = None
    execution_role_arn: str | None = None
    ingress: tuple[str, ...] = ()
    egress: tuple[str, ...] = ()
    parked_at: datetime | None = None

    @property
    def expires_at(self) -> datetime:
        return self.started_at + timedelta(seconds=self.maximum_duration_seconds)

    def remaining_seconds(self, now: datetime | None = None) -> float:
        current = now or datetime.now(UTC)
        return max(0.0, (self.expires_at - current).total_seconds())

    def __repr__(self) -> str:
        return (
            f"SlotRecord(sandbox_id={self.sandbox_id!r}, state={self.state!r}, "
            f"access_token={REDACTED!r}, expires_at={self.expires_at.isoformat()!r})"
        )


def record_from_info(
    info: SandboxInfo,
    *,
    access_token: str,
    region: str,
    state: SlotState,
    parked_at: datetime | None = None,
) -> SlotRecord:
    return SlotRecord(
        sandbox_id=info.sandbox_id,
        access_token=access_token,
        endpoint=info.endpoint,
        template=info.template,
        template_version=info.template_version,
        started_at=info.started_at,
        maximum_duration_seconds=info.maximum_duration_seconds,
        region=region,
        state=state,
        idle=info.idle,
        execution_role_arn=info.execution_role_arn,
        ingress=tuple(info.ingress),
        egress=tuple(info.egress),
        parked_at=parked_at,
    )


def sandbox_info_from_record(record: SlotRecord, state: str = "SUSPENDED") -> SandboxInfo:
    return SandboxInfo(
        sandbox_id=record.sandbox_id,
        state=state,
        endpoint=record.endpoint,
        template=record.template,
        template_version=record.template_version,
        started_at=record.started_at,
        maximum_duration_seconds=record.maximum_duration_seconds,
        idle=record.idle,
        execution_role_arn=record.execution_role_arn,
        ingress=record.ingress,
        egress=record.egress,
    )


def record_to_dict(record: SlotRecord) -> dict[str, Any]:
    """Forma JSON de una plaza (esquema `rayito.pool/1`, compartido con el
    SDK TypeScript): fechas ISO-8601 UTC, `idle` con sus tres campos."""
    return {
        "sandbox_id": record.sandbox_id,
        "access_token": record.access_token,
        "endpoint": record.endpoint,
        "template": record.template,
        "template_version": record.template_version,
        "started_at": iso_utc(record.started_at),
        "maximum_duration_seconds": record.maximum_duration_seconds,
        "region": record.region,
        "state": record.state,
        "idle": idle_to_dict(record.idle),
        "execution_role_arn": record.execution_role_arn,
        "ingress": list(record.ingress),
        "egress": list(record.egress),
        "parked_at": None if record.parked_at is None else iso_utc(record.parked_at),
    }


def record_from_dict(data: Mapping[str, Any]) -> SlotRecord:
    try:
        state = str(data["state"])
        if state not in ("warming", "ready"):
            raise InvalidArgumentException(f"estado de plaza desconocido: {state!r}")
        parked = data.get("parked_at")
        return SlotRecord(
            sandbox_id=str(data["sandbox_id"]),
            access_token=str(data["access_token"]),
            endpoint=str(data["endpoint"]),
            template=str(data["template"]),
            template_version=str(data["template_version"]),
            started_at=parse_iso_utc(str(data["started_at"])),
            maximum_duration_seconds=int(data["maximum_duration_seconds"]),
            region=str(data["region"]),
            state="warming" if state == "warming" else "ready",
            idle=idle_from_dict(data.get("idle")),
            execution_role_arn=optional_str(data.get("execution_role_arn")),
            ingress=tuple(str(arn) for arn in data.get("ingress") or ()),
            egress=tuple(str(arn) for arn in data.get("egress") or ()),
            parked_at=None if parked is None else parse_iso_utc(str(parked)),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise InvalidArgumentException(f"registro de plaza inválido: {exc!r}") from exc


def optional_str(value: object) -> str | None:
    return None if value is None else str(value)


def iso_utc(moment: datetime) -> str:
    aware = moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)
    return aware.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_iso_utc(text: str) -> datetime:
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def idle_to_dict(idle: IdlePolicy | None) -> dict[str, Any] | None:
    if idle is None:
        return None
    return {
        "max_idle_seconds": idle.max_idle_seconds,
        "suspended_duration_seconds": idle.suspended_duration_seconds,
        "auto_resume": idle.auto_resume,
    }


def idle_from_dict(data: Mapping[str, Any] | None) -> IdlePolicy | None:
    if not data:
        return None
    suspended = data.get("suspended_duration_seconds")
    return IdlePolicy(
        max_idle_seconds=int(data["max_idle_seconds"]),
        suspended_duration_seconds=None if suspended is None else int(suspended),
        auto_resume=bool(data.get("auto_resume", True)),
    )


def pick_ready_slot(
    records: Iterable[SlotRecord], now: datetime, min_remaining_seconds: int
) -> SlotRecord | None:
    """La plaza `ready` que caduca antes de entre las que aún tienen
    `min_remaining_seconds` de vida (las más viejas primero, así ninguna se
    pudre aparcada); `None` si no hay ninguna."""
    candidates = [
        record
        for record in records
        if record.state == "ready" and not slot_is_stale(record, now, min_remaining_seconds)
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda record: (record.expires_at, record.sandbox_id))


def slot_is_stale(record: SlotRecord, now: datetime, min_remaining_seconds: int) -> bool:
    return record.remaining_seconds(now) < min_remaining_seconds


def reconcile_action(record: SlotRecord, listed_state: str | None) -> ReconcileAction:
    """Qué hacer con una plaza `ready` según lo que dijo `list-microvms`:
    `keep` (suspendida o arrancando), `repark` (alguien la reanudó), `check`
    (ausente del listado: leer `get-microvm`) o `drop` (listada terminal)."""
    if record.state != "ready":
        return "keep"
    if listed_state is None:
        return "check"
    if listed_state == "RUNNING":
        return "repark"
    if listed_state in LISTED_STATES_TO_DROP:
        return "drop"
    return "keep"


class FillBackoff:
    """Espera tras un calentamiento fallido: 1 s doblando hasta 60 s con
    ±25 % de jitter; un éxito la devuelve a 1 s."""

    INITIAL_DELAY: float = 1.0
    MAX_DELAY: float = 60.0
    JITTER: float = 0.25

    def __init__(self, *, random: Callable[[], float] = random.random) -> None:
        self._random = random
        self._delay = self.INITIAL_DELAY

    def next_delay(self) -> float:
        delay = self._delay * (1.0 + self.JITTER * (2.0 * self._random() - 1.0))
        self._delay = min(self._delay * 2.0, self.MAX_DELAY)
        return max(0.0, delay)

    def reset(self) -> None:
        self._delay = self.INITIAL_DELAY


class TakePoll(ReadinessPoll):
    """Sondeo de `Health` tras un `resume-microvm` explícito: 0,1 s doblando
    hasta 0,5 s sin jitter. El VM ya estaba listo al aparcarlo, así que el
    primer `Health` que responde es el bueno."""

    INITIAL_DELAY: float = 0.1
    MAX_DELAY: float = 0.5
    JITTER: float = 0.0


@dataclass(frozen=True)
class PoolSlotInfo:
    """Una plaza vista desde `stats()`: nunca lleva el token."""

    sandbox_id: str
    state: SlotState
    started_at: datetime
    expires_at: datetime
    parked_at: datetime | None


@dataclass(frozen=True)
class PoolStats:
    """Contadores acumulados desde `start()` y las plazas actuales.

    `takes == hits + misses`; `launched` cuenta cada `run-microvm` aceptado
    (calentamientos y fallbacks), `recycled` los reciclados del sweeper,
    `lost` las plazas perdidas (reconciliación, toma o recuperación) y
    `failed` los calentamientos y tomas que lanzaron una excepción.
    """

    size: int
    ready: int
    warming: int
    takes: int
    hits: int
    misses: int
    launched: int
    recycled: int
    lost: int
    failed: int
    slots: tuple[PoolSlotInfo, ...] = ()


@dataclass
class PoolCounters:
    takes: int = 0
    hits: int = 0
    misses: int = 0
    launched: int = 0
    recycled: int = 0
    lost: int = 0
    failed: int = 0


def slot_info_from_record(record: SlotRecord) -> PoolSlotInfo:
    return PoolSlotInfo(
        sandbox_id=record.sandbox_id,
        state=record.state,
        started_at=record.started_at,
        expires_at=record.expires_at,
        parked_at=record.parked_at,
    )


def stats_from_records(
    records: Iterable[SlotRecord], *, size: int, warming: int, counters: PoolCounters
) -> PoolStats:
    """`ready` sale de los registros; `warming` lo cuenta el pool (un
    calentamiento en vuelo puede no tener registro aún)."""
    ordered = sorted(records, key=lambda record: (record.expires_at, record.sandbox_id))
    return PoolStats(
        size=size,
        ready=sum(1 for record in ordered if record.state == "ready"),
        warming=warming,
        takes=counters.takes,
        hits=counters.hits,
        misses=counters.misses,
        launched=counters.launched,
        recycled=counters.recycled,
        lost=counters.lost,
        failed=counters.failed,
        slots=tuple(slot_info_from_record(record) for record in ordered),
    )


@dataclass(frozen=True)
class LaunchKwargDefaults:
    """Los valores por defecto de `create()` con los que `pool=` compara."""

    template: object = None
    template_version: object = None
    timeout: object = DEFAULT_TIMEOUT_SECONDS
    max_lifetime: object = None
    on_timeout: object = None
    idle: object = DEFAULT_IDLE_POLICY
    envs: object = None
    metadata: object = None
    cpu_time_limit: object = None
    execution_role_arn: object = None
    allowed_ports: object = None
    ingress: object = None
    egress: object = None
    network: object = None
    allow_internet_access: object = True
    logging: object = "disabled"
    region: object = None
    session: object = None
    access_token: object = None
    keep_on_failure: object = False
    control_plane: object = None
    transport: object = None


LAUNCH_KWARG_DEFAULTS: Final = LaunchKwargDefaults()
POOL_REJECTED_KWARGS: Final[tuple[str, ...]] = tuple(
    name for name in LaunchKwargDefaults.__dataclass_fields__
)


def rejected_pool_kwarg(given: Mapping[str, object]) -> str | None:
    """El primer kwarg de lanzamiento o de plano que difiere de su valor por
    defecto, en el orden de la firma de `create()`; `None` si ninguno."""
    for name in POOL_REJECTED_KWARGS:
        if name in given and given[name] != getattr(LAUNCH_KWARG_DEFAULTS, name):
            return name
    return None


def reject_launch_kwargs_with_pool(given: Mapping[str, object]) -> None:
    offending = rejected_pool_kwarg(given)
    if offending is not None:
        raise InvalidArgumentException(
            f"create(pool=...) no admite `{offending}`: la configuración de lanzamiento es la "
            "del PoolConfig del pool; sólo pasan ready_timeout, request_timeout y "
            "reconnect_timeout"
        )


def pool_not_started_error() -> PoolClosedException:
    return PoolClosedException("pool not started: call start() or use it as a context manager")


def pool_closed_error() -> PoolClosedException:
    return PoolClosedException("el pool está cerrado: take() ya no entrega sandboxes")


def drain_requires_persistence_error() -> InvalidArgumentException:
    return InvalidArgumentException(
        "close(drain=False) requiere un backend persistent: con el backend en memoria las "
        "plazas aparcadas serían irrecuperables"
    )


__all__ = [
    "DEFAULT_FILL_CONCURRENCY",
    "DEFAULT_MIN_REMAINING_SECONDS",
    "DEFAULT_SWEEP_INTERVAL_SECONDS",
    "POOL_REJECTED_KWARGS",
    "POOL_SCHEMA",
    "FillBackoff",
    "PoolConfig",
    "PoolCounters",
    "PoolSlotInfo",
    "PoolStats",
    "SlotRecord",
    "SlotState",
    "TakePoll",
    "drain_requires_persistence_error",
    "launch_kwargs",
    "pick_ready_slot",
    "pool_closed_error",
    "pool_not_started_error",
    "reconcile_action",
    "record_from_dict",
    "record_from_info",
    "record_to_dict",
    "reject_launch_kwargs_with_pool",
    "rejected_pool_kwarg",
    "resolved_pool_idle",
    "sandbox_info_from_record",
    "slot_is_stale",
    "stats_from_records",
]
