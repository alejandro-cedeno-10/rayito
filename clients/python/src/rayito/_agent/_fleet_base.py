"""Lo que comparten `AgentFleet` y `AsyncAgentFleet` (ADR-029, D5), sin E/S:
el id de sesión enrutable (`rs1.<sandbox>.<sesión>`), el registro de cada
miembro, la decisión de dónde colocar una sesión, la salud por pings, el plazo
de la cola y las opciones del pool de plazas calientes.

Nada aquí llama a AWS ni crea clientes: la flota (`_fleet.py`,
`_fleet_async.py`) trae sus dependencias por constructor y este módulo sólo
decide.
"""

from __future__ import annotations

import dataclasses
import hashlib
import uuid
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final, Literal

from rayito._agent._deployment_base import parse_labels
from rayito._agent._events import AgentEvent, AgentFailed, AgentResult, Done
from rayito._agent._project_base import seconds_ceiling
from rayito._agent._scaling_domain import (
    SESSIONS_DEDICATED,
    ScalingOptions,
    SessionHandle,
    decode_handle,
    encode_handle,
    is_unhealthy,
)
from rayito._lifecycle_base import DEFAULT_IDLE_POLICY
from rayito._limits import (
    AGENT_FLEET_PING_TTL_SECONDS,
    AGENT_SERVICE_QUEUE_TIMEOUT_SECONDS_DEFAULT,
)
from rayito._pool_base import (
    DEFAULT_MIN_REMAINING_SECONDS,
    MIN_REMAINING_FLOOR_SECONDS,
    PoolConfig,
)
from rayito.exceptions import InvalidArgumentException, SandboxException

if TYPE_CHECKING:
    from rayito._agent._deployment_base import DeploymentState
    from rayito._agent._project_domain import AgentProject

#: Etiqueta del sandbox de un VM dedicado: el hash de la sesión que el
#: llamante eligió (`session_index`). Nunca el id en claro: las etiquetas
#: viajan en el `runHookPayload`.
AGENT_FLEET_SESSION_LABEL: Final = "rayito.agent.session"
SESSION_LABEL_HEX_CHARS: Final = 32
#: Espera entre dos intentos de colocar una sesión cuando la flota está llena.
AGENT_FLEET_BUSY_RETRY_SECONDS: Final = 1.0
#: Cuántos VMs crea a la vez `start()` (y `scale()`).
AGENT_FLEET_START_CONCURRENCY: Final = 4
#: Plazo para que las plazas calientes de un `deploy_agent` queden aparcadas:
#: un `create()` más su `pause()` son decenas de segundos
#: (`AWS_API_NOTES.md`, Q155), con holgura para varias a la vez.
AGENT_FLEET_WARM_READY_TIMEOUT_SECONDS: Final = 600.0
AGENT_FLEET_WARM_POLL_SECONDS: Final = 2.0
#: Estados de `list-microvms` que cuentan como miembro de la flota.
AGENT_FLEET_LIVE_STATES: Final[tuple[str, ...]] = (
    "PENDING",
    "RUNNING",
    "SUSPENDING",
    "SUSPENDED",
)
#: Estado de `list-microvms` en el que se puede leer el agente del VM (sus
#: etiquetas) sin despertarlo.
AGENT_FLEET_PROBE_STATE: Final = "RUNNING"
#: Códigos nuevos de las excepciones existentes (ver `exceptions.py`).
AGENT_CAPACITY_CODE: Final = "agent_capacity"
AGENT_VM_LOST_CODE: Final = "agent_vm_lost"

MemberHealth = Literal["healthy", "busy", "unavailable", "unknown"]
#: De dónde sale el escalado de una flota: `scaling=` explícito, el
#: proyecto, la receta guardada por `deploy_agent()` o, sin ninguno, los
#: valores por defecto (que no dicen nada de la flota real).
ScalingSource = Literal["explicit", "project", "recipe", "default"]
SCALING_SOURCES: Final[tuple[str, ...]] = ("explicit", "project", "recipe", "default")
PlanKind = Literal["use", "grow", "wait"]
UNKNOWN_HEALTH: Final = "unknown"


