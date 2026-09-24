"""Núcleo puro compartido por `Sandbox` y `AsyncSandbox`: validación de
argumentos, construcción de requests y la política de sondeo de readiness.
Sin I/O de red ni de disco.
"""

from __future__ import annotations

import dataclasses
import functools
import os
import random
import time
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from logging import Logger, getLogger
from typing import Any, Final, Generic, Literal, TypeVar, cast

import grpc

from rayito._aws import LaunchRequest, PortSpec
from rayito._lifecycle_base import lifecycle_from_proto, resolve_lifecycle
from rayito._lifecycle_base import resolve_idle_policy as resolve_idle_policy
from rayito._limits import (
    DEFAULT_PORT,
    HOOKS_PORT,
    MANAGED_NETWORK_CONNECTORS,
    MAX_DURATION_SECONDS,
    MICROVM_ID_MAX_LENGTH,
    MICROVM_ID_MIN_LENGTH,
    MIN_DURATION_SECONDS,
    NETWORK_CONNECTORS_MAX,
    SUSPENDED_STATES,
    TERMINAL_STATES,
)
from rayito._models import (
    IdlePolicy,
    SandboxHealth,
    SandboxInfo,
    template_name_from_arn,
    validate_port,
)
from rayito._network_base import enforcement_from_proto
from rayito._payload import (
    build_run_hook_payload,
    generate_access_token,
    validate_access_token,
)
from rayito._transport import is_phase_gate, rpc_details
from rayito.exceptions import (
    AuthenticationException,
    InvalidArgumentException,
    SandboxException,
    SandboxLifetimeException,
    SandboxNotFoundException,
    SandboxNotReadyException,
    SandboxStateException,
)
from rayito.v1 import health_pb2

TEMPLATE_ENV_VAR: Final = "RAYITO_TEMPLATE"
ACCESS_TOKEN_ENV_VAR: Final = "RAYITO_ACCESS_TOKEN"
DEFAULT_TIMEOUT_SECONDS: Final = 3600
DEFAULT_READY_TIMEOUT_SECONDS: Final = 90.0
DEFAULT_REQUEST_TIMEOUT_SECONDS: Final = 60.0
DEFAULT_RECONNECT_TIMEOUT_SECONDS: Final = 60.0
CLOCK_OFFSET_WARN_MS: Final = 5000
DEFAULT_IDLE_POLICY: Final = IdlePolicy()
LOG_GROUP_PREFIX: Final = "/rayito"

logger = getLogger("rayito.sandbox")

SHARED_ACCESS_TOKEN_WARNING: Final = (
    "%s está definida: todos los Sandbox.create() de este proceso comparten el "
    "mismo access token, así que una fuga abre la flota y no un solo MicroVM; "
    "pasa access_token= en cada create() para tener uno por sandbox"
)

LoggingOption = Literal["disabled", "cloudwatch"] | Mapping[str, Any]
PortLike = int | tuple[int, int]


VariantResult = TypeVar("VariantResult")


class class_method_variant:
    """Descriptor al estilo de E2B: `sbx.kill()` usa el método de instancia y
    `Sandbox.kill(sandbox_id)` el classmethod indicado. Ambas formas
    devuelven lo mismo, así que el tipo de retorno del método de instancia
    tipa también la forma de clase (`Sandbox.connect(id)` es un `Sandbox`)."""

    def __init__(self, class_method_name: str) -> None:
        self._class_method_name = class_method_name

    def __call__(self, method: Callable[..., VariantResult]) -> ClassMethodVariant[VariantResult]:
        return ClassMethodVariant(self._class_method_name, method)


class ClassMethodVariant(Generic[VariantResult]):
    """El descriptor que produce `class_method_variant` al decorar."""

    def __init__(self, class_method_name: str, method: Callable[..., VariantResult]) -> None:
        self._class_method_name = class_method_name
        self._method = method

    def __get__(
        self, instance: object | None, owner: type | None = None
    ) -> Callable[..., VariantResult]:
        if instance is not None:
            return functools.partial(self._method, instance)
        if owner is None:
            raise TypeError("class_method_variant necesita el owner")
        return cast("Callable[..., VariantResult]", getattr(owner, self._class_method_name))


