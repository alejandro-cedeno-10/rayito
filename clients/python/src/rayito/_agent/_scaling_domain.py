"""Dominio puro del escalado de un agente servido (`ScalingOptions`, ADR-029):
los límites de concurrencia por VM, el asa enrutable de una sesión y las
dos reglas de salud de la flota (cuándo se da un VM por perdido y cuántos
faltan para el mínimo). No importa `boto3`, `grpc` ni el código generado del
proto y no toca la red: `AgentFleet` (la cáscara de I/O) le da los datos que
midió y ejecuta lo que decide.

Los vectores compartidos con TypeScript están en
`testdata/agent-fleet/routing.json`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal, TypeAlias, get_args

from rayito._limits import (
    AGENT_FLEET_UNHEALTHY_PINGS,
    AGENT_SCALE_IN_AFTER_SECONDS_DEFAULT,
    AGENT_SCALE_IN_AFTER_SECONDS_MIN,
    AGENT_SCALING_MAX_VMS_DEFAULT,
    AGENT_SCALING_MIN_VMS_DEFAULT,
    AGENT_SCALING_WARM_VMS_DEFAULT,
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
    pasa `ScalingOptions`.

    Coste y activación
    ------------------
    Activa: `deploy_agent()` (`rayito agent deploy`) con `max_vms` mayor que 1,
        o con `warm_vms` mayor que 0; por sí sola, esta clase no crea nada.
    Recursos y llamadas AWS: `min_vms` MicroVMs en marcha (`run-microvm`),
        `warm_vms` plazas suspendidas de un pool y los VMs extra que cree el
        reparto hasta `max_vms`.
    Coste aproximado: el del MicroVM por segundo mientras vive (2 GB: ≈ $0,126
        por hora en us-east-1, consultado 2026-10-06 en
        https://aws.amazon.com/lambda/pricing/), por cada VM; una plaza
        aparcada o un VM suspendido sólo pagan el almacenamiento de su
        snapshot.
    IAM: los de `Sandbox.create()`, `Sandbox.list()` y `Sandbox.connect()`.
    Cómo apagarla: los valores por defecto (`min_vms=1, max_vms=1, warm_vms=0`)
        son un solo VM; `fleet.stop()` termina todos los VMs.
    Ejemplo:
        ScalingOptions(min_vms=1, max_vms=3, warm_vms=1)
    """

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
    "ScalingOptions",
    "SessionHandle",
    "SessionMode",
    "decode_handle",
    "encode_handle",
    "is_unhealthy",
    "max_sessions_for_memory",
    "replacements_needed",
    "validate_caller_session_id",
]