def owned_ids(items: Iterable[Any]) -> set[str]:
    """Los ids de los VMs sondeados cuyas etiquetas son las de un despliegue
    de agente de Rayito (`parse_labels`): los únicos que se sabe que son de
    una flota."""
    owned: set[str] = set()
    for item in items:
        try:
            parse_labels(item.metadata)
        except InvalidArgumentException:
            continue
        owned.add(item.sandbox_id)
    return owned


def select_members(items: Sequence[MemberListing], owned: Collection[str]) -> list[MemberListing]:
    """Los VMs de una imagen que cuentan como miembros de la flota. Un VM
    `RUNNING` lo es sólo si sus etiquetas lo prueban (`owned`); los que no
    están `RUNNING` no se pueden sondear sin despertarlos, así que cuentan
    salvo que la imagen ya haya demostrado tener VMs que no son de un agente:
    entonces ninguno se da por miembro, para no terminar sandboxes ajenos."""
    shared_image = any(
        item.state == AGENT_FLEET_PROBE_STATE and item.sandbox_id not in owned for item in items
    )
    return [
        item
        for item in items
        if (item.sandbox_id in owned if item.state == AGENT_FLEET_PROBE_STATE else not shared_image)
    ]


def route_of(session_id: str | None) -> SessionHandle | None:
    """El destino de `session_id` si es un id de sesión enrutable de la flota;
    `None` si es un id cualquiera (el del llamante o el de un despliegue de un
    solo VM)."""
    return None if session_id is None else decode_handle(session_id)


def session_label(session_id: str) -> str:
    """El valor de `AGENT_FLEET_SESSION_LABEL` de una sesión elegida por el
    llamante: un hash, para que el id no viaje en las etiquetas."""
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:SESSION_LABEL_HEX_CHARS]


def with_handle(result: AgentResult, sandbox_id: str) -> AgentResult:
    """El resultado con el id de sesión enrutable de `sandbox_id`."""
    if not result.session_id:
        return result
    return dataclasses.replace(result, session_id=encode_handle(sandbox_id, result.session_id))


def event_with_handle(event: AgentEvent, sandbox_id: str) -> AgentEvent:
    """El evento con su id de sesión enrutable, si el evento lleva uno."""
    if isinstance(event, Done | AgentFailed) and event.session_id:
        return dataclasses.replace(event, session_id=encode_handle(sandbox_id, event.session_id))
    return event


@dataclass
class Member:
    """Un VM de la flota tal como lo ve este proceso. `deployment` es el
    cliente (`None` hasta que se usa: abrir un VM suspendido lo reanuda);
    `inflight` son las invocaciones en curso desde este proceso; `bound` las
    sesiones (o reservas) de un VM dedicado; `busy_until` hasta cuándo se da
    por ocupado tras un 429 o un `HealthyBusy`; `extra` marca un VM por
    encima de `min_vms`, que se termina solo tras `scale_in_after_seconds`
    sin uso."""

    sandbox_id: str
    deployment: Any | None = None
    extra: bool = False
    inflight: int = 0
    bound: set[str] = field(default_factory=set)
    health: MemberHealth = UNKNOWN_HEALTH
    pinged_at: float | None = None
    busy_until: float = 0.0
    failed_pings: int = 0


@dataclass(frozen=True)
class MemberListing:
    """Un VM de la imagen de la flota según `list-microvms`."""

    sandbox_id: str
    state: str
    started_at: datetime


@dataclass(frozen=True)
class FleetMemberStatus:
    sandbox_id: str
    state: MemberHealth
    extra: bool
    inflight: int


@dataclass(frozen=True)
class FleetStatus:
    """El estado de una flota: sus miembros, cuántos están sanos u ocupados y
    los límites de `ScalingOptions`. Un miembro que este proceso aún no abrió
    figura como `unknown`: preguntarle lo reanudaría. `scaling_source` dice
    de dónde salen los límites: con `default` (sin proyecto ni receta) no
    describen la flota real."""

    name: str
    members: tuple[FleetMemberStatus, ...]
    min_vms: int
    max_vms: int
    warm_vms: int
    warm_ready: int
    scaling_source: ScalingSource = "default"

    @property
    def healthy(self) -> int:
        return sum(1 for member in self.members if member.state in ("healthy", "busy"))

    @property
    def busy(self) -> int:
        return sum(1 for member in self.members if member.state == "busy")