@dataclass(frozen=True)
class LaunchPlan:
    """Todo lo que `create()` necesita antes de tocar la red.
    `lifecycle_requested` es la puerta de agente M9: el payload lleva un
    bloque `lifecycle` y el `Health` de readiness debe traerlo. El access
    token es el secreto del sandbox: nunca aparece en `repr()`."""

    access_token: str = field(repr=False)
    request: LaunchRequest
    proxy_ports: tuple[PortSpec, ...]
    lifecycle_requested: bool = False


def sandbox_logger(custom: Logger | None) -> Logger:
    """El logger de los registros de un sandbox: el de `create(logger=)` o
    `connect(logger=)` si se dio, `rayito.sandbox` si no."""
    return logger if custom is None else custom


def resolve_template(template: str | None) -> str:
    resolved = template or os.environ.get(TEMPLATE_ENV_VAR)
    if not resolved:
        raise InvalidArgumentException(
            f"falta el template: pasa `template=` o define {TEMPLATE_ENV_VAR}"
        )
    return resolved


@functools.cache
def warn_shared_access_token() -> None:
    """Una sola vez por proceso: el aviso describe la configuración, no la
    llamada, y repetirlo por cada `create()` lo convertiría en ruido que el
    operador filtra."""
    logger.warning(SHARED_ACCESS_TOKEN_WARNING, ACCESS_TOKEN_ENV_VAR)


def resolve_access_token(access_token: str | None) -> str:
    """Token explícito, `RAYITO_ACCESS_TOKEN` o uno nuevo; siempre base64url
    canónico, que es lo único que `rayd` acepta en `x-access-token`."""
    if access_token:
        return validate_access_token(access_token)
    from_environment = os.environ.get(ACCESS_TOKEN_ENV_VAR)
    if not from_environment:
        return generate_access_token()
    warn_shared_access_token()
    return validate_access_token(from_environment)


def require_access_token(access_token: str | None, *, operation: str = "connect()") -> str:
    resolved = access_token or os.environ.get(ACCESS_TOKEN_ENV_VAR)
    if not resolved:
        raise AuthenticationException(
            f"{operation} necesita el access_token del sandbox: pásalo o define "
            f"{ACCESS_TOKEN_ENV_VAR}"
        )
    return validate_access_token(resolved)


def validate_sandbox_id(sandbox_id: str) -> str:
    """Sólo por longitud: el prefijo real es `microvm-` pero no se parsea."""
    if not isinstance(sandbox_id, str) or not (
        MICROVM_ID_MIN_LENGTH <= len(sandbox_id) <= MICROVM_ID_MAX_LENGTH
    ):
        raise InvalidArgumentException(f"sandbox_id inválido: {sandbox_id!r}")
    return sandbox_id


def validate_timeout(timeout: int) -> int:
    if isinstance(timeout, bool) or not isinstance(timeout, int):
        raise InvalidArgumentException(
            f"timeout debe ser un entero en segundos, recibido {timeout!r}"
        )
    if timeout > MAX_DURATION_SECONDS:
        raise SandboxLifetimeException(
            f"timeout={timeout} supera el tope de {MAX_DURATION_SECONDS} s (8 h, running + "
            "suspended); la vida de un MicroVM no se puede extender después"
        )
    if timeout < MIN_DURATION_SECONDS:
        raise InvalidArgumentException(
            f"timeout debe ser >= {MIN_DURATION_SECONDS}, recibido {timeout}"
        )
    return timeout


def proxy_port_specs(allowed_ports: Sequence[PortLike] | None) -> tuple[PortSpec, ...]:
    """Puertos del token principal: 8080 siempre; nunca el de hooks ni `allPorts`."""
    specs = [PortSpec.single(DEFAULT_PORT)]
    for entry in allowed_ports or ():
        spec = port_spec(entry)
        if spec.covers(HOOKS_PORT):
            raise InvalidArgumentException(
                f"allowed_ports no puede cubrir el puerto de hooks {HOOKS_PORT} (ADR-006)"
            )
        if spec not in specs:
            specs.append(spec)
    return tuple(specs)


def port_spec(entry: PortLike) -> PortSpec:
    if isinstance(entry, tuple):
        if len(entry) != 2:
            raise InvalidArgumentException(f"rango de puertos inválido: {entry!r}")
        return PortSpec.range(
            validate_port(entry[0], field="allowed_ports"),
            validate_port(entry[1], field="allowed_ports"),
        )
    return PortSpec.single(validate_port(entry, field="allowed_ports"))


