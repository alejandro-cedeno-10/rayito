"""Dominio puro del escalado de un agente servido (`ScalingOptions`, ADR-029):
los límites de concurrencia por VM, el asa enrutable de una sesión, la
colocación de una sesión nueva entre los MicroVMs de la flota, la decisión de
cola y el calendario de reducción. No importa `boto3`, `grpc` ni el código
generado del proto, no tiene reloj propio (el instante llega por parámetro) y
no toca la red: `AgentFleet` (la cáscara de I/O) le da los datos que midió y
ejecuta lo que decide.

Los vectores compartidos con TypeScript están en
`testdata/agent-fleet/routing.json`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal, TypeAlias, get_args

from rayito._limits import (
    AGENT_FLEET_UNHEALTHY_PINGS,
    AGENT_SCALE_IN_AFTER_SECONDS_DEFAULT,
    AGENT_SCALE_IN_AFTER_SECONDS_MIN,
    AGENT_SCALING_MAX_VMS_DEFAULT,
    AGENT_SCALING_MIN_VMS_DEFAULT,
    AGENT_SCALING_WARM_VMS_DEFAULT,
    AGENT_SERVICE_QUEUE_MAX,
    AGENT_SERVICE_RESERVED_CONNECTIONS,
    AGENT_SESSION_HANDLE_PREFIX,
    BASELINE_MIB_PER_VCPU,
    MAX_CONCURRENT_CONNECTIONS_1_VCPU,
    MAX_DURATION_SECONDS,
    POOL_SIZE_MAX,
)
from rayito.exceptions import InvalidArgumentException

#: Cómo se reparten las sesiones: compartidas (varias por VM, hasta
#: `ServeOptions.max_concurrency`) o dedicadas (una sesión por MicroVM).
SessionMode: TypeAlias = Literal["shared", "dedicated"]
SESSION_MODES: Final[tuple[str, ...]] = get_args(SessionMode)
SESSIONS_SHARED: Final = "shared"
SESSIONS_DEDICATED: Final = "dedicated"
DEFAULT_SESSION_MODE: Final = SESSIONS_SHARED
#: Separador del asa enrutable `rs1.<sandbox_id>.<session_id>`.
HANDLE_SEPARATOR: Final = "."
#: Las decisiones de `place_session` y de `decide_queue`.
PLACEMENT_ROUTE: Final = "route"
PLACEMENT_CLAIM: Final = "claim"
PLACEMENT_CREATE: Final = "create"
PLACEMENT_WAIT: Final = "wait"
PLACEMENT_LOST: Final = "lost"
QUEUE_ENQUEUE: Final = "enqueue"
QUEUE_REJECT: Final = "reject"
QUEUE_TIMEOUT: Final = "timeout"


def max_sessions_for_memory(memory_mib: int) -> int:
    """Tope de sesiones simultáneas por VM de `memory_mib`: las conexiones
    que admite el MicroVM (`MAX_CONCURRENT_CONNECTIONS_1_VCPU` por vCPU de
    base, `AWS_API_NOTES.md` cuotas) menos las que el servicio se reserva para
    `/ping` y control. Por debajo de la memoria de una vCPU de base se cuenta
    una vCPU, un valor conservador que la aceptación en AWS comprueba."""
    if isinstance(memory_mib, bool) or not isinstance(memory_mib, int) or memory_mib < 1:
        raise InvalidArgumentException("memory_mib debe ser un entero positivo")
    vcpus = max(1, memory_mib // BASELINE_MIB_PER_VCPU)
    return MAX_CONCURRENT_CONNECTIONS_1_VCPU * vcpus - AGENT_SERVICE_RESERVED_CONNECTIONS


def _whole(value: object, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise InvalidArgumentException(f"{label} debe ser un entero en {minimum}..{maximum}")
    return value


@dataclass(frozen=True)
class ScalingOptions:
    """Cuántos MicroVMs sirven el agente y cómo se reparten las sesiones.
    `min_vms` corren siempre, `warm_vms` esperan suspendidos (una sesión nueva
    los reanuda en vez de crear un VM) y `max_vms` acota el total; con
    `sessions="dedicated"` cada sesión tiene su propio MicroVM. Los VMs por
    encima de `min_vms` se terminan tras `scale_in_after_seconds` sin uso.

    Los valores por defecto son los de un solo VM: nada cambia para quien no
    pasa `ScalingOptions`."""

    min_vms: int = AGENT_SCALING_MIN_VMS_DEFAULT
    max_vms: int = AGENT_SCALING_MAX_VMS_DEFAULT
    warm_vms: int = AGENT_SCALING_WARM_VMS_DEFAULT
    sessions: SessionMode = DEFAULT_SESSION_MODE
    scale_in_after_seconds: float = AGENT_SCALE_IN_AFTER_SECONDS_DEFAULT

    def __post_init__(self) -> None:
        _whole(self.min_vms, "ScalingOptions.min_vms", 0, POOL_SIZE_MAX)
        _whole(self.max_vms, "ScalingOptions.max_vms", 1, POOL_SIZE_MAX)
        _whole(self.warm_vms, "ScalingOptions.warm_vms", 0, POOL_SIZE_MAX)
        if self.min_vms > self.max_vms:
            raise InvalidArgumentException("ScalingOptions.min_vms no puede superar a max_vms")
        if self.warm_vms > self.max_vms - self.min_vms:
            raise InvalidArgumentException(
                "ScalingOptions.warm_vms no puede superar a max_vms - min_vms"
            )
        if self.sessions not in SESSION_MODES:
            raise InvalidArgumentException(
                f"ScalingOptions.sessions debe ser uno de {list(SESSION_MODES)}"
            )
        seconds = self.scale_in_after_seconds
        if (
            isinstance(seconds, bool)
            or not isinstance(seconds, int | float)
            or not AGENT_SCALE_IN_AFTER_SECONDS_MIN <= seconds <= MAX_DURATION_SECONDS
        ):
            raise InvalidArgumentException(
                "ScalingOptions.scale_in_after_seconds debe estar en "
                f"[{AGENT_SCALE_IN_AFTER_SECONDS_MIN}, {MAX_DURATION_SECONDS}]"
            )

    @property
    def routes_by_handle(self) -> bool:
        """Con más de un VM posible, `AgentResult.session_id` es un asa que
        nombra el VM (`rs1.<sandbox_id>.<session_id>`); con uno solo, el id
        de sesión queda como siempre."""
        return self.max_vms > 1


@dataclass(frozen=True)
class SessionHandle:
    """El asa enrutable de una sesión: el MicroVM que la sirve y el id de
    sesión que ve el host."""

    sandbox_id: str
    session_id: str


def encode_handle(sandbox_id: str, session_id: str) -> str:
    """`rs1.<sandbox_id>.<session_id>`. El id del sandbox no lleva `.`."""
    if not sandbox_id or HANDLE_SEPARATOR in sandbox_id:
        raise InvalidArgumentException(
            "el id del sandbox de un asa no puede ser vacío ni llevar '.'"
        )
    if not session_id:
        raise InvalidArgumentException("el id de sesión de un asa no puede ser vacío")
    return (
        f"{AGENT_SESSION_HANDLE_PREFIX}{HANDLE_SEPARATOR}{sandbox_id}{HANDLE_SEPARATOR}{session_id}"
    )


def decode_handle(value: str) -> SessionHandle | None:
    """El asa de `value`, o `None` si es un id de sesión sin enrutar."""
    prefix = f"{AGENT_SESSION_HANDLE_PREFIX}{HANDLE_SEPARATOR}"
    if not value.startswith(prefix):
        return None
    sandbox_id, separator, session_id = value[len(prefix) :].partition(HANDLE_SEPARATOR)
    if not sandbox_id or not separator or not session_id:
        return None
    return SessionHandle(sandbox_id, session_id)


def validate_caller_session_id(value: str) -> str:
    """Un id de sesión que elige quien llama: no puede empezar por el
    prefijo reservado de las asas si no es un asa completa."""
    prefix = f"{AGENT_SESSION_HANDLE_PREFIX}{HANDLE_SEPARATOR}"
    if not value:
        raise InvalidArgumentException("session_id no puede ser vacío")
    if value.startswith(prefix) and decode_handle(value) is None:
        raise InvalidArgumentException(
            f"session_id no puede empezar por '{prefix}' salvo que sea un asa completa"
        )
    return value


@dataclass(frozen=True)
class VmView:
    """Lo que la flota sabe de un MicroVM al colocar una sesión: `active`
    llamadas en curso, `served` sesiones distintas que ya ha servido, si
    responde (`healthy`), si su `/ping` dice `HealthyBusy` (`busy`) y si es una
    plaza templada suspendida (`warm`) que aún no sirve."""

    sandbox_id: str
    active: int = 0
    served: int = 0
    healthy: bool = True
    busy: bool = False
    warm: bool = False


@dataclass(frozen=True)
class Placement:
    """Dónde va una sesión: `route` a un VM en marcha (`sandbox_id`), `claim`
    una plaza templada, `create` un VM nuevo, `wait` (la flota está llena) o
    `lost` (el asa nombra un VM que ya no está o no responde)."""

    action: str
    sandbox_id: str | None = None


def place_session(
    session_id: str | None,
    vms: Sequence[VmView],
    scaling: ScalingOptions,
    *,
    max_concurrency: int,
) -> Placement:
    """Decide dónde va una sesión. Con un asa va a su VM (o es `lost`). Sin
    asa, en modo compartido va al VM sano con menos llamadas y hueco; en modo
    dedicado, a un VM sin sesiones servidas. Si no hay, reanuda una plaza
    templada, crea un VM si el total no llega a `max_vms` o espera."""
    handle = None if session_id is None else decode_handle(session_id)
    if handle is not None:
        return _placement_for_handle(handle, vms)
    candidates = [vm for vm in vms if vm.healthy and not vm.warm]
    chosen = _free_vm(candidates, scaling, max_concurrency)
    if chosen is not None:
        return Placement(PLACEMENT_ROUTE, chosen.sandbox_id)
    warm = sorted((vm for vm in vms if vm.warm and vm.healthy), key=lambda vm: vm.sandbox_id)
    if warm:
        return Placement(PLACEMENT_CLAIM, warm[0].sandbox_id)
    if len(vms) < scaling.max_vms:
        return Placement(PLACEMENT_CREATE)
    return Placement(PLACEMENT_WAIT)


def _placement_for_handle(handle: SessionHandle, vms: Sequence[VmView]) -> Placement:
    for vm in vms:
        if vm.sandbox_id == handle.sandbox_id and vm.healthy:
            return Placement(PLACEMENT_ROUTE, vm.sandbox_id)
    return Placement(PLACEMENT_LOST, handle.sandbox_id)


def _free_vm(
    candidates: Sequence[VmView], scaling: ScalingOptions, max_concurrency: int
) -> VmView | None:
    if scaling.sessions == SESSIONS_DEDICATED:
        eligible = [vm for vm in candidates if vm.served == 0 and vm.active == 0]
    else:
        eligible = [vm for vm in candidates if not vm.busy and vm.active < max_concurrency]
    if not eligible:
        return None
    return min(eligible, key=lambda vm: (vm.active, vm.sandbox_id))


@dataclass(frozen=True)
class QueueDecision:
    """Qué hace el cliente con una llamada que no cabe: `enqueue` (espera
    `wait_seconds` y reintenta), `reject` (la cola está llena o desactivada) o
    `timeout` (esperó todo `queue_timeout_seconds`)."""

    action: str
    wait_seconds: float = 0.0


def decide_queue(
    *,
    waiting: int,
    elapsed_seconds: float,
    queue_max: int,
    queue_timeout_seconds: float,
    retry_after_seconds: float,
) -> QueueDecision:
    """La decisión de cola de una llamada que recibió 429. Con
    `queue_max == 0` (el valor por defecto) se rechaza enseguida, como en 0.14;
    con cola, espera lo que dice el servicio (`retry_after_seconds`) sin pasar
    de lo que queda del plazo."""
    _whole(queue_max, "queue_max", 0, AGENT_SERVICE_QUEUE_MAX)
    if queue_max == 0 or waiting >= queue_max:
        return QueueDecision(QUEUE_REJECT)
    remaining = queue_timeout_seconds - elapsed_seconds
    if remaining <= 0:
        return QueueDecision(QUEUE_TIMEOUT)
    return QueueDecision(QUEUE_ENQUEUE, min(retry_after_seconds, remaining))


@dataclass(frozen=True)
class VmUsage:
    """El uso de un VM en marcha para calcular la reducción: cuándo se creó
    y cuándo se usó por última vez (segundos desde la época)."""

    sandbox_id: str
    created_at: float
    last_used_at: float


def scale_in_deadlines(usages: Sequence[VmUsage], scaling: ScalingOptions) -> Mapping[str, float]:
    """Plazo de terminación de cada VM por encima de `min_vms`: su último uso
    más `scale_in_after_seconds`. Los `min_vms` más antiguos son la base y no
    tienen plazo."""
    ordered = sorted(usages, key=lambda usage: (usage.created_at, usage.sandbox_id))
    extras = ordered[scaling.min_vms :]
    return {
        usage.sandbox_id: usage.last_used_at + scaling.scale_in_after_seconds for usage in extras
    }


def expired_vms(deadlines: Mapping[str, float], now: float) -> tuple[str, ...]:
    """Los VMs cuyo plazo de reducción ya pasó en `now`, por plazo."""
    due = [(deadline, sandbox_id) for sandbox_id, deadline in deadlines.items() if deadline <= now]
    return tuple(sandbox_id for _, sandbox_id in sorted(due))


def is_unhealthy(failed_pings: int) -> bool:
    """Un VM se da por perdido tras `AGENT_FLEET_UNHEALTHY_PINGS` pings
    seguidos sin respuesta."""
    return failed_pings >= AGENT_FLEET_UNHEALTHY_PINGS


def replacements_needed(healthy_running: int, scaling: ScalingOptions) -> int:
    """Cuántos VMs crear para volver a `min_vms` tras perder alguno."""
    return max(0, scaling.min_vms - healthy_running)


__all__ = [
    "DEFAULT_SESSION_MODE",
    "SESSIONS_DEDICATED",
    "SESSIONS_SHARED",
    "SESSION_MODES",
    "Placement",
    "QueueDecision",
    "ScalingOptions",
    "SessionHandle",
    "SessionMode",
    "VmUsage",
    "VmView",
    "decide_queue",
    "decode_handle",
    "encode_handle",
    "expired_vms",
    "is_unhealthy",
    "max_sessions_for_memory",
    "place_session",
    "replacements_needed",
    "scale_in_deadlines",
    "validate_caller_session_id",
]
