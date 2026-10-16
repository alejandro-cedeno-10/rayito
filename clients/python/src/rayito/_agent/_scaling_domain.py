"""`ScalingOptions`: cuántos MicroVMs sirven un agente y cómo se reparten las
sesiones entre ellos (`AgentFleet`, `deploy_agent`). Es un valor puro: nada
aquí llama a AWS.

Los valores por defecto describen exactamente el despliegue de un solo VM de
`AgentProject.deploy()` (1 mínimo, 1 máximo, 0 calientes, sesiones
compartidas).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

from rayito._limits import IDLE_MAX_IDLE_MIN_SECONDS, POOL_SIZE_MAX
from rayito.exceptions import InvalidArgumentException

SessionMode = Literal["shared", "dedicated"]
#: Las sesiones comparten VM hasta `max_concurrency`.
SESSIONS_SHARED: Final = "shared"
#: Cada sesión tiene su propio MicroVM y un VM nunca atiende a dos sesiones.
SESSIONS_DEDICATED: Final = "dedicated"
SESSION_MODES: Final[tuple[str, ...]] = (SESSIONS_SHARED, SESSIONS_DEDICATED)
#: Un solo VM, sin plazas calientes: lo mismo que `AgentProject.deploy()`.
SCALING_MIN_VMS_DEFAULT: Final = 1
SCALING_MAX_VMS_DEFAULT: Final = 1
SCALING_WARM_VMS_DEFAULT: Final = 0
#: Los VMs por encima de `min_vms` se terminan solos tras este tiempo sin
#: uso (`rayd` `set_timeout`): por encima de los ≈ 140 s en que suspender
#: compensa (`AWS_API_NOTES.md`, sección de coste) y por debajo de una hora.
SCALE_IN_AFTER_SECONDS_DEFAULT: Final = 900
#: Suelo de `scale_in_after_seconds`: el mínimo de inactividad que admite la
#: plataforma para una política de idle.
SCALE_IN_AFTER_SECONDS_MIN: Final = IDLE_MAX_IDLE_MIN_SECONDS


def _integer(value: object, label: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise InvalidArgumentException(f"ScalingOptions.{label} debe ser un entero >= {minimum}")
    return value


@dataclass(frozen=True)
class ScalingOptions:
    """Escalado de un agente servido: `min_vms` VMs siempre vivos, hasta
    `max_vms`, y `warm_vms` plazas aparcadas (suspendidas) listas para
    absorber un pico sin esperar un `run-microvm`. `sessions="shared"` reparte
    las sesiones entre los VMs con cupo; `"dedicated"` da un MicroVM a cada
    sesión. `scale_in_after_seconds` es la inactividad tras la que un VM por
    encima de `min_vms` se termina solo.

    Coste y activación
    -------------------
    Activa: nada por sí mismo; el valor es puro. Lo que cuesta es
        `deploy_agent()` (o `rayito agent deploy`), que crea `min_vms` VMs y
        `warm_vms` plazas aparcadas.
    Recursos y llamadas AWS: ninguno aquí.
    Coste aproximado: ninguno aquí; ver `deploy_agent()`.
    IAM: ninguna para este valor.
    Cómo apagarla: los valores por defecto (1, 1, 0) son un único VM.
    Ejemplo:
        ScalingOptions(min_vms=1, max_vms=3, warm_vms=1, sessions="dedicated")
    """

    min_vms: int = SCALING_MIN_VMS_DEFAULT
    max_vms: int = SCALING_MAX_VMS_DEFAULT
    warm_vms: int = SCALING_WARM_VMS_DEFAULT
    sessions: SessionMode = SESSIONS_SHARED
    scale_in_after_seconds: int = SCALE_IN_AFTER_SECONDS_DEFAULT

    def __post_init__(self) -> None:
        _integer(self.min_vms, "min_vms", minimum=1)
        _integer(self.max_vms, "max_vms", minimum=1)
        _integer(self.warm_vms, "warm_vms", minimum=0)
        _integer(self.scale_in_after_seconds, "scale_in_after_seconds", minimum=0)
        if self.max_vms > POOL_SIZE_MAX:
            raise InvalidArgumentException(f"ScalingOptions.max_vms debe ser <= {POOL_SIZE_MAX}")
        if self.min_vms > self.max_vms:
            raise InvalidArgumentException("ScalingOptions.min_vms no puede superar max_vms")
        if self.warm_vms > self.max_vms - self.min_vms:
            raise InvalidArgumentException(
                "ScalingOptions.warm_vms no puede superar max_vms - min_vms"
            )
        if self.sessions not in SESSION_MODES:
            raise InvalidArgumentException(
                f"ScalingOptions.sessions debe ser uno de {', '.join(SESSION_MODES)}"
            )
        if self.scale_in_after_seconds < SCALE_IN_AFTER_SECONDS_MIN:
            raise InvalidArgumentException(
                f"ScalingOptions.scale_in_after_seconds debe ser >= {SCALE_IN_AFTER_SECONDS_MIN}"
            )

    @property
    def routable(self) -> bool:
        """Con más de un VM posible, el id de sesión lleva su VM (handle)."""
        return self.max_vms > 1