def validate_host_port(port: int) -> int:
    validated = validate_port(port)
    if validated == HOOKS_PORT:
        raise InvalidArgumentException(
            f"get_host({HOOKS_PORT}) no está permitido: es el puerto de hooks (ADR-006)"
        )
    return validated


def connector_arns(values: Sequence[str] | None, *, region: str, field: str) -> tuple[str, ...]:
    """Nombres gestionados (`ALL_INGRESS`, ...) o ARNs propios; máx. 10."""
    if not values:
        return ()
    if len(values) > NETWORK_CONNECTORS_MAX:
        raise InvalidArgumentException(f"{field}: máximo {NETWORK_CONNECTORS_MAX} conectores")
    resolved: list[str] = []
    for value in values:
        if value.startswith("arn:"):
            resolved.append(value)
        elif value in MANAGED_NETWORK_CONNECTORS:
            resolved.append(managed_connector_arn(value, region=region))
        else:
            raise InvalidArgumentException(
                f"{field}: {value!r} no es un ARN ni un conector gestionado "
                f"({', '.join(sorted(MANAGED_NETWORK_CONNECTORS))})"
            )
    return tuple(resolved)


def managed_connector_arn(name: str, *, region: str) -> str:
    return f"arn:aws:lambda:{region}:aws:network-connector:aws-network-connector:{name}"


def logging_config(option: LoggingOption, *, template_name: str) -> dict[str, Any]:
    """`logging` siempre explícito: `run-microvm` no hereda el log group de la imagen."""
    if option == "disabled":
        return {"disabled": {}}
    if option == "cloudwatch":
        return {"cloudWatch": {"logGroup": f"{LOG_GROUP_PREFIX}/{template_name}"}}
    if isinstance(option, Mapping) and set(option) in ({"disabled"}, {"cloudWatch"}):
        return dict(option)
    raise InvalidArgumentException(
        "logging debe ser 'disabled', 'cloudwatch' o un dict con 'disabled' o 'cloudWatch'"
    )


def build_launch_plan(
    *,
    image_arn: str,
    region: str,
    template_version: str | None,
    timeout: int,
    idle: IdlePolicy | None,
    envs: Mapping[str, str] | None,
    execution_role_arn: str | None,
    allowed_ports: Sequence[PortLike] | None,
    ingress: Sequence[str] | None,
    egress: Sequence[str] | None,
    logging: LoggingOption,
    access_token: str | None,
    metadata: Mapping[str, str] | None = None,
    cpu_time_limit: int | None = None,
    max_lifetime: int | None = None,
    on_timeout: str | None = None,
    network_enforce: bool = False,
) -> LaunchPlan:
    token = resolve_access_token(access_token)
    lifecycle = resolve_lifecycle(
        timeout=validate_timeout(timeout),
        max_lifetime=max_lifetime,
        on_timeout=on_timeout,
        idle=idle,
    )
    request = LaunchRequest(
        image_arn=image_arn,
        image_version=template_version,
        maximum_duration_seconds=lifecycle.platform_duration,
        run_hook_payload=build_run_hook_payload(
            access_token=token,
            envs=envs,
            metadata=metadata,
            cpu_time_limit=cpu_time_limit,
            lifecycle=lifecycle.block,
            network_enforce=network_enforce,
        ),
        client_token=uuid.uuid4().hex,
        logging=logging_config(logging, template_name=template_name_from_arn(image_arn)),
        execution_role_arn=execution_role_arn,
        idle=lifecycle.idle,
        ingress_connectors=connector_arns(ingress, region=region, field="ingress"),
        egress_connectors=connector_arns(egress, region=region, field="egress"),
    )
    return LaunchPlan(
        access_token=token,
        request=request,
        proxy_ports=proxy_port_specs(allowed_ports),
        lifecycle_requested=lifecycle.block is not None,
    )


