"""`AgentFleet`: uno o varios MicroVMs que sirven un mismo agente, con las
sesiones repartidas entre ellos (ADR-029, D5). Es la superficie de
`deploy_agent()` (`_deploy_facade.py`); el reparto, el id de sesión enrutable y
la salud son lógica pura de `_fleet_base.py`, y esta clase sólo hace la E/S.

Sin servidor propio: el enrutado es del cliente. La flota vive en el proceso
que la usa; los VMs no dependen de él (se reconectan con
`AgentFleet.connect()` y se terminan solos por sus plazos).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Self

from rayito._agent._deployment import AgentDeployment, default_transport
from rayito._agent._deployment_base import labels_for, parse_labels
from rayito._agent._events import AgentEvent, AgentResult
from rayito._agent._fleet_base import (
    AGENT_CAPACITY_CODE,
    AGENT_FLEET_BUSY_RETRY_SECONDS,
    AGENT_FLEET_LIVE_STATES,
    AGENT_FLEET_PROBE_STATE,
    AGENT_FLEET_SESSION_LABEL,
    AGENT_FLEET_START_CONCURRENCY,
    AGENT_VM_LOST_CODE,
    FleetMemberStatus,
    FleetSettings,
    FleetStatus,
    Member,
    MemberListing,
    capacity_message,
    event_with_handle,
    fleet_settings_of,
    is_lost,
    mark_extras,
    new_reservation,
    owned_ids,
    ping_is_fresh,
    plan_placement,
    pool_config_for,
    record_busy,
    record_ping,
    route_of,
    select_members,
    session_label,
    surplus,
    vm_lost_message,
    with_handle,
)
from rayito._agent._project_base import request_timeout
from rayito._agent._project_service import ProjectService
from rayito._agent._scaling_domain import (
    SESSIONS_DEDICATED,
    ScalingOptions,
    encode_handle,
    replacements_needed,
    validate_caller_session_id,
)
from rayito._limits import AGENT_SERVICE_READY_MARKER_PATH
from rayito.exceptions import (
    AgentException,
    CapacityException,
    InvalidArgumentException,
    SandboxException,
    SandboxNotFoundException,
)

if TYPE_CHECKING:
    from rayito._agent._deployment_base import DeploymentStatus
    from rayito._agent._project_domain import AgentProject
    from rayito._index import DynamoDbIndex
    from rayito._pool_backends import PoolBackend
    from rayito.sandbox_sync.pool import SandboxPool

logger = logging.getLogger("rayito.agent")

NO_TOKEN_MESSAGE = (
    "esta flota se reconectó sin access token: sólo puede listarse o terminarse; "
    "pasa access_token= a AgentFleet.connect() para usarla"
)
NO_PROJECT_MESSAGE = (
    "esta flota se reconectó sin proyecto: no puede crear ni arrancar VMs; "
    "pasa project= a AgentFleet.connect()"
)


class SandboxDirectory:
    """El adaptador de producción de la flota: lista los VMs de la imagen
    (`list-microvms` con el filtro de imagen) sin contar los que no son de un
    agente (`select_members`: los `RUNNING` se sondean con un `Health`, sin
    token ni despertar a nadie), conecta uno con el token de la flota y busca
    por sesión en el índice opcional."""

    def __init__(
        self, *, region: str | None = None, session: Any = None, sandbox_class: Any = None
    ) -> None:
        self._region = region
        self._session = session
        self._sandbox_class = sandbox_class

    def list_members(self, template: str) -> list[MemberListing]:
        sandbox_class = self._sandbox()
        listed = [
            MemberListing(item.sandbox_id, item.state, item.started_at)
            for item in sandbox_class.list(
                template=template, states=AGENT_FLEET_LIVE_STATES, **self._options()
            )
        ]
        probed = sandbox_class.list(
            template=template, states=(AGENT_FLEET_PROBE_STATE,), metadata={}, **self._options()
        )
        return select_members(listed, owned_ids(probed))

    def connect(self, sandbox_id: str, access_token: str) -> Any:
        return self._sandbox().connect(sandbox_id, access_token=access_token, **self._options())

    def kill(self, sandbox_id: str) -> bool:
        return bool(self._sandbox().kill(sandbox_id, **self._options()))

    def find_session(self, template: str, label: str, index: DynamoDbIndex) -> str | None:
        items = self._sandbox().list(
            template=template,
            states=AGENT_FLEET_LIVE_STATES,
            metadata={AGENT_FLEET_SESSION_LABEL: label},
            index=index,
            **self._options(),
        )
        return next((item.sandbox_id for item in items), None)

    def _options(self) -> dict[str, Any]:
        options = {"region": self._region, "session": self._session}
        return {key: value for key, value in options.items() if value}

    def _sandbox(self) -> Any:
        if self._sandbox_class is not None:
            return self._sandbox_class
        from rayito.sandbox_sync import Sandbox

        return Sandbox


class AgentFleetStream:
    """El stream de una invocación de la flota: los eventos del VM con el id
    de sesión enrutable, y el VM liberado al cerrarlo."""

    def __init__(
        self,
        stream: Any,
        *,
        sandbox_id: str,
        routable: bool,
        release: Callable[[str | None], None],
    ) -> None:
        self._stream = stream
        self._sandbox_id = sandbox_id
        self._routable = routable
        self._release = release
        self._released = False

    @property
    def session_id(self) -> str | None:
        inner = self._stream.session_id
        if inner is None or not self._routable:
            return inner  # type: ignore[no-any-return]
        return encode_handle(self._sandbox_id, inner)

    @property
    def dropped_lines(self) -> int:
        return self._stream.dropped_lines  # type: ignore[no-any-return]

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def __iter__(self) -> Iterator[AgentEvent]:
        for event in self._stream:
            yield event_with_handle(event, self._sandbox_id) if self._routable else event
        self._finish(self._stream.session_id)

    def result(self) -> AgentResult:
        result: AgentResult = self._stream.result()
        self._finish(result.session_id)
        return with_handle(result, self._sandbox_id) if self._routable else result

    def close(self) -> None:
        self._stream.close()
        self._finish(self._stream.session_id)

    def _finish(self, session_id: str | None) -> None:
        if not self._released:
            self._released = True
            self._release(session_id)


@dataclass(frozen=True)
class Lease:
    """Una invocación en curso en un VM: el miembro, la sesión que ve el host
    y la reserva de un VM dedicado hasta conocer la sesión real."""

    member: Member
    inner: str | None
    reservation: str | None = None


class AgentFleet:
    """Un agente servido por uno o varios MicroVMs (`deploy_agent()`).

    `invoke()` y `stream()` colocan cada sesión nueva en un VM con cupo (con
    `ScalingOptions.sessions="dedicated"`, uno por sesión), crean uno si hace
    falta y cabe, y esperan en una cola cliente hasta
    `ServeOptions.queue_timeout_seconds` si la flota está llena
    (`CapacityException` con `code="agent_capacity"` al agotarse). Con más de
    un VM posible (`max_vms > 1`), el `session_id` del resultado es un handle
    `rs1.<sandbox>.<sesión>`: al volver a pasarlo, la llamada va derecha a ese
    VM; si el VM ya no existe, `SandboxException` con `code="agent_vm_lost"`.
    Con `max_vms == 1` el id de sesión no cambia.

    Un id de sesión elegido por el llamante no dice en qué VM está. Con un
    VM (o `shared` y un solo proceso) no importa; con `dedicated`, sólo el
    mismo proceso lo recuerda, salvo con `session_index=DynamoDbIndex(...)`,
    que escribe una fila por VM dedicado para encontrarlo desde cualquier
    proceso. Sin índice, un id desconocido empieza un VM nuevo y el resultado
    trae el handle con el que seguir.

    Los VMs de la flota son todos los MicroVMs no terminados de la imagen del
    proyecto (un MicroVM no se puede etiquetar): una imagen, una flota. Los
    VMs por encima de `min_vms` se terminan solos tras
    `scale_in_after_seconds` sin uso (`set_timeout` de `rayd`, sin servidor
    propio).

    Coste y activación
    -------------------
    Activa: `deploy_agent()` (`rayito agent deploy`) crea `min_vms` VMs y
        `warm_vms` plazas aparcadas; `invoke()` crea más, hasta `max_vms`, si
        las sesiones llenan los que hay. Construir o reconectar la flota no
        crea nada.
    Recursos y llamadas AWS: un MicroVM por VM (`run-microvm`), el token del
        proxy (`CreateMicrovmAuthToken`, gratuito) y `list-microvms` al
        reconectar; con `warm_vms`, plazas suspendidas del pool; con
        `session_index`, una fila de DynamoDB por VM dedicado.
    Coste aproximado: el del MicroVM por segundo mientras vive (2 GB:
        ≈ $0,14 por hora en us-east-1, consultado 2026-10-06 en
        https://aws.amazon.com/lambda/pricing/), por cada VM; una plaza
        aparcada no factura cómputo pero sí almacenamiento de snapshot.
    IAM: los de `Sandbox.create()`, `Sandbox.list()` y `Sandbox.connect()`
        (`RayitoSandboxRunner`) y `secretsmanager:GetSecretValue` por
        pasarela; con `session_index`, los de `DynamoDbIndex`.
    Cómo apagarla: `stop()` termina todos los VMs; `min_vms=1, max_vms=1,
        warm_vms=0` (por defecto) es un solo VM; sin llamar a
        `deploy_agent()` nada se crea.
    Ejemplo:
        fleet = deploy_agent("./mi-agente")
        fleet.invoke("Resume el README.").text
        fleet.stop()
    """

    def __init__(
        self,
        project: AgentProject | None,
        *,
        name: str | None = None,
        access_token: str | None = None,
        scaling: ScalingOptions | None = None,
        service: ProjectService | None = None,
        directory: Any = None,
        pool_factory: Callable[..., SandboxPool] | None = None,
        pool_backend: PoolBackend | None = None,
        session_index: DynamoDbIndex | None = None,
        keep_on_failure: bool = False,
        region: str | None = None,
        session: Any = None,
        transport_factory: Any = default_transport,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if project is None and name is None:
            raise InvalidArgumentException("AgentFleet necesita project o name")
        self._project = project
        self._name = name or (project.template or project.name if project else "")
        self._token = access_token
        self._settings: FleetSettings = fleet_settings_of(project, scaling)
        self._service = service or (
            ProjectService(project, transport_factory=transport_factory) if project else None
        )
        self._directory = directory or SandboxDirectory(region=region, session=session)
        self._pool_factory = pool_factory
        self._pool_backend = pool_backend
        self._index = session_index
        self._keep_on_failure = keep_on_failure
        self._region = region
        self._session = session
        self._transport_factory = transport_factory
        self._clock = clock
        self._sleep = sleep
        self._members: list[Member] = []
        self._growing = 0
        self._sessions: dict[str, str] = {}
        self._pool: SandboxPool | None = None
        self._lock = threading.RLock()

    @classmethod
    def connect(
        cls,
        name: str,
        *,
        access_token: str | None = None,
        project: AgentProject | None = None,
        scaling: ScalingOptions | None = None,
        region: str | None = None,
        session: Any = None,
        **dependencies: Any,
    ) -> Self:
        """Reconstruye la flota de la imagen `name` en cualquier proceso: todos
        sus MicroVMs no terminados, sin reanudar ninguno (un VM se abre al
        usarlo). Sin `project` la flota enruta y consulta pero no puede crear
        VMs."""
        fleet = cls(
            project,
            name=name,
            access_token=access_token,
            scaling=scaling,
            region=region,
            session=session,
            **dependencies,
        )
        fleet.refresh_members()
        return fleet

    @property
    def name(self) -> str:
        return self._name

    @property
    def access_token(self) -> str:
        """El access token de todos los VMs de la flota: guárdalo (nunca lo
        imprimas ni lo versiones) para `AgentFleet.connect()` desde otro
        proceso. Una flota reconectada sin token (para listarla o terminarla)
        no lo tiene: `InvalidArgumentException`."""
        return self._require_token()

    def _require_token(self) -> str:
        if self._token is None:
            raise InvalidArgumentException(NO_TOKEN_MESSAGE)
        return self._token

    @property
    def scaling(self) -> ScalingOptions:
        return self._settings.scaling

    @property
    def deployments(self) -> tuple[AgentDeployment, ...]:
        """Los clientes de los VMs que este proceso ya abrió."""
        with self._lock:
            return tuple(m.deployment for m in self._members if m.deployment is not None)

    @property
    def sandbox_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(member.sandbox_id for member in self._members)

    @property
    def urls(self) -> tuple[str, ...]:
        return tuple(deployment.url for deployment in self.deployments)

    # ------------------------------------------------------------ lifecycle

    def start(self) -> Self:
        """Crea `min_vms` VMs (todo o nada: si uno falla, termina los que
        creó) y arranca el pool de plazas calientes. Lo llama
        `deploy_agent()`."""
        self._require_project()
        self._start_pool()
        missing = replacements_needed(len(self.sandbox_ids), self.scaling)
        created: list[Member] = []
        try:
            with ThreadPoolExecutor(max_workers=AGENT_FLEET_START_CONCURRENCY) as executor:
                futures = [executor.submit(self._create_member, False) for _ in range(missing)]
                errors: list[BaseException] = []
                for future in futures:
                    try:
                        created.append(future.result())
                    except Exception as exc:
                        errors.append(exc)
            if errors:
                raise errors[0]
        except BaseException:
            if not self._keep_on_failure:
                self._discard(created)
            self._close_pool(drain=True)
            raise
        with self._lock:
            self._members.extend(created)
            mark_extras(self._members, self.scaling)
        return self

    def refresh_members(self) -> None:
        """Vuelve a leer qué VMs de la imagen siguen vivos (`list-microvms`):
        añade los nuevos, olvida los que ya no están y deja fuera las plazas
        aparcadas del pool de este proceso."""
        parked = self._parked_ids()
        listing = sorted(
            (item for item in self._directory.list_members(self._name)),
            key=lambda item: item.started_at,
        )
        live = [item for item in listing if item.sandbox_id not in parked]
        with self._lock:
            known = {member.sandbox_id: member for member in self._members}
            self._members = [known.get(item.sandbox_id) or Member(item.sandbox_id) for item in live]
            mark_extras(self._members, self.scaling)

    def reconcile(self) -> None:
        """Deja la flota dentro de `ScalingOptions`: olvida los VMs perdidos,
        crea los que falten hasta `min_vms` y termina los que sobren por
        encima de `max_vms` (sólo los libres)."""
        self.refresh_members()
        with self._lock:
            lost = [member for member in self._members if is_lost(member)]
        for member in lost:
            self._replace(member)
        self._trim()
        self._fill_to_minimum()
        self._resize_pool()

    def scale(
        self,
        *,
        min_vms: int | None = None,
        max_vms: int | None = None,
        warm_vms: int | None = None,
    ) -> None:
        """Cambia los límites de la flota (se validan como `ScalingOptions`) y
        la reconcilia: crea hasta `min_vms`, termina los sobrantes y ajusta
        las plazas calientes."""
        current = self.scaling
        self._settings = FleetSettings(
            scaling=ScalingOptions(
                min_vms=current.min_vms if min_vms is None else min_vms,
                max_vms=current.max_vms if max_vms is None else max_vms,
                warm_vms=current.warm_vms if warm_vms is None else warm_vms,
                sessions=current.sessions,
                scale_in_after_seconds=current.scale_in_after_seconds,
            ),
            queue_timeout_seconds=self._settings.queue_timeout_seconds,
            max_concurrency=self._settings.max_concurrency,
        )
        self.reconcile()

    def detach(self) -> None:
        """Para el relleno de plazas calientes de este proceso y deja los VMs
        vivos: un proceso de un solo uso (`rayito agent deploy`) termina así
        sin tirar lo que creó. Las plazas aparcadas requieren un backend de
        pool persistente (`pool_backend`)."""
        self._close_pool(drain=self._pool_backend is None)

    def stop(self) -> None:
        """Termina todos los VMs de la flota y las plazas del pool. Sigue
        aunque uno falle y, al final, lanza el primer error."""
        errors: list[Exception] = []
        self._close_pool(drain=True, errors=errors)
        with self._lock:
            members, self._members = list(self._members), []
        for member in members:
            try:
                self._terminate(member)
            except Exception as exc:
                logger.warning("no se pudo terminar un VM de la flota", exc_info=True)
                errors.append(exc)
        if errors:
            raise errors[0]

    def status(self) -> FleetStatus:
        """Estado de la flota: pregunta a los VMs que este proceso ya abrió
        (`GET /ping`) y deja como `unknown` los demás."""
        with self._lock:
            members = list(self._members)
        statuses = []
        for member in members:
            if member.deployment is not None:
                self._probe(member, force=True)
            statuses.append(
                FleetMemberStatus(member.sandbox_id, member.health, member.extra, member.inflight)
            )
        warm_ready = self._pool.stats().ready if self._pool is not None else 0
        scaling = self.scaling
        return FleetStatus(
            name=self._name,
            members=tuple(statuses),
            min_vms=scaling.min_vms,
            max_vms=scaling.max_vms,
            warm_vms=scaling.warm_vms,
            warm_ready=warm_ready,
        )

    def wait_warm(self, timeout: float, poll: float) -> bool:
        """Espera a que las plazas calientes queden aparcadas; `False` si se
        agota `timeout` (la flota funciona igual: las plazas son una
        optimización de latencia)."""
        if self._pool is None:
            return True
        deadline = self._clock() + timeout
        while self._pool.stats().ready < self.scaling.warm_vms:
            if self._clock() >= deadline:
                return False
            self._sleep(poll)
        return True

    # --------------------------------------------------------- invocations

    def invoke(
        self,
        prompt: str,
        session_id: str | None = None,
        model: str | None = None,
        reasoning: bool = False,
    ) -> AgentResult:
        """Una invocación sin streaming: el `AgentResult`, con el id de sesión
        enrutable si la flota puede tener varios VMs."""
        lease, result = self._execute(
            session_id,
            lambda deployment, inner: deployment.invoke(prompt, inner, model, reasoning),
        )
        self._release(lease, result.session_id)
        return with_handle(result, lease.member.sandbox_id) if self._routable else result

    def stream(
        self,
        prompt: str,
        session_id: str | None = None,
        model: str | None = None,
        reasoning: bool = False,
    ) -> AgentFleetStream:
        """Como `invoke()`, con los eventos a medida que llegan. El VM queda
        ocupado hasta cerrar el stream."""
        lease, stream = self._execute(
            session_id,
            lambda deployment, inner: deployment.stream(prompt, inner, model, reasoning),
        )
        return AgentFleetStream(
            stream,
            sandbox_id=lease.member.sandbox_id,
            routable=self._routable,
            release=lambda inner_session: self._release(lease, inner_session),
        )

    # -------------------------------------------------------------- internals

    @property
    def _routable(self) -> bool:
        return self.scaling.routes_by_handle

    def _require_project(self) -> AgentProject:
        if self._project is None:
            raise InvalidArgumentException(NO_PROJECT_MESSAGE)
        return self._project

    def _execute(
        self, session_id: str | None, operation: Callable[[AgentDeployment, str | None], Any]
    ) -> tuple[Lease, Any]:
        """Coloca la sesión, ejecuta `operation` en su VM y devuelve el
        arrendamiento (con la invocación en curso anotada) y el resultado. Un
        429 del host marca el VM ocupado y reintenta (en otro VM si la sesión
        es nueva) hasta el plazo de la cola; un VM que ya no existe es
        `agent_vm_lost`."""
        if session_id is not None:
            validate_caller_session_id(session_id)
        deadline = self._clock() + self._settings.queue_timeout_seconds
        routed = route_of(session_id) is not None
        while True:
            lease = self._lease(session_id, deadline)
            try:
                return lease, operation(self._open_or_connect(lease.member), lease.inner)
            except AgentException as exc:
                self._unlease(lease)
                if exc.reason != "busy":
                    raise
                self._wait_for_capacity(lease.member, deadline, routed=routed)
            except SandboxNotFoundException:
                self._unlease(lease)
                self._replace(lease.member)
                raise SandboxException(vm_lost_message(), code=AGENT_VM_LOST_CODE) from None
            except SandboxException:
                self._unlease(lease)
                self._note_transport_failure(lease.member)
                raise
            except BaseException:
                self._unlease(lease)
                raise

    def _wait_for_capacity(self, member: Member, deadline: float, *, routed: bool) -> None:
        """Tras un 429: el VM cuenta como ocupado un intento; vencido el
        plazo, `CapacityException`. Una sesión nueva vuelve a la cola, que
        espera si no hay otro VM; una con handle sólo puede ir a su VM, así
        que espera aquí."""
        with self._lock:
            record_busy(member, self._clock())
        if self._clock() >= deadline:
            raise CapacityException(
                capacity_message(self.scaling), code=AGENT_CAPACITY_CODE
            ) from None
        if routed:
            self._sleep(AGENT_FLEET_BUSY_RETRY_SECONDS)

    def _lease(self, session_id: str | None, deadline: float) -> Lease:
        """El VM de la llamada con una invocación en curso ya anotada: el del
        handle, o el que elige la cola para una sesión nueva."""
        route = route_of(session_id)
        if route is not None:
            return Lease(self._route_member(route.sandbox_id), route.session_id)
        member, reservation = self._place(session_id, deadline)
        return Lease(member, session_id, reservation)

    def _route_member(self, sandbox_id: str) -> Member:
        member = self._find_locked(sandbox_id)
        if member is None:
            self.refresh_members()
            member = self._find_locked(sandbox_id)
        if member is None:
            raise SandboxException(vm_lost_message(), code=AGENT_VM_LOST_CODE)
        with self._lock:
            member.inflight += 1
        return member

    def _find_locked(self, sandbox_id: str) -> Member | None:
        with self._lock:
            return next((m for m in self._members if m.sandbox_id == sandbox_id), None)

    def _place(self, session_id: str | None, deadline: float) -> tuple[Member, str | None]:
        """La cola cliente: el VM de una sesión conocida, el primer VM libre,
        uno nuevo si cabe, o espera hasta el plazo de la cola y entonces
        `CapacityException`."""
        settings = self._settings
        while True:
            known = self._known_session_member(session_id)
            if known is not None:
                return known, None
            with self._lock:
                plan = plan_placement(
                    self._members,
                    self._growing,
                    settings.scaling,
                    settings.max_concurrency,
                    self._clock(),
                )
                reservation = self._reserve(plan.member) if plan.kind == "use" else None
                if plan.kind == "grow":
                    self._growing += 1
            if plan.kind == "use" and plan.member is not None:
                if self._probe(plan.member):
                    self._remember(session_id, plan.member)
                    return plan.member, reservation
                self._unreserve(plan.member, reservation)
                continue
            if plan.kind == "grow":
                return self._grow_for(session_id)
            if self._clock() >= deadline:
                raise CapacityException(
                    capacity_message(settings.scaling), code=AGENT_CAPACITY_CODE
                )
            self._sleep(AGENT_FLEET_BUSY_RETRY_SECONDS)

    def _reserve(self, member: Member | None) -> str | None:
        if member is None:
            return None
        member.inflight += 1
        if self.scaling.sessions != SESSIONS_DEDICATED:
            return None
        reservation = new_reservation()
        member.bound.add(reservation)
        return reservation

    def _unreserve(self, member: Member, reservation: str | None) -> None:
        with self._lock:
            member.inflight -= 1
            if reservation is not None:
                member.bound.discard(reservation)

    def _remember(self, session_id: str | None, member: Member) -> None:
        if session_id is not None and self.scaling.sessions == SESSIONS_DEDICATED:
            with self._lock:
                self._sessions[session_id] = member.sandbox_id

    def _known_session_member(self, session_id: str | None) -> Member | None:
        """El VM dedicado de una sesión que el llamante eligió: el que este
        proceso recuerda o, con `session_index`, el que el índice encuentra."""
        if session_id is None or self.scaling.sessions != SESSIONS_DEDICATED:
            return None
        with self._lock:
            sandbox_id = self._sessions.get(session_id)
        if sandbox_id is None and self._index is not None:
            sandbox_id = self._directory.find_session(
                self._name, session_label(session_id), self._index
            )
        if sandbox_id is None:
            return None
        member = self._route_member(sandbox_id)
        self._remember(session_id, member)
        return member

    def _grow_for(self, session_id: str | None) -> tuple[Member, str | None]:
        """Un VM nuevo para la sesión: ocupado desde el primer instante (una
        reserva si la flota es dedicada) para que ninguna otra se cuele."""
        dedicated = self.scaling.sessions == SESSIONS_DEDICATED
        try:
            member = self._create_member(True, session_id=session_id if dedicated else None)
        finally:
            with self._lock:
                self._growing -= 1
        reservation = new_reservation() if dedicated else None
        with self._lock:
            member.inflight += 1
            if reservation is not None:
                member.bound.add(reservation)
            self._members.append(member)
            mark_extras(self._members, self.scaling)
        self._remember(session_id, member)
        return member, reservation

    def _create_member(self, extra: bool, *, session_id: str | None = None) -> Member:
        """Un VM nuevo con servicio: si crece la flota (`extra`) y la sesión no
        necesita etiqueta propia, de una plaza caliente; si no, con
        `create()` (los VMs base no gastan las plazas)."""
        project = self._require_project()
        service = self._service
        assert service is not None
        label = (
            session_label(session_id)
            if session_id is not None and self._index is not None
            else None
        )
        if extra and self._pool is not None and label is None:
            sandbox = self._pool.take(gateways=dict(project.gateways))
            try:
                deployment = service.serve(sandbox)
            except BaseException:
                self._kill_quietly(sandbox)
                raise
        else:
            deployment = service.deploy(
                region=self._region,
                session=self._session,
                access_token=self._require_token(),
                keep_on_failure=self._keep_on_failure,
                metadata=None if label is None else {AGENT_FLEET_SESSION_LABEL: label},
                index=None if label is None else self._index,
            )
        member = Member(deployment.sandbox_id, deployment=deployment, extra=extra)
        self._limit_lifetime(member)
        return member

    def _index_label(self, session_id: str | None) -> str | None:
        """La etiqueta de sesión de un VM dedicado, sólo con índice."""
        if session_id is None or self._index is None:
            return None
        return session_label(session_id)

    @staticmethod
    def _kill_quietly(sandbox: Any) -> None:
        try:
            sandbox.kill()
        except Exception:
            logger.warning("no se pudo terminar el sandbox de la flota", exc_info=True)

    def _limit_lifetime(self, member: Member) -> None:
        """A un VM por encima de `min_vms` se le acota la vida: `rayd` lo
        termina solo tras `scale_in_after_seconds`, y cada uso la renueva."""
        if not member.extra or member.deployment is None:
            return
        try:
            member.deployment.set_timeout(self.scaling.scale_in_after_seconds)
        except SandboxException:
            logger.warning("no se pudo acotar la vida de un VM sobrante", exc_info=True)

    def _open_or_connect(self, member: Member) -> AgentDeployment:
        """El cliente del VM: el que ya hay o, la primera vez, conecta con el
        token de la flota y, si el VM no tiene servicio (una plaza aparcada),
        lo arranca."""
        with self._lock:
            deployment = member.deployment
        if deployment is not None:
            return deployment  # type: ignore[no-any-return]
        sandbox = self._directory.connect(member.sandbox_id, self._require_token())
        deployment = self._serving(sandbox)
        with self._lock:
            member.deployment = deployment
        self._limit_lifetime(member)
        return deployment

    def _serving(self, sandbox: Any) -> AgentDeployment:
        if sandbox.files.exists(AGENT_SERVICE_READY_MARKER_PATH):
            return self._deployment_of(sandbox)
        service = self._service
        if service is None:
            raise InvalidArgumentException(NO_PROJECT_MESSAGE)
        return service.serve(sandbox)

    def _deployment_of(self, sandbox: Any) -> AgentDeployment:
        labels = parse_labels(sandbox.metadata)
        timeout = request_timeout(self._project) if self._project else None
        options = {} if timeout is None else {"request_timeout": timeout}
        return AgentDeployment(
            sandbox,
            port=labels.port,
            project=labels.project,
            raw=labels.raw,
            transport_factory=self._transport_factory,
            **options,
        )

    def _probe(self, member: Member, *, force: bool = False) -> bool:
        """Si el VM puede atender una sesión: lo abre si hace falta y, si su
        último ping caducó, le pregunta por `GET /ping`. Un VM perdido se
        sustituye."""
        try:
            deployment = self._open_or_connect(member)
        except SandboxNotFoundException:
            self._drop(member)
            return False
        now = self._clock()
        if not force and ping_is_fresh(member, now):
            return member.health != "unavailable"
        status: DeploymentStatus = deployment.status()
        with self._lock:
            record_ping(member, status.state, self._clock())
        if is_lost(member):
            self._replace(member)
            return False
        return status.state == "healthy"

    def _note_transport_failure(self, member: Member) -> None:
        """Un fallo de transporte no basta para dar un VM por perdido: se
        pregunta a `/ping` y sólo tras `AGENT_FLEET_UNHEALTHY_PINGS` fallos
        seguidos se sustituye (la sesión de ese VM se pierde)."""
        deployment = member.deployment
        if deployment is None:
            return
        status = deployment.status()
        with self._lock:
            record_ping(member, status.state, self._clock())
        if is_lost(member):
            self._replace(member)

    def _release(self, lease: Lease, inner_session: str | None) -> None:
        """Suelta la invocación en curso: la reserva de un VM dedicado pasa a
        ser la sesión real y a un VM sobrante se le renueva la vida."""
        self._unlease(lease, keep=inner_session)
        if inner_session is not None:
            self._limit_lifetime(lease.member)

    def _unlease(self, lease: Lease, *, keep: str | None = None) -> None:
        with self._lock:
            member = lease.member
            member.inflight = max(0, member.inflight - 1)
            if lease.reservation is not None:
                member.bound.discard(lease.reservation)
                if keep is not None:
                    member.bound.add(keep)

    def _drop(self, member: Member) -> None:
        with self._lock:
            if member in self._members:
                self._members.remove(member)
            mark_extras(self._members, self.scaling)

    def _replace(self, member: Member) -> None:
        """Termina un VM perdido, lo saca de la flota y, si baja de `min_vms`,
        crea otro."""
        self._drop(member)
        try:
            self._terminate(member)
        except Exception:
            logger.warning("no se pudo terminar un VM perdido", exc_info=True)
        self._fill_to_minimum()

    def _fill_to_minimum(self) -> None:
        if self._project is None:
            return
        for _ in range(replacements_needed(len(self.sandbox_ids), self.scaling)):
            member = self._create_member(False)
            with self._lock:
                self._members.append(member)
                mark_extras(self._members, self.scaling)

    def _trim(self) -> None:
        with self._lock:
            excess = [
                member
                for member in surplus(self._members, self.scaling)
                if member.inflight == 0 and not member.bound
            ]
        for member in excess:
            self._drop(member)
            try:
                self._terminate(member)
            except Exception:
                logger.warning("no se pudo terminar un VM sobrante", exc_info=True)

    def _terminate(self, member: Member) -> None:
        """Termina el VM sin abrirlo: abrir uno suspendido lo reanudaría sólo
        para matarlo."""
        if member.deployment is not None:
            member.deployment.stop()
        else:
            self._directory.kill(member.sandbox_id)

    def _discard(self, members: Sequence[Member]) -> None:
        for member in members:
            try:
                self._terminate(member)
            except Exception:
                logger.warning("no se pudo terminar un VM de un arranque fallido", exc_info=True)

    # -------------------------------------------------------------- warm pool

    def _pool_metadata(self) -> Mapping[str, str]:
        project = self._require_project()
        return labels_for(project.name, project.serve.port, raw=False)

    def _start_pool(self) -> None:
        scaling = self.scaling
        if scaling.warm_vms == 0 or self._pool is not None:
            return
        project = self._require_project()
        config = pool_config_for(
            project,
            scaling,
            self._require_token(),
            self._pool_metadata(),
            template_version=getattr(self._service, "image_version", None),
        )
        factory = self._pool_factory or _default_pool
        self._pool = factory(
            config,
            backend=self._pool_backend,
            region=self._region,
            session=self._session,
        ).start()

    def _resize_pool(self) -> None:
        pool = self._pool
        warm = self.scaling.warm_vms
        if pool is not None and pool.config.size == warm:
            return
        self._close_pool(drain=True)
        self._start_pool()

    def _close_pool(self, *, drain: bool, errors: list[Exception] | None = None) -> None:
        pool, self._pool = self._pool, None
        if pool is None:
            return
        try:
            pool.close(drain=drain)
        except Exception as exc:
            logger.warning("no se pudo cerrar el pool de plazas calientes", exc_info=True)
            if errors is not None:
                errors.append(exc)

    def _parked_ids(self) -> set[str]:
        if self._pool is None:
            return set()
        return {slot.sandbox_id for slot in self._pool.stats().slots}


def _default_pool(config: Any, **options: Any) -> SandboxPool:
    from rayito.sandbox_sync.pool import SandboxPool

    return SandboxPool(config, **options)


__all__ = ["AgentFleet", "AgentFleetStream", "SandboxDirectory"]