@dataclass(frozen=True)
class Placement:
    """La decisión de `plan_placement`: usar un miembro, crear uno o esperar."""

    kind: PlanKind
    member: Member | None = None


def ping_is_fresh(member: Member, now: float) -> bool:
    return member.pinged_at is not None and now - member.pinged_at < AGENT_FLEET_PING_TTL_SECONDS


def record_ping(member: Member, state: DeploymentState, now: float) -> None:
    """Anota un ping: un `unavailable` suma al contador de pings fallidos, un
    `busy` da al miembro por ocupado hasta que el ping caduque y cualquier
    otra respuesta lo pone todo a cero."""
    member.health = state
    member.pinged_at = now
    member.failed_pings = member.failed_pings + 1 if state == "unavailable" else 0
    member.busy_until = now + AGENT_FLEET_PING_TTL_SECONDS if state == "busy" else 0.0


def record_busy(member: Member, now: float) -> None:
    """Anota un 429 del host: el miembro cuenta como ocupado hasta el
    siguiente intento de la cola (`AGENT_FLEET_BUSY_RETRY_SECONDS`)."""
    member.health = "busy"
    member.busy_until = now + AGENT_FLEET_BUSY_RETRY_SECONDS


def is_lost(member: Member) -> bool:
    return is_unhealthy(member.failed_pings)


def is_free(member: Member, scaling: ScalingOptions, max_concurrency: int, now: float) -> bool:
    """Si `member` puede aceptar una sesión nueva según lo que este proceso
    sabe. Un `unavailable` reciente nunca; uno viejo se vuelve a sondear, y
    tras `AGENT_FLEET_UNHEALTHY_PINGS` fallos seguidos se sustituye. Dedicado:
    sin ninguna sesión asignada. Compartido: con cupo (`inflight <
    max_concurrency`) y sin `busy_until` por delante."""
    if member.health == "unavailable" and ping_is_fresh(member, now):
        return False
    if scaling.sessions == SESSIONS_DEDICATED:
        return not member.bound
    return member.inflight < max_concurrency and now >= member.busy_until


def choose_member(
    members: Iterable[Member], scaling: ScalingOptions, max_concurrency: int, now: float
) -> Member | None:
    """El miembro libre con menos invocaciones en curso (el primero en caso de
    empate: así los VMs se llenan por orden y los sobrantes se apagan)."""
    free = [member for member in members if is_free(member, scaling, max_concurrency, now)]
    return min(free, key=lambda member: member.inflight, default=None)


def plan_placement(
    members: Sequence[Member],
    growing: int,
    scaling: ScalingOptions,
    max_concurrency: int,
    now: float,
) -> Placement:
    """Dónde va una sesión nueva: en un miembro libre; si no hay y la flota
    puede crecer (contando los VMs que ya se están creando), en uno nuevo; si
    no, a esperar."""
    member = choose_member(members, scaling, max_concurrency, now)
    if member is not None:
        return Placement("use", member)
    if len(members) + growing < scaling.max_vms:
        return Placement("grow")
    return Placement("wait")


def new_reservation() -> str:
    """La reserva con la que una sesión nueva ocupa un VM dedicado hasta
    conocer su id."""
    return f"reserva-{uuid.uuid4().hex}"


def surplus(members: Sequence[Member], scaling: ScalingOptions) -> list[Member]:
    """Los miembros que sobran por encima de `max_vms`, los más nuevos
    primero."""
    excess = len(members) - scaling.max_vms
    return list(reversed(members))[:excess] if excess > 0 else []


def mark_extras(members: Sequence[Member], scaling: ScalingOptions) -> None:
    """Marca como `extra` los miembros más allá de los `min_vms` primeros."""
    for position, member in enumerate(members):
        member.extra = position >= scaling.min_vms