class ReadinessPoll:
    """Calendario del sondeo de `Health`: 0,25 s doblando hasta 2 s, con una
    comprobación de `get-microvm` cada 5 s para detectar `TERMINATING|TERMINATED`."""

    INITIAL_DELAY: float = 0.25
    MAX_DELAY: float = 2.0
    STATE_CHECK_INTERVAL: float = 5.0
    MAX_RPC_TIMEOUT: float = 5.0
    MIN_RPC_TIMEOUT: float = 0.5
    JITTER: float = 0.0

    def __init__(
        self,
        *,
        timeout: float,
        monotonic: Callable[[], float] = time.monotonic,
        random: Callable[[], float] = random.random,
    ) -> None:
        self._monotonic = monotonic
        self._random = random
        started = monotonic()
        self._started = started
        self._deadline = started + timeout
        self._last_state_check = started
        self._delay = self.INITIAL_DELAY

    def elapsed(self) -> float:
        return max(0.0, self._monotonic() - self._started)

    def remaining(self) -> float:
        return max(0.0, self._deadline - self._monotonic())

    def timed_out(self) -> bool:
        return self.remaining() <= 0.0

    def rpc_timeout(self) -> float:
        return min(self.MAX_RPC_TIMEOUT, max(self.MIN_RPC_TIMEOUT, self.remaining()))

    def should_check_state(self) -> bool:
        now = self._monotonic()
        if now - self._last_state_check < self.STATE_CHECK_INTERVAL:
            return False
        self._last_state_check = now
        return True

    def next_delay(self) -> float:
        delay = min(self._jittered(self._delay), self.remaining())
        self._delay = min(self._delay * 2, self.MAX_DELAY)
        return max(0.0, delay)

    def _jittered(self, delay: float) -> float:
        if self.JITTER == 0.0:
            return delay
        return delay * (1.0 + self.JITTER * (2.0 * self._random() - 1.0))


class ReconnectPoll(ReadinessPoll):
    """Calendario de la reconexión tras un corte: 0,5 s doblando hasta 4 s
    con ±25 % de jitter (varios handles de un proceso no sondean al unísono),
    `get-microvm` cada 5 s, y un tope de `reconnect_timeout` (60 s por
    defecto: los 30 s de `resumeTimeoutInSeconds` de la imagen más 30)."""

    INITIAL_DELAY: float = 0.5
    MAX_DELAY: float = 4.0
    JITTER: float = 0.25


@dataclass(frozen=True)
class ReconnectOutcome:
    """Resultado de `Sandbox._reconnect`: si el agente volvió a responder, si
    la generación de resume cambió (los streams del servidor se perdieron)
    y, si no volvió, la excepción con la que debe fallar el caller."""

    resumed: bool
    generation_changed: bool
    resume_generation: int
    error: Exception | None = None


MAX_FUTILE_RECONNECTS: Final = 3


class ReconnectBudget:
    """Cuenta las reconexiones seguidas que no vieron una generación nueva:
    un stream que el proxy corta una y otra vez con el agente vivo no se
    reabre para siempre; a la cuarta el corte se clasifica como en M2."""

    def __init__(self) -> None:
        self.futile = 0

    def allows(self, outcome: ReconnectOutcome) -> bool:
        if outcome.generation_changed:
            self.futile = 0
            return True
        self.futile += 1
        return self.futile <= MAX_FUTILE_RECONNECTS


def already_suspended(info: SandboxInfo) -> bool:
    """`suspend-microvm` es idempotente: sobre un VM `SUSPENDED` responde 200
    (medido 2026-09-16, nunca `ConflictException`), así que "no estaba
    `RUNNING`" se decide leyendo `get-microvm` antes de llamar."""
    return info.state in SUSPENDED_STATES


def needs_explicit_resume(info: SandboxInfo) -> bool:
    """`connect()` reanuda con `resume-microvm` un sandbox `SUSPENDED` sin
    auto-resume; con auto-resume el propio sondeo de `Health` lo despierta."""
    return info.state == "SUSPENDED" and not (info.idle and info.idle.auto_resume)


def is_suspending_reason(reason: Exception) -> bool:
    """Un corte causado por un `/suspend` (final en-stream `suspending` o el
    phase gate `UNAVAILABLE suspending`): sólo cuenta como reconectado un
    `Health` con una generación de resume nueva, porque el agente sigue
    respondiendo unos cientos de ms mientras el VM se congela."""
    if isinstance(reason, grpc.RpcError):
        return is_phase_gate(reason)
    return isinstance(reason, SandboxStateException)


