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
from datetime import datetime, timedelta
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
from rayito._lifecycle_base import (
    CONNECT_CAP_SAFETY_MS,
    DEFAULT_IDLE_POLICY,
    LifetimeBeyondCapException,
    default_max_lifetime_for,
    instant_from_unix_ms,
)
from rayito._limits import (
    AGENT_FLEET_PING_TTL_SECONDS,
    AGENT_SERVICE_QUEUE_TIMEOUT_SECONDS_DEFAULT,
    LIFECYCLE_CAP_MARGIN_SECONDS,
    LIFECYCLE_MIN_TIMEOUT_SECONDS,
    MAX_DURATION_SECONDS,
)
from rayito._models import TimeoutActionName
from rayito._pool_base import (
    DEFAULT_MIN_REMAINING_SECONDS,
    MIN_REMAINING_FLOOR_SECONDS,
    PoolConfig,
)
from rayito.exceptions import (
    InvalidArgumentException,
    RateLimitException,
    SandboxException,
    TimeoutException,
)

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
#: Cuántos VMs `RUNNING` sondea a la vez el listado de la flota (un
#: `get-microvm` más un `Health` sin token cada uno). Los resultados y los
#: avisos salen en el orden de `list-microvms`.
AGENT_FLEET_PROBE_CONCURRENCY: Final = 8
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

#: La acción al vencer de los VMs de una flota que puede crecer: `rayd`
#: termina el VM (ADR-011, `AWS_API_NOTES.md` Q63). Es lo que hace que un
#: `set_timeout` de reducción se acepte.
AGENT_FLEET_ON_TIMEOUT: Final[TimeoutActionName] = "kill"
#: Margen bajo el tope de `rayd` al acotar la vida de un VM (desfase de
#: relojes): el mismo que deja `connect()` al reabrir un sandbox
#: (`CONNECT_CAP_SAFETY_MS`).
AGENT_FLEET_CAP_SAFETY_SECONDS: Final = CONNECT_CAP_SAFETY_MS // 1000

#: Lo que se loguea una vez por VM cuando `rayd` rechaza acotar su vida.
UNMANAGED_LIFETIME_MESSAGE: Final = (
    "un VM de la flota no tiene plazo lógico (se creó antes de 0.17 o con max_vms igual a "
    "min_vms): no se reducirá solo; termínalo con stop() o vuelve a desplegar"
)

#: Lo que se loguea (sólo el id y la clase del error) cuando la sonda de un
#: VM `RUNNING` falla al listar la flota: se trata como un VM que termina.
UNREADABLE_MEMBER_MESSAGE: Final = (
    "el VM %s de la flota no respondió a Health al listarla (%s): se trata como un VM que termina"
)
#: Lo que se loguea (sólo el id) cuando la flota no termina un VM porque no
#: pudo probar que es suyo: lo deja a su propio plazo y a su política de idle.
UNPROVEN_MEMBER_MESSAGE: Final = (
    "no se termina el VM %s: no se pudo probar que sea de la flota; se deja a su propio plazo"
)

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


#: Los errores de la sonda de un VM que no dicen nada del VM sino del plano de
#: control (una limitación o un plazo de `get-microvm`): hacen fallar el
#: listado en vez de dejar fuera a un miembro sano.
LISTING_CONTROL_PLANE_ERRORS: Final = (RateLimitException, TimeoutException)


def probed_running(info: Any) -> Any | None:
    """La `SandboxInfo` sondeada de un VM si sigue `RUNNING`; `None` si dejó
    de estarlo entre `list-microvms` y la sonda (lo que pasa con un VM que se
    termina al reducir la flota)."""
    return info if info.state == AGENT_FLEET_PROBE_STATE else None


def select_members(
    items: Sequence[MemberListing],
    owned: Collection[str],
    unreadable: Collection[str] = frozenset(),
) -> list[MemberListing]:
    """Los VMs de una imagen que cuentan como miembros de la flota. Un VM
    `RUNNING` lo es sólo si sus etiquetas lo prueban (`owned`); los que no
    están `RUNNING` no se pueden sondear sin despertarlos, así que cuentan
    salvo que la imagen ya haya demostrado tener VMs que no son de un agente:
    entonces ninguno se da por miembro, para no terminar sandboxes ajenos.

    `unreadable` son los VMs `RUNNING` cuya sonda falló o que dejaron de
    estar `RUNNING` al sondearlos: la plataforma aún los lista, pero su
    `rayd` ya no responde (un VM que se está terminando al reducir la
    flota). No cuentan como miembros ni prueban que la imagen sea
    compartida: se tratan como VMs que terminan. Como tampoco prueban lo
    contrario (uno podría ser un sandbox ajeno), con alguno los miembros que
    no están `RUNNING` salen con `proven=False`: cuentan para el cupo, pero
    la flota nunca los termina."""
    shared_image = any(
        item.state == AGENT_FLEET_PROBE_STATE
        and item.sandbox_id not in owned
        and item.sandbox_id not in unreadable
        for item in items
    )
    unproven = bool(unreadable)
    selected: list[MemberListing] = []
    for item in items:
        if item.state == AGENT_FLEET_PROBE_STATE:
            if item.sandbox_id in owned:
                selected.append(item)
        elif not shared_image:
            selected.append(dataclasses.replace(item, proven=not unproven))
    return selected


