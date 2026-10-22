"""`AsyncAgentFleet`: el espejo asíncrono de `AgentFleet` (ADR-029, D5), sobre
`AsyncAgentDeployment`, `AsyncProjectService` y `AsyncSandboxPool`. El reparto,
el id de sesión enrutable y la salud son los mismos de `_fleet_base.py`.

En un único bucle de eventos no hace falta un lock: ninguna sección que toca
el estado de la flota espera (`await`) a medias.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from types import TracebackType
from typing import TYPE_CHECKING, Any, Self

from rayito._agent import _fleet_recipe_aws as recipe_aws
from rayito._agent._deployment_async import AsyncAgentDeployment, default_async_transport
from rayito._agent._deployment_base import labels_for, parse_labels
from rayito._agent._events import AgentEvent, AgentResult
from rayito._agent._fleet import (
    NO_PROJECT_MESSAGE,
    NO_TOKEN_MESSAGE,
    PARKED_SLOT_MESSAGE,
    FleetKeyRenewal,
    Lease,
)
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
    ParkedSlotException,
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
from rayito._agent._fleet_recipe import FleetRecipe, recipe_of
from rayito._agent._fleet_recipe_aws import (
    RECIPE_SAVED,
    FleetRecipeStore,
    forget_recipe,
    load_recipe,
    store_recipe,
)
from rayito._agent._gateway_keys import GatewayKeyRenewer
from rayito._agent._project_base import request_timeout
from rayito._agent._project_service_async import AsyncProjectService
from rayito._agent._scaling_domain import (
    SESSIONS_DEDICATED,
    ScalingOptions,
    encode_handle,
    replacements_needed,
    validate_caller_session_id,
)
from rayito._agent._stream_base import require_prompt_or_input
from rayito._limits import AGENT_SERVICE_READY_MARKER_PATH
from rayito._sandbox_base import DEFAULT_REQUEST_TIMEOUT_SECONDS
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
    from rayito.sandbox_async.pool import AsyncSandboxPool

logger = logging.getLogger("rayito.agent")


class AsyncSandboxDirectory:
    """El adaptador de producción de la flota asíncrona: lista los VMs de la
    imagen (`list-microvms` con el filtro de imagen) sin contar los que no son
    de un agente (`select_members`: los `RUNNING` se sondean con un `Health`,
    sin token ni despertar a nadie), conecta uno con el token de la flota y
    busca por sesión en el índice opcional."""

    def __init__(
        self, *, region: str | None = None, session: Any = None, sandbox_class: Any = None
    ) -> None:
        self._region = region
        self._session = session
        self._sandbox_class = sandbox_class

    async def list_members(self, template: str) -> list[MemberListing]:
        sandbox_class = self._sandbox()
        listed = [
            MemberListing(item.sandbox_id, item.state, item.started_at)
            for item in await sandbox_class.list(
                template=template, states=AGENT_FLEET_LIVE_STATES, **self._options()
            )
        ]
        probed = await sandbox_class.list(
            template=template, states=(AGENT_FLEET_PROBE_STATE,), metadata={}, **self._options()
        )
        return select_members(listed, owned_ids(probed))

    async def connect(self, sandbox_id: str, access_token: str) -> Any:
        return await self._sandbox().connect(
            sandbox_id, access_token=access_token, **self._options()
        )

    async def kill(self, sandbox_id: str) -> bool:
        return bool(await self._sandbox().kill(sandbox_id, **self._options()))

    async def find_session(self, template: str, label: str, index: DynamoDbIndex) -> str | None:
        items = await self._sandbox().list(
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
        from rayito.sandbox_async import AsyncSandbox

        return AsyncSandbox


class AsyncAgentFleetStream:
    """El stream de una invocación de la flota asíncrona: los eventos del VM
    con el id de sesión enrutable, y el VM liberado al cerrarlo."""

    def __init__(
        self,
        stream: Any,
        *,
        sandbox_id: str,
        routable: bool,
        release: Callable[[str | None], Awaitable[None]],
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

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()

    def __aiter__(self) -> AsyncIterator[AgentEvent]:
        return self._events()

    async def _events(self) -> AsyncIterator[AgentEvent]:
        async for event in self._stream:
            yield event_with_handle(event, self._sandbox_id) if self._routable else event
        await self._finish(self._stream.session_id)

    async def result(self) -> AgentResult:
        result: AgentResult = await self._stream.result()
        await self._finish(result.session_id)
        return with_handle(result, self._sandbox_id) if self._routable else result

    async def close(self) -> None:
        await self._stream.close()
        await self._finish(self._stream.session_id)

    async def _finish(self, session_id: str | None) -> None:
        if not self._released:
            self._released = True
            await self._release(session_id)


class AsyncAgentFleet:
    """El espejo asíncrono de `AgentFleet`: las mismas operaciones como
    corrutinas (`start`, `invoke`, `status`, `scale`, `reconcile`, `stop`,
    `connect`) y `stream()` como contexto asíncrono. Sesiones, id de sesión
    enrutable (`rs1.<sandbox>.<sesión>`), cola cliente, sesiones dedicadas e
    índice de sesión opcional son los de `AgentFleet`.

    Coste y activación
    -------------------
    Activa: `await deploy_agent_async()` crea `min_vms` VMs y `warm_vms`
        plazas aparcadas; `invoke()` crea más, hasta `max_vms`, si las
        sesiones llenan los que hay. Construir o reconectar la flota no crea
        nada.
    Recursos y llamadas AWS: un MicroVM por VM (`run-microvm`), el token del
        proxy (`CreateMicrovmAuthToken`, gratuito) y `list-microvms` al
        reconectar; con `warm_vms`, plazas suspendidas del pool; con
        `session_index`, una fila de DynamoDB por VM dedicado.
    Coste aproximado: el del MicroVM por segundo mientras vive (2 GB:
        ≈ $0,14 por hora en us-east-1, consultado 2026-10-06 en
        https://aws.amazon.com/lambda/pricing/), por cada VM; una plaza
        aparcada no factura cómputo pero sí almacenamiento de snapshot.
    IAM: los de `AsyncSandbox.create()`, `list()` y `connect()`
        (`RayitoSandboxRunner`) y `secretsmanager:GetSecretValue` por
        pasarela; con `session_index`, los de `DynamoDbIndex`.
    Cómo apagarla: `await stop()` termina todos los VMs; `min_vms=1,
        max_vms=1, warm_vms=0` (por defecto) es un solo VM; sin llamar a
        `deploy_agent_async()` nada se crea.
    Ejemplo:
        fleet = await deploy_agent_async("./mi-agente")
        (await fleet.invoke("Resume el README.")).text
        await fleet.stop()
    """

    def __init__(
        self,
        project: AgentProject | None,
        *,
        name: str | None = None,
        access_token: str | None = None,
        scaling: ScalingOptions | None = None,
        service: AsyncProjectService | None = None,
        directory: Any = None,
        pool_factory: Callable[..., AsyncSandboxPool] | None = None,
        pool_backend: PoolBackend | None = None,
        session_index: DynamoDbIndex | None = None,
        keep_on_failure: bool = False,
        region: str | None = None,
        session: Any = None,
        transport_factory: Any = default_async_transport,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        renew_gateway_keys: bool = False,
        recipe: FleetRecipe | None = None,
        recipe_store: FleetRecipeStore | None = None,
        key_renewer: GatewayKeyRenewer | None = None,
    ) -> None:
        if project is None and name is None:
            raise InvalidArgumentException("AsyncAgentFleet necesita project o name")
        self._project = project
        self._name = name or (project.template or project.name if project else "")
        self._token = access_token
        self._settings: FleetSettings = fleet_settings_of(
            project, scaling, recipe.scaling if recipe else None
        )
        self._service = service or (
            AsyncProjectService(
                project,
                transport_factory=transport_factory,
                image_version=recipe.image_version if recipe else None,
            )
            if project
            else None
        )
        self._key_renewal = FleetKeyRenewal(
            project,
            enabled=renew_gateway_keys,
            key_renewer=key_renewer,
            region=region,
            session=session,
        )
        self._recipe_store = recipe_store
        self._recipe_known = recipe is not None
        self._directory = directory or AsyncSandboxDirectory(region=region, session=session)
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
        self._pool: AsyncSandboxPool | None = None

    @classmethod
    async def connect(
        cls,
        name: str,
        *,
        access_token: str | None = None,
        project: AgentProject | None = None,
        scaling: ScalingOptions | None = None,
        region: str | None = None,
        session: Any = None,
        use_recipe: bool = True,
        renew_gateway_keys: bool | None = None,
        **dependencies: Any,
    ) -> Self:
        """Como `AgentFleet.connect()`: sin `project` y con `use_recipe` lee
        la receta de la flota (un `GetParameter`, en un hilo) y con ella crea
        VMs iguales a los del despliegue; `scaling=` y `renew_gateway_keys=`
        explícitos ganan a la receta."""
        recipe: FleetRecipe | None = None
        if project is None and use_recipe:
            store = dependencies.pop("recipe_store", None) or recipe_aws.default_recipe_store(
                region, session
            )
            loaded = await asyncio.to_thread(load_recipe, store, name)
            if loaded is not None:
                recipe, project = loaded
            dependencies["recipe_store"] = store
        renew = recipe.renew_gateway_keys if recipe is not None else False
        fleet = cls(
            project,
            name=name,
            access_token=access_token,
            scaling=scaling,
            region=region,
            session=session,
            renew_gateway_keys=renew if renew_gateway_keys is None else renew_gateway_keys,
            recipe=recipe,
            **dependencies,
        )
        fleet._key_renewal.inherited = renew_gateway_keys is None
        await fleet.refresh_members()
        return fleet

    @property
    def name(self) -> str:
        return self._name

    @property
    def access_token(self) -> str:
        """El access token de todos los VMs de la flota: guárdalo (nunca lo
        imprimas ni lo versiones) para `AsyncAgentFleet.connect()` desde otro
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
    def deployments(self) -> tuple[AsyncAgentDeployment, ...]:
        """Los clientes de los VMs que este proceso ya abrió."""
        return tuple(m.deployment for m in self._members if m.deployment is not None)

    @property
    def sandbox_ids(self) -> tuple[str, ...]:
        return tuple(member.sandbox_id for member in self._members)

    @property
    def urls(self) -> tuple[str, ...]:
        return tuple(deployment.url for deployment in self.deployments)

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> Self:
        """Crea `min_vms` VMs (todo o nada: si uno falla, termina los que
        creó) y arranca el pool de plazas calientes. Lo llama
        `deploy_agent_async()`."""
        self._require_project()
        await self._start_pool()
        missing = replacements_needed(len(self._members), self.scaling)
        gate = asyncio.Semaphore(AGENT_FLEET_START_CONCURRENCY)

        async def create() -> Member:
            async with gate:
                return await self._create_member(False)

        outcomes = await asyncio.gather(*(create() for _ in range(missing)), return_exceptions=True)
        created = [outcome for outcome in outcomes if isinstance(outcome, Member)]
        errors = [outcome for outcome in outcomes if isinstance(outcome, BaseException)]
        if errors:
            if not self._keep_on_failure:
                await self._discard(created)
            await self._close_pool(drain=True)
            raise errors[0]
        self._members.extend(created)
        mark_extras(self._members, self.scaling)
        return self

    async def refresh_members(self) -> None:
        """Vuelve a leer qué VMs de la imagen siguen vivos (`list-microvms`):
        añade los nuevos, olvida los que ya no están y deja fuera las plazas
        aparcadas del pool de este proceso."""
        parked = self._parked_ids()
        listing = sorted(
            await self._directory.list_members(self._name), key=lambda item: item.started_at
        )
        live = [item for item in listing if item.sandbox_id not in parked]
        known = {member.sandbox_id: member for member in self._members}
        self._members = [known.get(item.sandbox_id) or Member(item.sandbox_id) for item in live]
        mark_extras(self._members, self.scaling)

    async def reconcile(self) -> None:
        """Deja la flota dentro de `ScalingOptions`: olvida los VMs perdidos,
        crea los que falten hasta `min_vms` y termina los que sobren por
        encima de `max_vms` (sólo los libres)."""
        await self.refresh_members()
        for member in [member for member in self._members if is_lost(member)]:
            await self._replace(member)
        await self._trim()
        await self._fill_to_minimum()
        await self._resize_pool()

    async def scale(
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
            scaling_source="explicit",
        )
        await self.reconcile()

    async def detach(self) -> None:
        """Para el relleno de plazas calientes de este proceso y deja los VMs
        vivos: un proceso de un solo uso (`rayito agent deploy`) termina así
        sin tirar lo que creó. Las plazas aparcadas requieren un backend de
        pool persistente (`pool_backend`)."""
        await self._close_pool(drain=self._pool_backend is None)

    async def stop(self, *, keep_recipe: bool = False) -> None:
        """Termina todos los VMs de la flota y las plazas del pool y, si este
        proceso guardó o leyó su receta, la borra (`keep_recipe=True` la
        conserva). Sigue aunque uno falle y, al final, lanza el primer
        error."""
        errors: list[Exception] = []
        await self._close_pool(drain=True, errors=errors)
        members, self._members = list(self._members), []
        for member in members:
            try:
                await self._terminate(member)
            except Exception as exc:
                logger.warning("no se pudo terminar un VM de la flota", exc_info=True)
                errors.append(exc)
        if self._recipe_known and not keep_recipe:
            try:
                await asyncio.to_thread(forget_recipe, self._recipes(), self._name)
                self._recipe_known = False
            except SandboxException as exc:
                errors.append(exc)
        if errors:
            raise errors[0]

    async def status(self) -> FleetStatus:
        """Estado de la flota: pregunta a los VMs que este proceso ya abrió
        (`GET /ping`) y deja como `unknown` los demás."""
        statuses = []
        for member in list(self._members):
            if member.deployment is not None:
                await self._probe(member, force=True)
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
            scaling_source=self._settings.scaling_source,
        )

    async def wait_warm(self, timeout: float, poll: float) -> bool:
        """Espera a que las plazas calientes queden aparcadas; `False` si se
        agota `timeout` (la flota funciona igual: las plazas son una
        optimización de latencia)."""
        if self._pool is None:
            return True
        deadline = self._clock() + timeout
        while self._pool.stats().ready < self.scaling.warm_vms:
            if self._clock() >= deadline:
                return False
            await self._sleep(poll)
        return True

    # --------------------------------------------------------- invocations

    async def invoke(
        self,
        prompt: str | None = None,
        session_id: str | None = None,
        model: str | None = None,
        reasoning: bool = False,
        *,
        input: Mapping[str, Any] | None = None,
    ) -> AgentResult:
        """Una invocación sin streaming: el `AgentResult`, con el id de sesión
        enrutable si la flota puede tener varios VMs. Con `input` en lugar de
        `prompt`, `result.output` trae la salida del grafo; se comprueba antes
        de reservar un VM."""
        require_prompt_or_input(prompt, input)

        async def operation(deployment: AsyncAgentDeployment, inner: str | None) -> AgentResult:
            return await deployment.invoke(prompt, inner, model, reasoning, input=input)

        lease, result = await self._execute(session_id, operation)
        await self._release(lease, result.session_id)
        return with_handle(result, lease.member.sandbox_id) if self._routable else result

    async def stream(
        self,
        prompt: str | None = None,
        session_id: str | None = None,
        model: str | None = None,
        reasoning: bool = False,
        *,
        input: Mapping[str, Any] | None = None,
    ) -> AsyncAgentFleetStream:
        """Como `invoke()`, con los eventos a medida que llegan. La petición se
        hace aquí (un 429 se reintenta) y el VM queda ocupado hasta cerrar el
        stream."""
        require_prompt_or_input(prompt, input)

        async def operation(deployment: AsyncAgentDeployment, inner: str | None) -> Any:
            stream = deployment.stream(prompt, inner, model, reasoning, input=input)
            await stream.__aenter__()
            return stream

        lease, stream = await self._execute(session_id, operation)

        async def release(inner_session: str | None) -> None:
            await self._release(lease, inner_session)

        return AsyncAgentFleetStream(
            stream,
            sandbox_id=lease.member.sandbox_id,
            routable=self._routable,
            release=release,
        )

    # -------------------------------------------------------------- internals

    @property
    def _routable(self) -> bool:
        return self.scaling.routes_by_handle

    def _require_project(self) -> AgentProject:
        if self._project is None:
            raise InvalidArgumentException(NO_PROJECT_MESSAGE)
        return self._project

    async def _execute(
        self,
        session_id: str | None,
        operation: Callable[[AsyncAgentDeployment, str | None], Awaitable[Any]],
    ) -> tuple[Lease, Any]:
        """Coloca la sesión, ejecuta `operation` en su VM y devuelve el
        arrendamiento (con la invocación en curso anotada) y el resultado. Un
        429 del host marca el VM ocupado y reintenta (en otro VM si la sesión
        es nueva) hasta el plazo de la cola; un VM que ya no existe es
        `agent_vm_lost`."""
        deadline = self._clock() + self._settings.queue_timeout_seconds
        if session_id is not None:
            validate_caller_session_id(session_id)
        routed = route_of(session_id) is not None
        while True:
            lease = await self._lease(session_id, deadline)
            try:
                deployment = await self._open_or_connect(lease.member)
                return lease, await operation(deployment, lease.inner)
            except AgentException as exc:
                self._unlease(lease)
                if exc.reason != "busy":
                    raise
                await self._wait_for_capacity(lease.member, deadline, routed=routed)
            except SandboxNotFoundException:
                self._unlease(lease)
                await self._replace(lease.member)
                raise SandboxException(vm_lost_message(), code=AGENT_VM_LOST_CODE) from None
            except SandboxException:
                self._unlease(lease)
                await self._note_transport_failure(lease.member)
                raise
            except BaseException:
                self._unlease(lease)
                raise

    async def _wait_for_capacity(self, member: Member, deadline: float, *, routed: bool) -> None:
        """Tras un 429: el VM cuenta como ocupado un intento; vencido el
        plazo, `CapacityException`. Una sesión nueva vuelve a la cola, que
        espera si no hay otro VM; una con handle sólo puede ir a su VM, así
        que espera aquí."""
        record_busy(member, self._clock())
        if self._clock() >= deadline:
            raise CapacityException(
                capacity_message(self.scaling), code=AGENT_CAPACITY_CODE
            ) from None
        if routed:
            await self._sleep(AGENT_FLEET_BUSY_RETRY_SECONDS)

    async def _lease(self, session_id: str | None, deadline: float) -> Lease:
        """El VM de la llamada con una invocación en curso ya anotada: el del
        handle, o el que elige la cola para una sesión nueva."""
        route = route_of(session_id)
        if route is not None:
            return Lease(await self._route_member(route.sandbox_id), route.session_id)
        member, reservation = await self._place(session_id, deadline)
        return Lease(member, session_id, reservation)

    async def _route_member(self, sandbox_id: str) -> Member:
        member = self._find(sandbox_id)
        if member is None:
            await self.refresh_members()
            member = self._find(sandbox_id)
        if member is None:
            raise SandboxException(vm_lost_message(), code=AGENT_VM_LOST_CODE)
        member.inflight += 1
        return member

    def _find(self, sandbox_id: str) -> Member | None:
        return next((m for m in self._members if m.sandbox_id == sandbox_id), None)

    async def _place(self, session_id: str | None, deadline: float) -> tuple[Member, str | None]:
        """La cola cliente: el VM de una sesión conocida, el primer VM libre,
        uno nuevo si cabe, o espera hasta el plazo de la cola y entonces
        `CapacityException`."""
        settings = self._settings
        while True:
            known = await self._known_session_member(session_id)
            if known is not None:
                return known, None
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
                if await self._probe(plan.member):
                    self._remember(session_id, plan.member)
                    return plan.member, reservation
                self._unreserve(plan.member, reservation)
                continue
            if plan.kind == "grow":
                return await self._grow_for(session_id)
            if self._clock() >= deadline:
                raise CapacityException(
                    capacity_message(settings.scaling), code=AGENT_CAPACITY_CODE
                )
            await self._sleep(AGENT_FLEET_BUSY_RETRY_SECONDS)

    def _reserve(self, member: Member | None) -> str | None:
        if member is None:
            return None
        member.inflight += 1
        if self.scaling.sessions != SESSIONS_DEDICATED:
            return None
        reservation = new_reservation()
        member.bound.add(reservation)
        return reservation

    @staticmethod
    def _unreserve(member: Member, reservation: str | None) -> None:
        member.inflight -= 1
        if reservation is not None:
            member.bound.discard(reservation)

    def _remember(self, session_id: str | None, member: Member) -> None:
        if session_id is not None and self.scaling.sessions == SESSIONS_DEDICATED:
            self._sessions[session_id] = member.sandbox_id

    async def _known_session_member(self, session_id: str | None) -> Member | None:
        """El VM dedicado de una sesión que el llamante eligió: el que este
        proceso recuerda o, con `session_index`, el que el índice encuentra."""
        if session_id is None or self.scaling.sessions != SESSIONS_DEDICATED:
            return None
        sandbox_id = self._sessions.get(session_id)
        if sandbox_id is None and self._index is not None:
            sandbox_id = await self._directory.find_session(
                self._name, session_label(session_id), self._index
            )
        if sandbox_id is None:
            return None
        member = await self._route_member(sandbox_id)
        self._remember(session_id, member)
        return member

    async def _grow_for(self, session_id: str | None) -> tuple[Member, str | None]:
        """Un VM nuevo para la sesión: ocupado desde el primer instante (una
        reserva si la flota es dedicada) para que ninguna otra se cuele."""
        dedicated = self.scaling.sessions == SESSIONS_DEDICATED
        try:
            member = await self._create_member(True, session_id=session_id if dedicated else None)
        finally:
            self._growing -= 1
        reservation = new_reservation() if dedicated else None
        member.inflight += 1
        if reservation is not None:
            member.bound.add(reservation)
        self._members.append(member)
        mark_extras(self._members, self.scaling)
        self._remember(session_id, member)
        return member, reservation

    async def _create_member(self, extra: bool, *, session_id: str | None = None) -> Member:
        """Un VM nuevo con servicio: si crece la flota (`extra`) y la sesión no
        necesita etiqueta propia, de una plaza caliente; si no, con
        `create()` (los VMs base no gastan las plazas)."""
        project = self._require_project()
        service = self._service
        assert service is not None
        await self._renew_keys()
        label = self._index_label(session_id)
        if extra and self._pool is not None and label is None:
            sandbox = await self._pool.take(gateways=dict(project.gateways))
            try:
                deployment = await service.serve(sandbox)
            except BaseException:
                await self._kill_quietly(sandbox)
                raise
        else:
            deployment = await service.deploy(
                region=self._region,
                session=self._session,
                access_token=self._require_token(),
                keep_on_failure=self._keep_on_failure,
                metadata=None if label is None else {AGENT_FLEET_SESSION_LABEL: label},
                index=None if label is None else self._index,
            )
        member = Member(deployment.sandbox_id, deployment=deployment, extra=extra)
        await self._limit_lifetime(member)
        return member

    def _index_label(self, session_id: str | None) -> str | None:
        """La etiqueta de sesión de un VM dedicado, sólo con índice."""
        if session_id is None or self._index is None:
            return None
        return session_label(session_id)

    @staticmethod
    async def _kill_quietly(sandbox: Any) -> None:
        try:
            await sandbox.kill()
        except Exception:
            logger.warning("no se pudo terminar el sandbox de la flota", exc_info=True)

    async def _limit_lifetime(self, member: Member) -> None:
        """A un VM por encima de `min_vms` se le acota la vida: `rayd` lo
        termina solo tras `scale_in_after_seconds`, y cada uso la renueva."""
        if not member.extra or member.deployment is None:
            return
        try:
            await member.deployment.set_timeout(self.scaling.scale_in_after_seconds)
        except SandboxException:
            logger.warning("no se pudo acotar la vida de un VM sobrante", exc_info=True)

    async def _open_or_connect(self, member: Member) -> AsyncAgentDeployment:
        """El cliente del VM: el que ya hay o, la primera vez, conecta con el
        token de la flota y, si el VM no tiene servicio (una plaza aparcada),
        lo arranca."""
        if member.deployment is not None:
            return member.deployment  # type: ignore[no-any-return]
        sandbox = await self._directory.connect(member.sandbox_id, self._require_token())
        deployment = await self._serving(member, sandbox)
        member.deployment = deployment
        await self._limit_lifetime(member)
        return deployment

    async def _serving(self, member: Member, sandbox: Any) -> AsyncAgentDeployment:
        if await sandbox.files.exists(AGENT_SERVICE_READY_MARKER_PATH):
            return await self._deployment_of(sandbox)
        service = self._service
        if service is None:
            raise InvalidArgumentException(NO_PROJECT_MESSAGE)
        await self._open_parked(member, sandbox)
        return await service.serve(sandbox)

    async def _open_parked(self, member: Member, sandbox: Any) -> None:
        """Como `AgentFleet._open_parked`: abre las pasarelas del proyecto en
        una plaza aparcada antes de arrancar el host, o la sustituye."""
        project = self._project
        if project is None or not project.gateways:
            return
        from rayito.sandbox_async.pool import apply_gateways

        await self._renew_keys()
        try:
            await apply_gateways(
                sandbox, dict(project.gateways), timeout=DEFAULT_REQUEST_TIMEOUT_SECONDS
            )
        except Exception as exc:
            logger.warning("una plaza aparcada no pudo abrir sus pasarelas: se sustituye")
            await self._replace(member)
            raise ParkedSlotException(PARKED_SLOT_MESSAGE) from exc

    async def _deployment_of(self, sandbox: Any) -> AsyncAgentDeployment:
        labels = parse_labels(sandbox.metadata)
        timeout = request_timeout(self._project) if self._project else None
        options = {} if timeout is None else {"request_timeout": timeout}
        return AsyncAgentDeployment(
            sandbox,
            port=labels.port,
            host_access=await sandbox.get_host(labels.port),
            project=labels.project,
            raw=labels.raw,
            transport_factory=self._transport_factory,
            **options,
        )

    async def _probe(self, member: Member, *, force: bool = False) -> bool:
        """Si el VM puede atender una sesión: lo abre si hace falta y, si su
        último ping caducó, le pregunta por `GET /ping`. Un VM perdido se
        sustituye."""
        try:
            deployment = await self._open_or_connect(member)
        except SandboxNotFoundException:
            self._drop(member)
            return False
        except ParkedSlotException:
            return False
        if not force and ping_is_fresh(member, self._clock()):
            return member.health != "unavailable"
        status: DeploymentStatus = await deployment.status()
        record_ping(member, status.state, self._clock())
        if is_lost(member):
            await self._replace(member)
            return False
        return status.state == "healthy"

    async def _note_transport_failure(self, member: Member) -> None:
        """Un fallo de transporte no basta para dar un VM por perdido: se
        pregunta a `/ping` y sólo tras `AGENT_FLEET_UNHEALTHY_PINGS` fallos
        seguidos se sustituye (la sesión de ese VM se pierde)."""
        deployment = member.deployment
        if deployment is None:
            return
        status = await deployment.status()
        record_ping(member, status.state, self._clock())
        if is_lost(member):
            await self._replace(member)

    async def _release(self, lease: Lease, inner_session: str | None) -> None:
        """Suelta la invocación en curso: la reserva de un VM dedicado pasa a
        ser la sesión real y a un VM sobrante se le renueva la vida."""
        self._unlease(lease, keep=inner_session)
        if inner_session is not None:
            await self._limit_lifetime(lease.member)

    @staticmethod
    def _unlease(lease: Lease, *, keep: str | None = None) -> None:
        member = lease.member
        member.inflight = max(0, member.inflight - 1)
        if lease.reservation is not None:
            member.bound.discard(lease.reservation)
            if keep is not None:
                member.bound.add(keep)

    def _drop(self, member: Member) -> None:
        if member in self._members:
            self._members.remove(member)
        mark_extras(self._members, self.scaling)

    async def _replace(self, member: Member) -> None:
        """Termina un VM perdido, lo saca de la flota y, si baja de `min_vms`,
        crea otro."""
        self._drop(member)
        try:
            await self._terminate(member)
        except Exception:
            logger.warning("no se pudo terminar un VM perdido", exc_info=True)
        await self._fill_to_minimum()

    async def _fill_to_minimum(self) -> None:
        if self._project is None:
            return
        for _ in range(replacements_needed(len(self._members), self.scaling)):
            member = await self._create_member(False)
            self._members.append(member)
            mark_extras(self._members, self.scaling)

    async def _trim(self) -> None:
        excess = [
            member
            for member in surplus(self._members, self.scaling)
            if member.inflight == 0 and not member.bound
        ]
        for member in excess:
            self._drop(member)
            try:
                await self._terminate(member)
            except Exception:
                logger.warning("no se pudo terminar un VM sobrante", exc_info=True)

    async def _terminate(self, member: Member) -> None:
        """Termina el VM sin abrirlo: abrir uno suspendido lo reanudaría sólo
        para matarlo."""
        if member.deployment is not None:
            await member.deployment.stop()
        else:
            await self._directory.kill(member.sandbox_id)

    async def _discard(self, members: Sequence[Member]) -> None:
        for member in members:
            try:
                await self._terminate(member)
            except Exception:
                logger.warning("no se pudo terminar un VM de un arranque fallido", exc_info=True)

    # ------------------------------------------------------- recipe and keys

    def _recipes(self) -> FleetRecipeStore:
        if self._recipe_store is None:
            self._recipe_store = recipe_aws.default_recipe_store(self._region, self._session)
        return self._recipe_store

    async def _save_recipe(self) -> str:
        """Como `AgentFleet._save_recipe`, con Parameter Store en un hilo."""
        project = self._require_project()
        recipe = recipe_of(
            project,
            name=self._name,
            image_version=getattr(self._service, "image_version", None),
            scaling=self.scaling,
            renew_gateway_keys=self._key_renewal.enabled,
        )
        outcome = await asyncio.to_thread(store_recipe, self._recipes(), recipe)
        if outcome == RECIPE_SAVED:
            self._recipe_known = True
        return outcome

    async def _renew_keys(self) -> None:
        """Como `AgentFleet._renew_keys`: la renovación (síncrona) corre en
        un hilo y los VMs abiertos refrescan su pasarela."""
        if not self._key_renewal.enabled or not await asyncio.to_thread(self._key_renewal.renew):
            return
        for deployment in self.deployments:
            try:
                await deployment.refresh_gateways()
            except SandboxException:
                logger.warning("no se pudo refrescar la pasarela de un VM de la flota")

    # -------------------------------------------------------------- warm pool

    def _pool_metadata(self) -> Mapping[str, str]:
        project = self._require_project()
        return labels_for(project.name, project.serve.port, raw=False)

    async def _start_pool(self) -> None:
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
        pool = factory(
            config,
            backend=self._pool_backend,
            region=self._region,
            session=self._session,
        )
        self._pool = await pool.start()

    async def _resize_pool(self) -> None:
        pool = self._pool
        if pool is not None and pool.config.size == self.scaling.warm_vms:
            return
        await self._close_pool(drain=True)
        await self._start_pool()

    async def _close_pool(self, *, drain: bool, errors: list[Exception] | None = None) -> None:
        pool, self._pool = self._pool, None
        if pool is None:
            return
        try:
            await pool.close(drain=drain)
        except Exception as exc:
            logger.warning("no se pudo cerrar el pool de plazas calientes", exc_info=True)
            if errors is not None:
                errors.append(exc)

    def _parked_ids(self) -> set[str]:
        if self._pool is None:
            return set()
        return {slot.sandbox_id for slot in self._pool.stats().slots}


def _default_pool(config: Any, **options: Any) -> AsyncSandboxPool:
    from rayito.sandbox_async.pool import AsyncSandboxPool

    return AsyncSandboxPool(config, **options)


__all__ = ["AsyncAgentFleet", "AsyncAgentFleetStream", "AsyncSandboxDirectory"]