def reconnect_failure(
    reason: Exception,
    *,
    info: SandboxInfo | None = None,
    timeout: float | None = None,
    wake: bool = True,
) -> Exception | None:
    """Por qué el sondeo de reconexión se detiene: un estado terminal de
    `get-microvm`, `SUSPENDED` sin auto-resume o el deadline agotado. `None`
    significa seguir sondeando. `SUSPENDED` sin auto-resume sólo detiene a
    un caller con `wake` (sus sondas de `Health` no van a despertar nada);
    un handle dormido espera al `resume()` sea cual sea la política."""
    if info is not None:
        if info.state in TERMINAL_STATES:
            return chained(terminal_state_error(info), reason)
        if wake and info.state == "SUSPENDED" and not (info.idle and info.idle.auto_resume):
            return chained(
                SandboxStateException(
                    f"el sandbox {info.sandbox_id} está suspendido sin auto-resume; llama a "
                    "resume() para reanudarlo"
                ),
                reason,
            )
        return None
    if timeout is None:
        return None
    if is_suspending_reason(reason):
        phase = rpc_details(reason) if isinstance(reason, grpc.RpcError) else "suspending"
        return chained(
            SandboxStateException(
                f"el sandbox está {phase} y no volvió a responder en {timeout:g} s"
            ),
            reason,
        )
    return chained(SandboxException(f"el sandbox no volvió a responder en {timeout:g} s"), reason)


def chained(error: Exception, cause: Exception) -> Exception:
    error.__cause__ = cause
    return error


def health_from_proto(response: health_pb2.HealthResponse) -> SandboxHealth:
    return SandboxHealth(
        agent_ready=bool(response.agent_ready),
        kernel_ready=bool(response.kernel_ready),
        agent_version=str(response.agent_version),
        uptime_ms=int(response.uptime_ms),
        sandbox_id=str(response.sandbox_id),
        resume_generation=int(response.resume_generation),
        clock_offset_ms=int(response.clock_offset_ms),
        kernel_state_lost=bool(response.kernel_state_lost),
        metadata=metadata_from_health(response),
        imds_blocked=bool(response.imds_blocked),
        hook_anomalies=int(response.hook_anomalies),
        lifecycle=lifecycle_from_proto(response),
        egress_enforcement=enforcement_from_proto(response.egress_enforcement),
        cpu_count=int(response.cpu_count),
        memory_total_bytes=int(response.memory_total_bytes),
    )


def metadata_from_health(response: health_pb2.HealthResponse) -> dict[str, str]:
    """El mapa `metadata` de `Health` como dict; vacío sobre una imagen
    anterior a M6 (el campo no existe y protobuf lo lee vacío)."""
    return {str(key): str(value) for key, value in response.metadata.items()}


MEBIBYTE: Final = 1024 * 1024


@dataclass(frozen=True)
class GuestFacts:
    """Lo que `SandboxInfo` toma de `Health`: la versión del agente y la
    vista del guest (CPUs y `MemTotal` en MiB). `None` = no leído o
    desconocido."""

    agent_version: str | None = None
    cpu_count: int | None = None
    memory_mb: int | None = None


def guest_facts_from_health(response: health_pb2.HealthResponse) -> GuestFacts:
    """Un agente anterior a M9 manda `cpu_count` y `memory_total_bytes` a 0,
    que aquí son `None`: 0 CPUs o 0 MiB nunca es un dato real."""
    return GuestFacts(
        agent_version=str(response.agent_version) or None,
        cpu_count=int(response.cpu_count) or None,
        memory_mb=int(response.memory_total_bytes) // MEBIBYTE or None,
    )


def ready_guest_facts(response: health_pb2.HealthResponse | None) -> GuestFacts:
    """Los hechos del guest sólo de un agente que respondió `agent_ready`
    (la misma regla que los metadatos de `Sandbox.get_info(sandbox_id)`)."""
    if response is None or not response.agent_ready:
        return GuestFacts()
    return guest_facts_from_health(response)


def with_guest_facts(info: SandboxInfo, facts: GuestFacts) -> SandboxInfo:
    return dataclasses.replace(
        info,
        agent_version=facts.agent_version,
        cpu_count=facts.cpu_count,
        memory_mb=facts.memory_mb,
    )