def capacity_message(scaling: ScalingOptions) -> str:
    kind = "dedicadas" if scaling.sessions == SESSIONS_DEDICATED else "compartidas"
    return (
        f"la flota no tiene cupo: sus {scaling.max_vms} VMs están ocupados "
        f"(sesiones {kind}) y se agotó el plazo de la cola"
    )


def vm_lost_message() -> str:
    return "el VM de esta sesión ya no existe: la sesión se perdió"


def pool_config_for(
    project: AgentProject,
    scaling: ScalingOptions,
    access_token: str,
    metadata: Mapping[str, str],
    *,
    template_version: str | None = None,
) -> PoolConfig:
    """La configuración del pool de plazas calientes de una flota: las
    mismas opciones de lanzamiento que un VM servido (imagen, vida, idle,
    red) y el token único de la flota. Una plaza se recicla cuando le queda
    menos vida de la que `min_remaining_seconds` exige: por defecto una hora,
    o lo que deje el plazo del servicio. `template_version` fija la versión
    de la imagen (la que eligió el build del despliegue); `None` usa la más
    reciente activa."""
    serve = project.serve
    timeout = seconds_ceiling(serve.timeout_seconds)
    min_remaining = min(DEFAULT_MIN_REMAINING_SECONDS, timeout - MIN_REMAINING_FLOOR_SECONDS)
    return PoolConfig(
        size=scaling.warm_vms,
        template=project.template or project.name,
        template_version=template_version,
        timeout=timeout,
        min_remaining_seconds=min_remaining,
        idle=serve.idle or DEFAULT_IDLE_POLICY,
        metadata=metadata,
        envs=dict(project.env) or None,
        egress=project.egress,
        network=project.network,
        allow_internet_access=project.allow_internet_access,
        access_token=access_token,
    )


@dataclass(frozen=True)
class FleetSettings:
    """Lo que la flota lee de un `AgentProject` (o de su receta): el
    escalado y de dónde sale, el plazo de la cola y la concurrencia por VM."""

    scaling: ScalingOptions
    queue_timeout_seconds: float
    max_concurrency: int
    scaling_source: ScalingSource = "default"


def scaling_source_of(
    project: AgentProject | None,
    scaling: ScalingOptions | None,
    recipe_scaling: ScalingOptions | None,
) -> ScalingSource:
    """De dónde sale el escalado: lo explícito gana a la receta, y la receta
    (que guarda las banderas del despliegue) al proyecto."""
    if scaling is not None:
        return "explicit"
    if recipe_scaling is not None:
        return "recipe"
    return "default" if project is None else "project"


def fleet_settings_of(
    project: AgentProject | None,
    scaling: ScalingOptions | None = None,
    recipe_scaling: ScalingOptions | None = None,
) -> FleetSettings:
    """Un único sitio donde la flota mira el proyecto: un `scaling` explícito
    gana al de la receta (`recipe_scaling`) y éste al del proyecto
    (`AgentProject.scaling`), y la cola cliente comparte el plazo de la cola
    del host. Una flota reconectada sin proyecto ni receta usa los valores por
    defecto."""
    source = scaling_source_of(project, scaling, recipe_scaling)
    chosen = scaling or recipe_scaling
    if project is None:
        return FleetSettings(
            scaling=chosen or ScalingOptions(),
            queue_timeout_seconds=AGENT_SERVICE_QUEUE_TIMEOUT_SECONDS_DEFAULT,
            max_concurrency=1,
            scaling_source=source,
        )
    project_scaling = getattr(project, "scaling", None)
    queue_timeout = getattr(project.serve, "queue_timeout_seconds", None)
    return FleetSettings(
        scaling=chosen or project_scaling or ScalingOptions(),
        queue_timeout_seconds=float(
            AGENT_SERVICE_QUEUE_TIMEOUT_SECONDS_DEFAULT if queue_timeout is None else queue_timeout
        ),
        max_concurrency=project.serve.max_concurrency,
        scaling_source=source,
    )


class ParkedSlotException(SandboxException):
    """Una plaza aparcada que no pudo abrir las pasarelas del proyecto: la
    flota ya la sacó y la sustituyó (`_replace`), y quien la eligió busca
    otra."""
