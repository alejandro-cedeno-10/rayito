"""Núcleo puro del plazo lógico (ADR-011), compartido por `Sandbox` y
`AsyncSandbox`: el plan de lanzamiento (`max_lifetime`, `on_timeout`, el
bloque `lifecycle` del `runHookPayload` y la política de idle de la
plataforma), la lectura de `LifecycleState`, la extensión de `connect()`, el
retardo del disparador del modo `pause` y la traducción de los errores de
`SetTimeout`. Sin I/O.

`rayd` es la fuente de verdad del plazo: el SDK sólo valida antes de
`run-microvm` las mismas reglas que `rayd` aplica al payload y reacciona a
los plazos que `Health` y `SetTimeout` le reportan.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Literal

import grpc

from rayito._limits import (
    IDLE_SUSPENDED_MIN_SECONDS,
    LIFECYCLE_AUTO_RESUME_MIN_SECONDS,
    LIFECYCLE_MIN_MAX_LIFETIME_SECONDS,
    LIFECYCLE_MIN_TIMEOUT_SECONDS,
    MAX_DURATION_SECONDS,
)
from rayito._models import (
    IdlePolicy,
    LifecyclePhaseName,
    SandboxLifecycle,
    TimeoutActionName,
)
from rayito._transport import (
    SANDBOX_TIMEOUT_DETAIL,
    rpc_details,
    rpc_status,
    translate_rpc_error,
)
from rayito.exceptions import (
    InvalidArgumentException,
    LifecycleUnsupportedException,
    SandboxLifetimeException,
    SandboxStateException,
)
from rayito.v1 import lifecycle_pb2

__all__ = [
    "SANDBOX_TIMEOUT_DETAIL",
    "LifecycleBlock",
    "LifecyclePlan",
    "TimeoutRequest",
    "auto_resume_reopen",
    "beyond_cap_error",
    "connect_extension",
    "deadline_may_have_moved",
    "lifecycle_from_proto",
    "lifecycle_from_state",
    "older_agent_error",
    "pause_trigger_delay",
    "resolve_idle_policy",
    "resolve_lifecycle",
    "suspended_set_timeout_error",
    "translate_set_timeout_error",
    "unix_ms_now",
    "validate_set_timeout_seconds",
]

PAUSE_TRIGGER_SLACK_SECONDS: Final = 1.0
CONNECT_CAP_SAFETY_MS: Final = 5000
MIN_REOPEN_MS: Final = LIFECYCLE_MIN_TIMEOUT_SECONDS * 1000
MAX_LIFETIME_MARGIN_SECONDS: Final = 60
BEYOND_CAP_PREFIX: Final = "timeout beyond cap"
LIFECYCLE_UNMANAGED_DETAIL: Final = "lifecycle_unmanaged"
CAP_UNIX_MS_PATTERN: Final = re.compile(r"cap_unix_ms=(-?\d+)")
ON_TIMEOUT_VALUES: Final[tuple[TimeoutActionName, ...]] = ("kill", "pause")
REOPEN_PHASES: Final[frozenset[LifecyclePhaseName]] = frozenset({"resume_grace", "expired"})

TimeoutModeName = Literal["exact", "at_least"]

PHASES: Final[dict[int, LifecyclePhaseName]] = {
    lifecycle_pb2.LIFECYCLE_PHASE_UNSPECIFIED: "unmanaged",
    lifecycle_pb2.LIFECYCLE_PHASE_UNMANAGED: "unmanaged",
    lifecycle_pb2.LIFECYCLE_PHASE_ACTIVE: "active",
    lifecycle_pb2.LIFECYCLE_PHASE_RESUME_GRACE: "resume_grace",
    lifecycle_pb2.LIFECYCLE_PHASE_EXPIRED: "expired",
}
ACTIONS: Final[dict[int, TimeoutActionName]] = {
    lifecycle_pb2.TIMEOUT_ACTION_KILL: "kill",
    lifecycle_pb2.TIMEOUT_ACTION_PAUSE: "pause",
}
MODES: Final[dict[TimeoutModeName, lifecycle_pb2.TimeoutMode]] = {
    "exact": lifecycle_pb2.TIMEOUT_MODE_EXACT,
    "at_least": lifecycle_pb2.TIMEOUT_MODE_AT_LEAST,
}

PAUSE_REQUIRES_IDLE_MESSAGE: Final = (
    "on_timeout='pause' necesita idle=IdlePolicy(...): sin cliente, la suspensión la hace la "
    "política de idle de la plataforma"
)


@dataclass(frozen=True)
class LifecycleBlock:
    """El bloque `lifecycle` del `runHookPayload` (design D3)."""

    timeout_s: int
    cap_s: int
    on_timeout: TimeoutActionName
    auto_resume: bool

    def to_wire(self) -> dict[str, object]:
        return {
            "auto_resume": self.auto_resume,
            "cap_s": self.cap_s,
            "on_timeout": self.on_timeout,
            "timeout_s": self.timeout_s,
        }


@dataclass(frozen=True)
class LifecyclePlan:
    """Lo que `create()` manda a `run-microvm`: el bloque (o `None`, el
    lanzamiento de ADR-007), `maximumDurationInSeconds` y el `idlePolicy`."""

    block: LifecycleBlock | None
    platform_duration: int
    idle: IdlePolicy | None


@dataclass(frozen=True)
class TimeoutRequest:
    """Un `SetTimeoutRequest` por mandar: `exact` (lo que manda
    `set_timeout`) puede acortar el plazo, `at_least` (lo que manda
    `connect`) nunca lo acorta."""

    mode: TimeoutModeName
    timeout_ms: int

    @property
    def seconds(self) -> int:
        return self.timeout_ms // 1000

    @property
    def operation(self) -> str:
        return "set_timeout" if self.mode == "exact" else "connect(timeout=)"

    @property
    def call(self) -> str:
        if self.mode == "exact":
            return f"set_timeout({self.seconds})"
        return f"connect(timeout={self.seconds})"

    def to_proto(self) -> Any:
        return lifecycle_pb2.SetTimeoutRequest(timeout_ms=self.timeout_ms, mode=MODES[self.mode])


def resolve_idle_policy(idle: IdlePolicy | None, timeout: int) -> IdlePolicy | None:
    """Rellena `suspended_duration_seconds` con `timeout - max_idle_seconds`."""
    if idle is None:
        return None
    if idle.max_idle_seconds >= timeout:
        raise InvalidArgumentException(
            f"idle.max_idle_seconds={idle.max_idle_seconds} debe ser menor que timeout={timeout}"
        )
    if idle.suspended_duration_seconds is not None:
        return idle
    return IdlePolicy(
        max_idle_seconds=idle.max_idle_seconds,
        suspended_duration_seconds=timeout - idle.max_idle_seconds,
        auto_resume=idle.auto_resume,
    )


def resolve_lifecycle(
    *,
    timeout: int,
    max_lifetime: int | None,
    on_timeout: str | None,
    idle: IdlePolicy | None,
) -> LifecyclePlan:
    """Sin `max_lifetime` ni `on_timeout` el lanzamiento es byte a byte el
    de ADR-007 (sin bloque, `maximumDurationInSeconds = timeout`). Con
    cualquiera de los dos, `timeout` (ya validado) es el plazo lógico y
    `max_lifetime` el tope de la plataforma (por defecto `timeout + 60`,
    mínimo 120 s)."""
    if max_lifetime is None and on_timeout is None:
        return LifecyclePlan(
            block=None, platform_duration=timeout, idle=resolve_idle_policy(idle, timeout)
        )
    action = validate_on_timeout(on_timeout)
    cap = resolve_max_lifetime(timeout, max_lifetime)
    if action == "pause":
        return pause_plan(timeout, cap, idle)
    return LifecyclePlan(
        block=LifecycleBlock(timeout_s=timeout, cap_s=cap, on_timeout="kill", auto_resume=False),
        platform_duration=cap,
        idle=resolve_idle_policy(idle, cap),
    )


def validate_on_timeout(on_timeout: str | None) -> TimeoutActionName:
    if on_timeout is None:
        return "kill"
    for action in ON_TIMEOUT_VALUES:
        if on_timeout == action:
            return action
    raise InvalidArgumentException(f"on_timeout debe ser 'kill' o 'pause', recibido {on_timeout!r}")


def resolve_max_lifetime(timeout: int, max_lifetime: int | None) -> int:
    if max_lifetime is None:
        return default_max_lifetime_for(timeout)
    return validate_max_lifetime(max_lifetime, timeout)


def default_max_lifetime_for(timeout: int) -> int:
    return min(
        max(timeout + MAX_LIFETIME_MARGIN_SECONDS, LIFECYCLE_MIN_MAX_LIFETIME_SECONDS),
        MAX_DURATION_SECONDS,
    )


def validate_max_lifetime(max_lifetime: object, timeout: int) -> int:
    if isinstance(max_lifetime, bool) or not isinstance(max_lifetime, int):
        raise InvalidArgumentException(
            f"max_lifetime debe ser un entero en segundos, recibido {max_lifetime!r}"
        )
    if max_lifetime > MAX_DURATION_SECONDS:
        raise SandboxLifetimeException(
            f"max_lifetime={max_lifetime} supera el tope de {MAX_DURATION_SECONDS} s (8 h, "
            "running + suspended) de un MicroVM; usa reincarnate() para seguir con los "
            "ficheros en un sandbox nuevo"
        )
    if max_lifetime < LIFECYCLE_MIN_MAX_LIFETIME_SECONDS:
        raise InvalidArgumentException(
            f"max_lifetime debe ser >= {LIFECYCLE_MIN_MAX_LIFETIME_SECONDS} s (60 s de margen "
            f"sobre el /run más 60 s de vida útil), recibido {max_lifetime}"
        )
    if timeout > max_lifetime:
        raise InvalidArgumentException(
            f"timeout={timeout} no puede superar max_lifetime={max_lifetime}"
        )
    return max_lifetime


def pause_plan(timeout: int, cap: int, idle: IdlePolicy | None) -> LifecyclePlan:
    """En modo `pause` la plataforma siempre auto-reanuda (así la próxima
    petición despierta un sandbox suspendido por el plazo) y `auto_resume`
    viaja como regla lógica que `rayd` aplica al reanudarse."""
    if idle is None:
        raise InvalidArgumentException(PAUSE_REQUIRES_IDLE_MESSAGE)
    if idle.suspended_duration_seconds is not None:
        raise InvalidArgumentException(
            "on_timeout='pause' fija idle.suspended_duration_seconds en max_lifetime - "
            "max_idle_seconds: no lo pases"
        )
    if idle.max_idle_seconds >= cap:
        raise InvalidArgumentException(
            f"idle.max_idle_seconds={idle.max_idle_seconds} debe ser menor que max_lifetime={cap}"
        )
    platform_idle = IdlePolicy(
        max_idle_seconds=idle.max_idle_seconds,
        suspended_duration_seconds=max(IDLE_SUSPENDED_MIN_SECONDS, cap - idle.max_idle_seconds),
        auto_resume=True,
    )
    block = LifecycleBlock(
        timeout_s=timeout, cap_s=cap, on_timeout="pause", auto_resume=idle.auto_resume
    )
    return LifecyclePlan(block=block, platform_duration=cap, idle=platform_idle)


def unix_ms_now() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


def instant_from_unix_ms(value: int) -> datetime | None:
    return None if value == 0 else datetime.fromtimestamp(value / 1000, tz=UTC)


def unix_ms_from_instant(instant: datetime | None) -> int:
    return 0 if instant is None else int(instant.timestamp() * 1000)


def lifecycle_from_state(state: Any) -> SandboxLifecycle:
    """Un `LifecycleState` (de `Health` o de la respuesta de `SetTimeout`)."""
    return SandboxLifecycle(
        phase=PHASES.get(int(state.phase), "unmanaged"),
        deadline=instant_from_unix_ms(int(state.deadline_unix_ms)),
        cap=instant_from_unix_ms(int(state.cap_unix_ms)),
        timeout_seconds=int(state.timeout_ms) / 1000,
        on_timeout=ACTIONS.get(int(state.on_timeout)),
        auto_resume=bool(state.auto_resume),
        extensions=int(state.extensions),
    )


def lifecycle_from_proto(response: Any) -> SandboxLifecycle | None:
    """`None` cuando `Health` no trae `lifecycle`: un agente anterior a M9,
    que no impone ningún timeout (la puerta de capacidad del SDK)."""
    if not response.HasField("lifecycle"):
        return None
    return lifecycle_from_state(response.lifecycle)


def deadline_may_have_moved(state: str, lifecycle: SandboxLifecycle | None) -> bool:
    """Si `get_info()` debe releer `Health`: sólo un sandbox `RUNNING` con
    plazo lógico gestionado, porque otro cliente (`Sandbox.set_timeout`,
    `connect(timeout=)`) o `rayd` al reanudarse pueden haberlo movido.
    `metadata` y los hechos del guest quedan fijos en `/run`, así que sin
    plazo gestionado no hay nada que refrescar y no se paga un RPC."""
    return state == "RUNNING" and lifecycle is not None and lifecycle.managed


def validate_set_timeout_seconds(timeout: object, *, mode: TimeoutModeName = "exact") -> int:
    if isinstance(timeout, bool) or not isinstance(timeout, int):
        raise InvalidArgumentException(
            f"timeout debe ser un entero en segundos, recibido {timeout!r}"
        )
    if timeout < LIFECYCLE_MIN_TIMEOUT_SECONDS:
        raise InvalidArgumentException(
            f"timeout debe ser >= {LIFECYCLE_MIN_TIMEOUT_SECONDS} s, recibido {timeout}"
        )
    if timeout > MAX_DURATION_SECONDS:
        raise beyond_cap_error(TimeoutRequest(mode, timeout * 1000), None)
    return timeout


def connect_extension(
    lifecycle: SandboxLifecycle | None, requested: int | None, now_ms: int
) -> TimeoutRequest | None:
    """Qué manda `connect()` (o `resume()`) tras la readiness (design D8).

    Un `timeout` pedido siempre es `at_least`. Sin él, sólo un sandbox en
    `resume_grace`/`expired` se reabre, con su propio `timeout` acotado al
    tope menos 5 s de seguridad. Un agente anterior a M9 o un sandbox sin
    plazo lógico no admiten un `timeout` pedido."""
    seconds = (
        None if requested is None else validate_set_timeout_seconds(requested, mode="at_least")
    )
    if lifecycle is None:
        if seconds is not None:
            raise LifecycleUnsupportedException(
                "connect(timeout=) necesita una imagen M9: el agente de este sandbox no impone "
                "el timeout del servidor"
            )
        return None
    if not lifecycle.managed:
        if seconds is not None:
            raise unmanaged_error("connect(timeout=)")
        return None
    if seconds is not None:
        return TimeoutRequest("at_least", seconds * 1000)
    if lifecycle.phase not in REOPEN_PHASES:
        return None
    return TimeoutRequest("at_least", reopen_timeout_ms(lifecycle, now_ms))


def reopen_timeout_ms(lifecycle: SandboxLifecycle, now_ms: int) -> int:
    cap_ms = unix_ms_from_instant(lifecycle.cap)
    timeout_ms = round(lifecycle.timeout_seconds * 1000)
    reopened = min(timeout_ms, cap_ms - now_ms - CONNECT_CAP_SAFETY_MS)
    if reopened < MIN_REOPEN_MS:
        raise SandboxLifetimeException(
            f"al sandbox le queda menos de 1 s hasta su max_lifetime (tope a las "
            f"{cap_text(cap_ms)}): no se puede reabrir; usa reincarnate() para seguir con los "
            "ficheros en un sandbox nuevo"
        )
    return reopened


def auto_resume_reopen(
    lifecycle: SandboxLifecycle | None,
    *,
    paused_generation: int | None,
    generation: int,
    now_ms: int,
) -> TimeoutRequest | None:
    """El `SetTimeout` que reabre un sandbox en modo `pause` con `auto_resume`
    que este cliente suspendió por su plazo y que ya se reanudó (la
    `resume_generation` avanzó desde `paused_generation`) pero sigue
    `expired`.

    `rayd` sólo aplica la regla de E2B (`max(timeout, 5 min)`, acotada al
    tope) si su vigilante vio una congelación de al menos 2 s; una
    suspensión real más corta (el disparador suspende y la siguiente
    petición del mismo cliente reanuda al instante) lo deja `expired`. El
    SDK aplica entonces la misma regla con el token de acceso, acotada al
    tope menos 5 s de seguridad. `None` si no toca o si no queda ni 1 s
    hasta el tope."""
    if paused_generation is None or generation <= paused_generation:
        return None
    if lifecycle is None or lifecycle.phase != "expired":
        return None
    if lifecycle.on_timeout != "pause" or not lifecycle.auto_resume:
        return None
    timeout_ms = max(
        round(lifecycle.timeout_seconds * 1000), LIFECYCLE_AUTO_RESUME_MIN_SECONDS * 1000
    )
    cap_ms = unix_ms_from_instant(lifecycle.cap)
    reopened = min(timeout_ms, cap_ms - now_ms - CONNECT_CAP_SAFETY_MS)
    if reopened < MIN_REOPEN_MS:
        return None
    return TimeoutRequest("exact", reopened)


def pause_trigger_delay(lifecycle: SandboxLifecycle | None, now_unix_ms: int) -> float | None:
    """Segundos hasta que el SDK debe comprobar un sandbox en modo `pause`:
    el plazo más 1 s de holgura por el desfase de relojes, `0` si ya venció,
    `None` si no hay nada que vigilar."""
    if lifecycle is None or lifecycle.on_timeout != "pause":
        return None
    if lifecycle.phase == "expired":
        return 0.0
    if lifecycle.phase != "active" or lifecycle.deadline is None:
        return None
    remaining_ms = max(0, unix_ms_from_instant(lifecycle.deadline) - now_unix_ms)
    return remaining_ms / 1000 + PAUSE_TRIGGER_SLACK_SECONDS


def cap_text(cap_unix_ms: int) -> str:
    return datetime.fromtimestamp(cap_unix_ms / 1000, tz=UTC).isoformat(timespec="seconds")


def beyond_cap_error(request: TimeoutRequest, cap_unix_ms: int | None) -> InvalidArgumentException:
    cap = "" if cap_unix_ms is None else f"tope a las {cap_text(cap_unix_ms)}; "
    return InvalidArgumentException(
        f"{request.call} supera el max_lifetime del sandbox ({cap}max_lifetime se fija en "
        f"create() y como mucho vale {MAX_DURATION_SECONDS} s): la vida de la plataforma no se "
        "puede extender (no existe UpdateMicrovm); usa reincarnate() para seguir con los "
        "ficheros en un sandbox nuevo"
    )


def unmanaged_error(operation: str) -> InvalidArgumentException:
    return InvalidArgumentException(
        f"{operation} no aplica: el sandbox se creó sin max_lifetime ni on_timeout y su vida es "
        "la de la plataforma (maximumDurationInSeconds, fija); crea el sandbox con "
        "max_lifetime= para tener un timeout movible"
    )


def suspended_set_timeout_error(sandbox_id: str) -> SandboxStateException:
    """`Sandbox.set_timeout(sandbox_id)` nunca despierta un sandbox
    suspendido: el `SetTimeout` sería la petición que lo reanuda."""
    return SandboxStateException(
        f"el sandbox {sandbox_id} está suspendido: connect() lo reanuda y acepta timeout="
    )


def older_agent_error(template: str, agent_version: str) -> LifecycleUnsupportedException:
    return LifecycleUnsupportedException(
        f"la imagen {template} (agent_version {agent_version}) no impone el timeout del "
        "servidor: publica una imagen M9 o crea el sandbox sin max_lifetime ni on_timeout"
    )


def translate_set_timeout_error(exc: grpc.RpcError, request: TimeoutRequest) -> Exception:
    """Los rechazos propios de `SetTimeout` antes de la tabla unaria:
    más allá del tope, sandbox sin plazo lógico y agente anterior a M9."""
    code = rpc_status(exc)
    details = rpc_details(exc)
    if code is grpc.StatusCode.INVALID_ARGUMENT and details.startswith(BEYOND_CAP_PREFIX):
        return beyond_cap_error(request, cap_unix_ms_from_details(details))
    if code is grpc.StatusCode.FAILED_PRECONDITION and details == LIFECYCLE_UNMANAGED_DETAIL:
        return unmanaged_error(request.operation)
    if code is grpc.StatusCode.UNIMPLEMENTED:
        return LifecycleUnsupportedException(
            f"{request.operation} necesita una imagen M9: el agente de este sandbox no tiene "
            "LifecycleService",
            grpc_code=code,
        )
    return translate_rpc_error(exc)


def cap_unix_ms_from_details(details: str) -> int | None:
    match = CAP_UNIX_MS_PATTERN.search(details)
    return None if match is None else int(match.group(1))