METADATA_PROBE_TIMEOUT_SECONDS: Final = 5.0
METADATA_LIST_STATES: Final[tuple[str, ...]] = ("RUNNING",)


def metadata_matches(candidate: Mapping[str, str] | None, wanted: Mapping[str, str]) -> bool:
    """Igualdad de subconjunto sobre las claves pedidas (cadenas exactas);
    un `wanted` vacío acepta cualquier sandbox con metadatos leídos."""
    if candidate is None:
        return False
    return all(candidate.get(key) == value for key, value in wanted.items())


def list_states_for_metadata(states: Iterable[str] | None) -> tuple[str, ...]:
    """`list(metadata=)` sólo sondea sandboxes `RUNNING`: los metadatos viven
    en el agente y una sonda de `Health` a un sandbox suspendido lo
    despertaría (AWS_API_NOTES.md Q4, Q40)."""
    if states is None:
        return METADATA_LIST_STATES
    requested = tuple(states)
    unsupported = sorted(set(requested) - set(METADATA_LIST_STATES))
    if unsupported:
        raise InvalidArgumentException(
            "list(metadata=) sólo filtra sandboxes RUNNING: los metadatos viven en el agente "
            f"y sondear un sandbox suspendido lo despertaría (recibido {unsupported})"
        )
    return METADATA_LIST_STATES


def metadata_probe_failure(sandbox_id: str, cause: Exception) -> SandboxException:
    """Una sonda de `Health` fallida nunca se convierte en un hueco silencioso
    de la lista: el caller recibe el id del sandbox que no respondió."""
    error = SandboxException(
        f"no se pudieron leer los metadatos del sandbox {sandbox_id}: "
        f"Health no respondió ({type(cause).__name__})"
    )
    error.__cause__ = cause
    return error


def health_ready(response: health_pb2.HealthResponse | None) -> bool:
    """Listo para la readiness de `create()`/`connect()`/`resume()`: agente y
    kernel listos y `sandbox_id` presente. El proxy deja pasar `Health`
    antes de que `rayd` reciba `/run`; ese `Health` trae el kernel del
    snapshot sin rotar y ningún `sandbox_id`, y la rotación de `/run` lo
    pondría `kernel_ready=false` justo después (regresión de M9,
    `AWS_API_NOTES.md` Q78)."""
    return (
        response is not None
        and response.agent_ready
        and response.kernel_ready
        and bool(response.sandbox_id)
    )


def health_reconnected(
    response: health_pb2.HealthResponse | None,
    *,
    seen_generation: int,
    suspending: bool,
    running: bool = False,
) -> bool:
    """`Health` cuenta como "volvió" cuando el agente y el kernel están
    listos; tras un `/suspend` además hace falta una generación nueva, salvo
    que `get-microvm`, leído después del corte, diga `RUNNING` (`running`):
    un `/suspend` que nadie checkpointeó (forjado por el proxy) no trae
    `/resume`, y `rayd` reabre su gate en ≤ 20 s sin subir la generación.
    Si el gate aún está cerrado, la resuscripción responde `UNAVAILABLE
    suspending` y se reintenta con backoff (`is_gate_refusal`)."""
    if response is None or not (response.agent_ready and response.kernel_ready):
        return False
    return not suspending or int(response.resume_generation) != seen_generation or running


def is_gate_refusal(exc: BaseException) -> bool:
    """La resuscripción tras un corte fue rechazada por el phase gate de
    `rayd` (`UNAVAILABLE suspending`, ya traducido): el agente vive y el gate
    reabrirá con el `/resume` o con la recuperación del watchdog."""
    return isinstance(exc, SandboxStateException) and exc.grpc_code is grpc.StatusCode.UNAVAILABLE


class GateRetry:
    """Reintentos de una resuscripción rechazada por el phase gate con el
    backoff de `ReconnectPoll` dentro de `reconnect_timeout`. Tras un
    `/suspend` que nadie checkpointeó el agente vive con el gate cerrado
    hasta el `/resume` o la recuperación del watchdog (≤ 20 s), así que
    `Connect`, `Pty.Connect`, `WatchDir` y `Reattach` reintentan en vez de
    matar el handle; el caller duerme lo que `retry_delay` le diga."""

    def __init__(self, reconnect_timeout: float) -> None:
        self._poll = ReconnectPoll(timeout=reconnect_timeout)

    def retry_delay(self, exc: BaseException) -> float | None:
        """Segundos a dormir antes de reintentar; `None` cuando la excepción
        no es un rechazo del gate o el presupuesto venció (el caller la
        levanta tal cual)."""
        if not is_gate_refusal(exc) or self._poll.timed_out():
            return None
        return self._poll.next_delay()