@dataclass(frozen=True)
class MemberProbe:
    """La sonda de un VM `RUNNING` del listado: su `SandboxInfo` si sigue
    `RUNNING` (`None` si no) y, si la sonda falló, la clase del error."""

    sandbox_id: str
    info: Any | None
    error: str | None = None


def select_probed(
    items: Sequence[MemberListing], probes: Sequence[MemberProbe]
) -> list[MemberListing]:
    """`select_members` con las sondas de los VMs `RUNNING`: las que trajeron
    una `SandboxInfo` prueban (o no) las etiquetas; las demás son
    `unreadable`."""
    readable = [probe.info for probe in probes if probe.info is not None]
    unreadable = {probe.sandbox_id for probe in probes if probe.info is None}
    return select_members(items, owned_ids(readable), unreadable)


def member_of(known: Member | None, item: MemberListing, cap: datetime | None) -> Member:
    """El miembro de un VM listado: el que este proceso ya conocía (que pasa
    a probado si el listado lo prueba) o uno nuevo con la prueba del
    listado."""
    if known is None:
        return Member(item.sandbox_id, cap=cap, proven=item.proven)
    known.proven = known.proven or item.proven
    return known


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
    sin uso. `cap` es el tope de `rayd` del VM (`rayd_cap`; `None` si no se
    conoce), `limited` dice si este proceso le acortó el plazo y
    `lifetime_rejected` que su `set_timeout` se rechazó por un argumento (un
    VM sin plazo lógico: no se reintenta). `proven` dice si se sabe que el VM
    es de la flota (lo creó este proceso, sus etiquetas lo prueban o la
    imagen sólo tiene agentes); uno sin probar nunca se termina."""

    sandbox_id: str
    deployment: Any | None = None
    extra: bool = False
    inflight: int = 0
    bound: set[str] = field(default_factory=set)
    health: MemberHealth = UNKNOWN_HEALTH
    pinged_at: float | None = None
    busy_until: float = 0.0
    failed_pings: int = 0
    cap: datetime | None = None
    limited: bool = False
    lifetime_rejected: bool = False
    proven: bool = True


@dataclass(frozen=True)
class MemberListing:
    """Un VM de la imagen de la flota según `list-microvms`."""

    sandbox_id: str
    state: str
    started_at: datetime
    proven: bool = True


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


@dataclass(frozen=True)
class FleetLifetime:
    """Los argumentos de vida con los que se crea cada VM de una flota que
    puede crecer (y cada plaza caliente): el plazo lógico en modo `kill` y su
    tope. Sin ellos `rayd` rechaza el `set_timeout` que termina un VM
    sobrante."""

    max_lifetime: int
    on_timeout: TimeoutActionName = AGENT_FLEET_ON_TIMEOUT


def scales_in(scaling: ScalingOptions) -> bool:
    """Si la flota puede tener VMs por encima de `min_vms`."""
    return scaling.max_vms > scaling.min_vms


def fleet_lifetime(project: AgentProject, scaling: ScalingOptions) -> FleetLifetime | None:
    """La vida de los VMs de la flota: `None` si nunca puede haber un VM
    sobrante (`max_vms == min_vms`), y así se crean byte a byte como en
    0.16. Si puede, `serve.timeout_seconds` sigue siendo la vida de un VM
    base (ahora como plazo lógico) y el tope es el de `Sandbox.create()` por
    defecto, `timeout + 60` (como mucho 28 800 s)."""
    if not scales_in(scaling):
        return None
    timeout = seconds_ceiling(project.serve.timeout_seconds)
    return FleetLifetime(max_lifetime=default_max_lifetime_for(timeout))


def rayd_cap(platform_cap: datetime) -> datetime:
    """El tope que impone `rayd` a partir del de la plataforma: `rayd` lo
    fija en `/run + max_lifetime - 60 s` (`LIFECYCLE_CAP_MARGIN_SECONDS`,
    `CAP_MARGIN` de `rayd-core`) y rechaza cualquier plazo posterior. `/run`
    llega unos segundos después del `startedAt` de la plataforma, así que
    contar desde `startedAt` (o desde antes de `create()`) se queda del lado
    seguro."""
    return platform_cap - timedelta(seconds=LIFECYCLE_CAP_MARGIN_SECONDS)


def lifetime_seconds(desired: float | None, cap: datetime | None, now: datetime) -> int | None:
    """Los segundos que se piden en `set_timeout`: `desired` redondeado
    hacia arriba (o, con `None`, todo lo que queda) y nunca más allá del
    tope de `rayd` (`cap`, ver `rayd_cap`) menos
    `AGENT_FLEET_CAP_SAFETY_SECONDS`, porque `rayd` rechaza un plazo más
    allá de su tope. `None` si no hay nada que pedir: el tope está
    demasiado cerca (el VM termina antes de todos modos) o, sin `desired`,
    no se conoce."""
    if cap is None:
        return None if desired is None else seconds_ceiling(desired)
    remaining = int((cap - now).total_seconds()) - AGENT_FLEET_CAP_SAFETY_SECONDS
    if remaining < LIFECYCLE_MIN_TIMEOUT_SECONDS:
        return None
    return remaining if desired is None else min(seconds_ceiling(desired), remaining)


def corrected_cap(error: LifetimeBeyondCapException, current: datetime | None) -> datetime | None:
    """El tope que `rayd` informó al rechazar un plazo más allá de él, si
    adelanta el que la flota estimaba (`current`): con él se reintenta una
    vez. `None` si el rechazo no trae tope o no corrige nada (no hay nada que
    reintentar)."""
    if error.cap_unix_ms is None:
        return None
    reported = instant_from_unix_ms(error.cap_unix_ms)
    if reported is None or (current is not None and reported >= current):
        return None
    return reported


def restore_seconds(cap: datetime | None, now: datetime) -> int | None:
    """Los segundos que devuelven toda su vida a un VM que este proceso
    acortó y ahora es base: todo lo que queda hasta el tope de `rayd` si se
    conoce y, si no, `MAX_DURATION_SECONDS` (el máximo de la plataforma), que
    `rayd` rechaza informando su tope (`LifetimeBeyondCapException`) para
    reintentar con él (`corrected_cap`). `None` si el tope está demasiado
    cerca: el VM termina antes de todos modos."""
    if cap is None:
        return MAX_DURATION_SECONDS
    return lifetime_seconds(None, cap, now)


def hold_seconds(scaling: ScalingOptions, request_timeout: float | None) -> float:
    """El plazo de un VM sobrante mientras atiende una invocación: nunca
    menos que lo que puede durar la petición, para que la reducción no lo
    termine a media llamada."""
    return max(scaling.scale_in_after_seconds, request_timeout or 0.0)


def sandbox_cap(sandbox: Any) -> datetime | None:
    """El tope de `rayd` de un sandbox abierto, sin llamar a AWS: el que
    informó el propio agente (`SandboxInfo.lifecycle.cap`) o, si no se leyó,
    el de la plataforma (`platform_expires_at`) pasado por `rayd_cap`.
    `None` si el objeto no trae ninguno."""
    info = getattr(sandbox, "info", None)
    reported = getattr(getattr(info, "lifecycle", None), "cap", None)
    if isinstance(reported, datetime):
        return reported
    platform = getattr(info, "platform_expires_at", None)
    return rayd_cap(platform) if isinstance(platform, datetime) else None


def listed_cap(started_at: datetime, lifetime: FleetLifetime | None) -> datetime | None:
    """El tope de `rayd` de un VM que arrancó en `started_at` (el listado,
    o el instante previo a `create()` para uno que la flota crea), si la
    flota sabe con qué vida se crean sus VMs."""
    if lifetime is None:
        return None
    return rayd_cap(started_at + timedelta(seconds=lifetime.max_lifetime))


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
    red, y el plazo lógico de `fleet_lifetime`) y el token único de la
    flota. Una plaza se recicla cuando le queda menos vida de la que
    `min_remaining_seconds` exige: por defecto una hora, o lo que deje el
    plazo del servicio. `template_version` fija la versión
    de la imagen (la que eligió el build del despliegue); `None` usa la más
    reciente activa."""
    serve = project.serve
    timeout = seconds_ceiling(serve.timeout_seconds)
    min_remaining = min(DEFAULT_MIN_REMAINING_SECONDS, timeout - MIN_REMAINING_FLOOR_SECONDS)
    lifetime = fleet_lifetime(project, scaling)
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
        max_lifetime=None if lifetime is None else lifetime.max_lifetime,
        on_timeout=None if lifetime is None else lifetime.on_timeout,
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