HOOK_ANOMALIES_WARNING: Final = (
    "sandbox %s: hooks forjados o repetidos detectados: %d (hook_anomalies; alguien "
    "posee un token allPorts de la VM)"
)
IMDS_OPEN_WARNING: Final = (
    "sandbox %s: las credenciales del execution role son legibles por el código del "
    "sandbox (imds_blocked=False): usa la imagen con capabilities o quita el rol"
)
IMDS_VERIFY_BUDGET_MS: Final = 10_000


def imds_open_warning_due(
    response: health_pb2.HealthResponse,
    *,
    execution_role_arn: str | None,
    ready_uptime_ms: int | None,
) -> bool:
    """Si este `Health` es el que debe disparar `IMDS_OPEN_WARNING`. Sin rol
    IMDS no sirve credenciales de nadie. Con rol, `imds_blocked=False` sólo
    es un veredicto cuando `rayd` ya terminó de verificar el bloqueo: lo hace
    en segundo plano tras `/run` con un presupuesto de 10 s
    (`IMDS_VERIFY_BUDGET`) y publica `False` mientras tanto (medido en la
    imagen con capabilities: `True` 0,1 s *después* de `kernel_ready`), así
    que el aviso espera a que el `uptime` del agente supere en ese
    presupuesto al del primer `Health` registrado (el de readiness, que
    llega después del `/run` que arranca la verificación)."""
    if not execution_role_arn or bool(response.imds_blocked) or ready_uptime_ms is None:
        return False
    return int(response.uptime_ms) - ready_uptime_ms >= IMDS_VERIFY_BUDGET_MS


def not_ready_error(
    info: SandboxInfo | None, *, ready_timeout: float, terminated: bool
) -> SandboxNotReadyException:
    state = info.state if info else None
    reason = info.state_reason if info else None
    detail = f" (state={state}, stateReason={reason})" if info else ""
    outcome = "; el MicroVM fue terminado" if terminated else ""
    return SandboxNotReadyException(
        f"el agente no respondió a Health en {ready_timeout:g} s{detail}{outcome}",
        state=state,
        state_reason=reason,
    )


def terminal_state_error(info: SandboxInfo) -> SandboxNotFoundException:
    """Un MicroVM `TERMINATING|TERMINATED` ya no existe para el SDK; si
    `rayd` salió por el plazo lógico (`timed_out`), el mensaje lo dice."""
    if info.timed_out:
        return SandboxNotFoundException(
            f"el sandbox {info.sandbox_id} alcanzó su timeout y está {info.state}: "
            f"{info.state_reason}"
        )
    return SandboxNotFoundException(
        f"el sandbox {info.sandbox_id} está {info.state}: {info.state_reason or 'sin stateReason'}"
    )


def info_with_health(info: SandboxInfo, response: health_pb2.HealthResponse | None) -> SandboxInfo:
    """`Sandbox.get_info(sandbox_id)`: los metadatos y el plazo lógico del
    único `Health` sondeado; sin respuesta (el sandbox no está `RUNNING`)
    ninguno de los dos, y metadatos `None` mientras el agente arranca."""
    if response is None:
        return dataclasses.replace(info, metadata=None)
    lifecycle = lifecycle_from_proto(response)
    if not response.agent_ready:
        logger.info("sandbox %s aún arrancando: sin metadatos", info.sandbox_id)
        return dataclasses.replace(info, metadata=None, lifecycle=lifecycle)
    return dataclasses.replace(info, metadata=metadata_from_health(response), lifecycle=lifecycle)


def terminated_during_boot_error(info: SandboxInfo) -> SandboxNotReadyException:
    return SandboxNotReadyException(
        f"el MicroVM pasó a {info.state} antes de estar listo: "
        f"{info.state_reason or 'sin stateReason'}",
        state=info.state,
        state_reason=info.state_reason,
    )
