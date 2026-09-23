"""`AsyncSandbox`: la misma superficie que `Sandbox` sobre `grpc.aio`.

Las llamadas de plano de control (boto3) van por `asyncio.to_thread`; un
canal nunca se comparte con un `Sandbox` síncrono. El contrato de
reconexión es el de `Sandbox` con `asyncio.Lock` y `asyncio.sleep`.
"""

from __future__ import annotations

import asyncio
import builtins
import contextlib
import dataclasses
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from datetime import datetime
from types import TracebackType
from typing import TYPE_CHECKING, Any, Self, TypeVar, cast

import boto3
import grpc
import grpc.aio

from rayito._aws import ControlPlane, PortSpec, control_plane_session
from rayito._code_base import (
    DEFAULT_CODE_TIMEOUT_SECONDS,
    ContextLike,
    ErrorCallback,
    ResultCallback,
    StdoutCallback,
)
from rayito._lifecycle_base import (
    TimeoutRequest,
    auto_resume_reopen,
    connect_extension,
    deadline_may_have_moved,
    lifecycle_from_proto,
    lifecycle_from_state,
    older_agent_error,
    pause_trigger_delay,
    suspended_set_timeout_error,
    translate_set_timeout_error,
    unix_ms_now,
    validate_set_timeout_seconds,
)
from rayito._limits import (
    DEFAULT_PERSIST_TIMEOUT_SECONDS,
    DEFAULT_PORT,
    SUSPENDED_STATES,
    TERMINAL_STATES,
)
from rayito._listing_base import ListOrder, listing_request
from rayito._metrics_base import (
    CLASS_HISTORY_FEATURE,
    HISTORY_FEATURE,
    ensure_history_readable,
    history_unimplemented_error,
    is_history_unimplemented,
    metrics_history_from_proto,
    metrics_history_request,
)
from rayito._models import (
    AsyncUploadTicket,
    CheckpointResult,
    CodeContext,
    DownloadLink,
    Execution,
    HostAccess,
    IdlePolicy,
    LaunchOptions,
    NetworkOptions,
    NetworkPolicy,
    NetworkState,
    RestoreResult,
    S3Prefix,
    S3Staging,
    SandboxHealth,
    SandboxInfo,
    SandboxLifecycle,
    SandboxListItem,
    SandboxMetrics,
    TimeoutActionName,
)
from rayito._network_base import (
    GET_NETWORK_FEATURE,
    UPDATE_NETWORK_FEATURE,
    NetworkLaunch,
    egress_gate_error,
    network_rpc_error,
    plan_network_launch,
    policy_to_proto,
    pool_network_kwarg,
    readiness_enforcement,
    state_from_proto,
    update_policy,
)
from rayito._persistence_base import (
    CheckpointProgressCallback,
    RestoreProgressCallback,
    add_reincarnate_note,
    bind_persist,
    launch_kwargs,
    reincarnate_requires_create_error,
    reincarnate_requires_persist_error,
    require_named_persist,
    require_role_for_persist,
    should_auto_restore,
)
from rayito._pool_base import reject_launch_kwargs_with_pool
from rayito._process_base import (
    STREAM_EOF,
    STREAM_PROBE_TIMEOUT_SECONDS,
    metrics_from_proto,
    stream_failure_exception,
)
from rayito._sandbox_base import (
    CLOCK_OFFSET_WARN_MS,
    DEFAULT_IDLE_POLICY,
    DEFAULT_READY_TIMEOUT_SECONDS,
    DEFAULT_RECONNECT_TIMEOUT_SECONDS,
    DEFAULT_REQUEST_TIMEOUT_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
    HOOK_ANOMALIES_WARNING,
    IMDS_OPEN_WARNING,
    METADATA_PROBE_TIMEOUT_SECONDS,
    GuestFacts,
    LoggingOption,
    PortLike,
    ReadinessPoll,
    ReconnectOutcome,
    ReconnectPoll,
    already_suspended,
    build_launch_plan,
    class_method_variant,
    guest_facts_from_health,
    health_from_proto,
    health_ready,
    health_reconnected,
    imds_open_warning_due,
    info_with_health,
    is_suspending_reason,
    metadata_from_health,
    metadata_probe_failure,
    needs_explicit_resume,
    not_ready_error,
    ready_guest_facts,
    reconnect_failure,
    require_access_token,
    resolve_template,
    sandbox_logger,
    terminal_state_error,
    terminated_during_boot_error,
    validate_host_port,
    validate_sandbox_id,
    with_guest_facts,
)
from rayito._transfer_base import (
    expires_in_from_signature_expiration,
    resolve_staging,
    validate_staging_against_persist,
)
from rayito._transport import (
    AsyncTokenRefresher,
    ProxyAuthPlugin,
    TokenRefresher,
    TokenStore,
    TransportSettings,
    is_not_yet_reachable,
    is_proxy_forbidden,
    is_reconnectable,
    is_sandbox_timeout,
    is_stream_reset,
    rpc_status,
    translate_rpc_error,
)
from rayito.exceptions import (
    InvalidArgumentException,
    NotFoundException,
    SandboxException,
    SandboxNotFoundException,
    SandboxNotReadyException,
)
from rayito.sandbox_async.code import AsyncCodeClient
from rayito.sandbox_async.commands import AsyncCommands, StreamStarter
from rayito.sandbox_async.filesystem import AsyncFilesystem
from rayito.sandbox_async.git import AsyncGit
from rayito.sandbox_async.lifecycle import AsyncDeadlineTrigger, set_timeout_once_async
from rayito.sandbox_async.listing import (
    AsyncListingIo,
    AsyncSandboxListPaginator,
    collect_listing,
)
from rayito.sandbox_async.persistence import AsyncPersistenceClient
from rayito.sandbox_async.pty import AsyncPty
from rayito.sandbox_async.transfer import AsyncTransfers
from rayito.sandbox_sync.main import (
    StubFactory,
    closed_during_reconnect,
    resolve_control_plane,
    terminate_quietly,
    wait_for_state,
)
from rayito.v1 import (
    code_pb2_grpc,
    filesystem_pb2_grpc,
    health_pb2,
    health_pb2_grpc,
    lifecycle_pb2_grpc,
    network_pb2,
    network_pb2_grpc,
    process_pb2_grpc,
    pty_pb2_grpc,
)

if TYPE_CHECKING:
    from rayito.sandbox_async.pool import AsyncSandboxPool

logger = logging.getLogger("rayito.sandbox")

T = TypeVar("T")


async def wait_for_state_async(
    control_plane: ControlPlane, sandbox_id: str, wanted: str, *, timeout: float
) -> SandboxInfo:
    return await asyncio.to_thread(
        wait_for_state, control_plane, sandbox_id, wanted, timeout=timeout
    )


async def first_stream_message_async(call: Any, *, allow_empty: bool = False) -> tuple[Any, Any]:
    """`read()` del primer mensaje de un server-stream `grpc.aio`; sólo `Read`
    de un fichero vacío puede terminar sin mensajes (`None`)."""
    first = await call.read()
    if first is STREAM_EOF:
        if allow_empty:
            return call, None
        raise SandboxException("el stream terminó antes del primer mensaje")
    return call, first


async def probe_health_async(
    control_plane: ControlPlane,
    info: SandboxInfo,
    transport: TransportSettings,
    request_timeout: float,
) -> health_pb2.HealthResponse | None:
    """`sandbox_sync.main.probe_health` sobre `grpc.aio`: un JWE (boto3 en
    un hilo), un `Health` por un canal dedicado que se cierra al salir;
    `None` sin tocar el endpoint cuando el estado no es `RUNNING`."""
    if info.state != "RUNNING":
        return None
    refresher = TokenRefresher(
        TokenStore(), lambda ports: control_plane.create_auth_token(info.sandbox_id, ports)
    )
    await asyncio.to_thread(refresher.mint, (PortSpec.single(DEFAULT_PORT),))
    plugin = ProxyAuthPlugin(
        refresher.store,
        port=DEFAULT_PORT,
        access_token=None,
        extra=transport.extra_metadata,
    )
    channel = transport.open_aio_channel(info.endpoint, plugin)
    try:
        response = await probe_health_reminting_async(
            health_pb2_grpc.HealthServiceStub(channel), refresher, request_timeout
        )
    except grpc.RpcError as exc:
        raise metadata_probe_failure(info.sandbox_id, exc) from exc
    finally:
        await channel.close(grace=None)
    return cast("health_pb2.HealthResponse", response)


async def probe_metadata_async(
    control_plane: ControlPlane,
    info: SandboxInfo,
    transport: TransportSettings,
    request_timeout: float,
) -> dict[str, str] | None:
    """`sandbox_sync.main.probe_metadata` sobre `probe_health_async`."""
    response = await probe_health_async(control_plane, info, transport, request_timeout)
    if response is None:
        return None
    if not response.agent_ready:
        logger.info("sandbox %s aún arrancando: sin metadatos", info.sandbox_id)
        return None
    return metadata_from_health(response)


async def probe_health_reminting_async(stub: Any, refresher: TokenRefresher, timeout: float) -> Any:
    try:
        return await stub.Health(health_pb2.HealthRequest(), timeout=timeout)
    except grpc.RpcError as exc:
        if not is_proxy_forbidden(exc):
            raise
    await asyncio.to_thread(refresher.refresh_all)
    return await stub.Health(health_pb2.HealthRequest(), timeout=timeout)


async def call_dedicated_health_async(
    control_plane: ControlPlane,
    info: SandboxInfo,
    transport: TransportSettings,
    access_token: str,
    invoke: Callable[[health_pb2_grpc.HealthServiceStub], Awaitable[T]],
) -> T:
    """`sandbox_sync.main.call_dedicated_health` sobre `grpc.aio`: el JWE
    (boto3 en un hilo), un canal dedicado con el access token que se cierra
    al salir, un reintento tras un 403 del proxy y la traducción unaria."""
    refresher = TokenRefresher(
        TokenStore(), lambda ports: control_plane.create_auth_token(info.sandbox_id, ports)
    )
    await asyncio.to_thread(refresher.mint, (PortSpec.single(DEFAULT_PORT),))
    plugin = ProxyAuthPlugin(
        refresher.store,
        port=DEFAULT_PORT,
        access_token=access_token,
        extra=transport.extra_metadata,
    )
    channel = transport.open_aio_channel(info.endpoint, plugin)
    try:
        stub = health_pb2_grpc.HealthServiceStub(channel)
        return await invoke_reminting_async(invoke, stub, refresher)
    except grpc.RpcError as exc:
        raise translate_rpc_error(exc) from exc
    finally:
        await channel.close(grace=None)


async def invoke_reminting_async(
    invoke: Callable[[Any], Awaitable[T]], stub: Any, refresher: TokenRefresher
) -> T:
    try:
        return await invoke(stub)
    except grpc.RpcError as exc:
        if not is_proxy_forbidden(exc):
            raise
    await asyncio.to_thread(refresher.refresh_all)
    return await invoke(stub)


class AsyncSandbox:
    """Un MicroVM con `rayd` dentro. Se crea con `await AsyncSandbox.create()`."""

    def __init__(
        self,
        *,
        info: SandboxInfo,
        access_token: str,
        control_plane: ControlPlane,
        transport: TransportSettings,
        refresher: AsyncTokenRefresher,
        request_timeout: float,
        ready_timeout: float,
        reconnect_timeout: float = DEFAULT_RECONNECT_TIMEOUT_SECONDS,
        logger: logging.Logger | None = None,
    ) -> None:
        self._custom_logger = logger
        self._logger = sandbox_logger(logger)
        self._info = info
        self._launch_info = info
        self._access_token = access_token
        self._control_plane = control_plane
        self._transport = transport
        self._refresher = refresher
        self._request_timeout = request_timeout
        self._ready_timeout = ready_timeout
        self._reconnect_timeout = reconnect_timeout
        self._closed = False
        self._resume_generation = 0
        self._metadata: dict[str, str] = {}
        self._guest = GuestFacts()
        self._paused = False
        self._anomalies_warned_generation: int | None = None
        self._imds_warned = False
        self._ready_uptime_ms: int | None = None
        self._reconnect_lock = asyncio.Lock()
        self._resumed = asyncio.Event()
        self._plugin = ProxyAuthPlugin(
            refresher.store,
            port=DEFAULT_PORT,
            access_token=access_token,
            extra=transport.extra_metadata,
        )
        self._channel = transport.open_aio_channel(info.endpoint, self._plugin)
        self._stream_channel: grpc.aio.Channel | None = None
        self._health = health_pb2_grpc.HealthServiceStub(self._channel)
        self._process = process_pb2_grpc.ProcessServiceStub(self._channel)
        self._files = filesystem_pb2_grpc.FilesystemServiceStub(self._channel)
        self._code = code_pb2_grpc.CodeServiceStub(self._channel)
        self._pty_stub = pty_pb2_grpc.PtyServiceStub(self._channel)
        self._network_stub = network_pb2_grpc.NetworkServiceStub(self._channel)
        self._readiness_health: SandboxHealth | None = None
        self._lifecycle_stub = lifecycle_pb2_grpc.LifecycleServiceStub(self._channel)
        self._lifecycle: SandboxLifecycle | None = None
        self._deadline_pause_generation: int | None = None
        self._reopen_task: asyncio.Task[bool] | None = None
        self._reopens = 0
        self._deadline_trigger = AsyncDeadlineTrigger(self._on_deadline)
        self._unary_stubs: dict[StubFactory, Any] = {
            process_pb2_grpc.ProcessServiceStub: self._process,
            filesystem_pb2_grpc.FilesystemServiceStub: self._files,
            code_pb2_grpc.CodeServiceStub: self._code,
            pty_pb2_grpc.PtyServiceStub: self._pty_stub,
        }
        self._stream_stubs: dict[StubFactory, Any] = {}
        self._commands = AsyncCommands(self)
        self._filesystem = AsyncFilesystem(self)
        self._code_client = AsyncCodeClient(self)
        self._pty = AsyncPty(self)
        self._persistence = AsyncPersistenceClient(self)
        self._persist: S3Prefix | None = None
        self._last_restore: RestoreResult | None = None
        self._launch_options: LaunchOptions | None = None
        self._transfer: S3Staging | None = None
        self._session: boto3.session.Session | None = None
        self._transfers = AsyncTransfers(self)
        self._git: AsyncGit | None = None

    # ------------------------------------------------------------------ create

    @classmethod
    async def create(
        cls,
        template: str | None = None,
        *,
        template_version: str | None = None,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
        max_lifetime: int | None = None,
        on_timeout: TimeoutActionName | None = None,
        idle: IdlePolicy | None = DEFAULT_IDLE_POLICY,
        envs: Mapping[str, str] | None = None,
        metadata: Mapping[str, str] | None = None,
        cpu_time_limit: int | None = None,
        execution_role_arn: str | None = None,
        allowed_ports: Sequence[PortLike] | None = None,
        ingress: Sequence[str] | None = None,
        egress: Sequence[str] | None = None,
        network: NetworkPolicy | NetworkOptions | None = None,
        allow_internet_access: bool = True,
        logging: LoggingOption = "disabled",
        region: str | None = None,
        session: boto3.session.Session | None = None,
        access_token: str | None = None,
        ready_timeout: float = DEFAULT_READY_TIMEOUT_SECONDS,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
        reconnect_timeout: float = DEFAULT_RECONNECT_TIMEOUT_SECONDS,
        keep_on_failure: bool = False,
        control_plane: ControlPlane | None = None,
        transport: TransportSettings | None = None,
        persist: S3Prefix | None = None,
        persist_timeout: float = DEFAULT_PERSIST_TIMEOUT_SECONDS,
        pool: AsyncSandboxPool | None = None,
        transfer: S3Staging | None = None,
        logger: logging.Logger | None = None,
    ) -> Self:
        """Misma semántica que `Sandbox.create` (incluidos `metadata`, `pool=`,
        `persist=`, `transfer=`, el plazo lógico de `max_lifetime`/`on_timeout`
        y la política de egress de `network=`/`allow_internet_access`, que
        termina el VM aunque haya `keep_on_failure` si la imagen no la aplica)."""
        launch = plan_network_launch(network, allow_internet_access=allow_internet_access)
        require_role_for_persist(persist, execution_role_arn)
        staging = resolve_staging(transfer)
        validate_staging_against_persist(staging, persist)
        if pool is not None and persist is not None:
            raise InvalidArgumentException(
                "create(pool=...) no admite persist=: una plaza del pool no puede restaurar "
                "un home con nombre al tomarla"
            )
        if pool is not None:
            reject_launch_kwargs_with_pool(
                {
                    "template": template,
                    "template_version": template_version,
                    "timeout": timeout,
                    "max_lifetime": max_lifetime,
                    "on_timeout": on_timeout,
                    "idle": idle,
                    "envs": envs,
                    "metadata": metadata,
                    "cpu_time_limit": cpu_time_limit,
                    "execution_role_arn": execution_role_arn,
                    "allowed_ports": allowed_ports,
                    "ingress": ingress,
                    "egress": egress,
                    "network": pool_network_kwarg(network),
                    "allow_internet_access": allow_internet_access,
                    "logging": logging,
                    "region": region,
                    "session": session,
                    "access_token": access_token,
                    "keep_on_failure": keep_on_failure,
                    "control_plane": control_plane,
                    "transport": transport,
                }
            )
            taken = cast(
                "Self",
                await pool.take(
                    ready_timeout=ready_timeout,
                    request_timeout=request_timeout,
                    reconnect_timeout=reconnect_timeout,
                ),
            )
            taken._bind_transfer(staging, pool.session)
            taken._bind_logger(logger)
            return taken
        plane = resolve_control_plane(control_plane, session, region)
        image_arn = await asyncio.to_thread(plane.resolve_template_arn, resolve_template(template))
        plan = build_launch_plan(
            image_arn=image_arn,
            region=plane.region,
            template_version=template_version,
            timeout=timeout,
            idle=idle,
            envs=envs,
            execution_role_arn=execution_role_arn,
            allowed_ports=allowed_ports,
            ingress=ingress,
            egress=egress,
            logging=logging,
            access_token=access_token,
            metadata=metadata,
            cpu_time_limit=cpu_time_limit,
            max_lifetime=max_lifetime,
            on_timeout=on_timeout,
            network_enforce=launch.enforce,
        )
        info = await asyncio.to_thread(plane.run_microvm, plan.request)
        sandbox_logger(logger).info("run-microvm aceptado: %s (%s)", info.sandbox_id, info.state)
        sandbox = await cls._open(
            info,
            access_token=plan.access_token,
            control_plane=plane,
            transport=transport or TransportSettings(),
            proxy_ports=plan.proxy_ports,
            request_timeout=request_timeout,
            ready_timeout=ready_timeout,
            reconnect_timeout=reconnect_timeout,
            terminate_on_failure=not keep_on_failure,
            require_lifecycle=plan.lifecycle_requested,
            logger=logger,
        )
        sandbox._bind_transfer(staging, session)
        if launch.enforce:
            await sandbox._apply_initial_network(launch)
        sandbox._launch_options = LaunchOptions(
            template=image_arn,
            template_version=template_version,
            timeout=timeout,
            idle=idle,
            envs=None if envs is None else dict(envs),
            metadata=None if metadata is None else dict(metadata),
            cpu_time_limit=cpu_time_limit,
            execution_role_arn=execution_role_arn,
            allowed_ports=None if allowed_ports is None else tuple(allowed_ports),
            ingress=None if ingress is None else tuple(ingress),
            egress=None if egress is None else tuple(egress),
            logging=logging,
            access_token=access_token,
            ready_timeout=ready_timeout,
            request_timeout=request_timeout,
            reconnect_timeout=reconnect_timeout,
            keep_on_failure=keep_on_failure,
            control_plane=plane,
            transport=transport,
            max_lifetime=max_lifetime,
            on_timeout=on_timeout,
            network=launch.stored_policy,
        )
        if persist is not None:
            await sandbox._bind_and_restore(
                persist, persist_timeout, terminate_on_failure=not keep_on_failure
            )
        return sandbox

    @class_method_variant("_class_connect")
    async def connect(
        self, *, timeout: int | None = None, request_timeout: float | None = None
    ) -> AsyncSandbox:
        """Misma semántica que `sbx.connect()` de `Sandbox`: reabre este
        handle y extiende el plazo (`AT_LEAST`). Devuelve `self`."""
        info = await asyncio.to_thread(self._control_plane.get_microvm, self.sandbox_id)
        if info.state in TERMINAL_STATES:
            raise terminal_state_error(info)
        self._info = info
        self._paused = False
        if needs_explicit_resume(info):
            await asyncio.to_thread(self._control_plane.resume_microvm, self.sandbox_id)
            await self._refresher.refresh_all()
        await self._wait_until_ready(terminate_on_failure=False)
        await self._extend_after_readiness(timeout, request_timeout=request_timeout)
        return self

    @classmethod
    async def _class_connect(
        cls,
        sandbox_id: str,
        *,
        timeout: int | None = None,
        access_token: str | None = None,
        region: str | None = None,
        session: boto3.session.Session | None = None,
        ready_timeout: float = DEFAULT_READY_TIMEOUT_SECONDS,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
        reconnect_timeout: float = DEFAULT_RECONNECT_TIMEOUT_SECONDS,
        control_plane: ControlPlane | None = None,
        transport: TransportSettings | None = None,
        persist: S3Prefix | None = None,
        transfer: S3Staging | None = None,
        logger: logging.Logger | None = None,
    ) -> Self:
        """Misma semántica que `Sandbox.connect(sandbox_id)` (incluidos
        `persist=`, que sólo enlaza el prefijo con `name`, `transfer=` y
        `timeout`, que nunca acorta el plazo lógico)."""
        token = require_access_token(access_token)
        bound = None if persist is None else require_named_persist(persist)
        staging = resolve_staging(transfer)
        validate_staging_against_persist(staging, bound)
        plane = resolve_control_plane(control_plane, session, region)
        info = await asyncio.to_thread(plane.get_microvm, validate_sandbox_id(sandbox_id))
        if info.state in TERMINAL_STATES:
            raise terminal_state_error(info)
        if needs_explicit_resume(info):
            await asyncio.to_thread(plane.resume_microvm, sandbox_id)
        sandbox = await cls._open(
            info,
            access_token=token,
            control_plane=plane,
            transport=transport or TransportSettings(),
            proxy_ports=(PortSpec.single(DEFAULT_PORT),),
            request_timeout=request_timeout,
            ready_timeout=ready_timeout,
            reconnect_timeout=reconnect_timeout,
            terminate_on_failure=False,
            logger=logger,
        )
        sandbox._persist = bound
        sandbox._bind_transfer(staging, session)
        try:
            await sandbox._extend_after_readiness(timeout, request_timeout=None)
        except BaseException:
            await sandbox.close()
            raise
        return sandbox

    @classmethod
    async def _open(
        cls,
        info: SandboxInfo,
        *,
        access_token: str,
        control_plane: ControlPlane,
        transport: TransportSettings,
        proxy_ports: Sequence[PortSpec],
        request_timeout: float,
        ready_timeout: float,
        reconnect_timeout: float,
        terminate_on_failure: bool,
        readiness: type[ReadinessPoll] = ReadinessPoll,
        require_lifecycle: bool = False,
        logger: logging.Logger | None = None,
    ) -> Self:
        """Misma política de limpieza que `Sandbox._open`: con
        `terminate_on_failure`, todo fallo previo al primer `agent_ready` que no
        sea `SandboxNotReadyException` termina el MicroVM. `readiness` es el
        calendario del sondeo (`TakePoll` desde el pool) y `require_lifecycle`
        la puerta de agente M9 de un lanzamiento con bloque `lifecycle`."""
        refresher = AsyncTokenRefresher(
            TokenRefresher(
                TokenStore(),
                lambda ports: control_plane.create_auth_token(info.sandbox_id, ports),
                logger=logger,
            )
        )
        sandbox: Self | None = None
        try:
            await refresher.mint(proxy_ports)
            sandbox = cls(
                info=info,
                access_token=access_token,
                control_plane=control_plane,
                transport=transport,
                refresher=refresher,
                request_timeout=request_timeout,
                ready_timeout=ready_timeout,
                reconnect_timeout=reconnect_timeout,
                logger=logger,
            )
            ready = await sandbox._wait_until_ready(
                terminate_on_failure=terminate_on_failure, readiness=readiness
            )
            if require_lifecycle and lifecycle_from_proto(ready) is None:
                raise older_agent_error(info.template_name, str(ready.agent_version))
        except BaseException as exc:
            if sandbox is not None:
                await sandbox.close()
            if terminate_on_failure and not isinstance(exc, SandboxNotReadyException):
                await asyncio.to_thread(
                    terminate_quietly,
                    control_plane,
                    info.sandbox_id,
                    sandbox_logger(logger),
                )
            raise
        refresher.start()
        return sandbox

    @classmethod
    async def list(
        cls,
        *,
        template: str | None = None,
        template_version: str | None = None,
        states: Iterable[str] | None = None,
        metadata: Mapping[str, str] | None = None,
        started_after: datetime | None = None,
        order: ListOrder | None = None,
        region: str | None = None,
        session: boto3.session.Session | None = None,
        control_plane: ControlPlane | None = None,
        transport: TransportSettings | None = None,
        request_timeout: float = METADATA_PROBE_TIMEOUT_SECONDS,
    ) -> builtins.list[SandboxListItem]:
        """Misma semántica y mismo coste O(n) con `metadata` que `Sandbox.list`;
        las sondas de `Health` van por `grpc.aio`, una tras otra."""
        request = listing_request(
            template=None,
            template_version=template_version,
            states=states,
            metadata=metadata,
            started_after=started_after,
            order=order,
            limit=None,
            next_token=None,
        )
        plane = resolve_control_plane(control_plane, session, region)
        image_arn = (
            await asyncio.to_thread(plane.resolve_template_arn, template) if template else None
        )
        io = AsyncListingIo(
            plane, probe_metadata_async, transport or TransportSettings(), request_timeout
        )
        return await collect_listing(io, request, image_arn)

    @classmethod
    def paginate(
        cls,
        *,
        template: str | None = None,
        template_version: str | None = None,
        states: Iterable[str] | None = None,
        metadata: Mapping[str, str] | None = None,
        started_after: datetime | None = None,
        order: ListOrder | None = None,
        limit: int | None = None,
        next_token: str | None = None,
        region: str | None = None,
        session: boto3.session.Session | None = None,
        control_plane: ControlPlane | None = None,
        transport: TransportSettings | None = None,
        request_timeout: float = METADATA_PROBE_TIMEOUT_SECONDS,
    ) -> AsyncSandboxListPaginator:
        """`Sandbox.paginate` con `next_items()` asíncrono. No es `async`:
        construir el paginador valida sin hacer E/S."""
        request = listing_request(
            template=template,
            template_version=template_version,
            states=states,
            metadata=metadata,
            started_after=started_after,
            order=order,
            limit=limit,
            next_token=next_token,
        )
        plane = resolve_control_plane(control_plane, session, region)
        io = AsyncListingIo(
            plane, probe_metadata_async, transport or TransportSettings(), request_timeout
        )
        return AsyncSandboxListPaginator(
            io=io, request=request, resolve_template_arn=plane.resolve_template_arn
        )

    # -------------------------------------------------------------- properties

    @property
    def sandbox_id(self) -> str:
        return self._info.sandbox_id

    @property
    def access_token(self) -> str:
        return self._access_token

    @property
    def endpoint(self) -> str:
        return self._info.endpoint

    @property
    def endpoint_url(self) -> str:
        return self._info.endpoint_url

    @property
    def info(self) -> SandboxInfo:
        return self._info

    @property
    def launch_info(self) -> SandboxInfo:
        """La `SandboxInfo` con la que se abrió el handle, nunca refrescada."""
        return self._launch_info

    @property
    def region(self) -> str:
        return self._control_plane.region

    @property
    def resume_generation(self) -> int:
        return self._resume_generation

    @property
    def metadata(self) -> dict[str, str]:
        return dict(self._metadata)

    @property
    def persist(self) -> S3Prefix | None:
        """El `S3Prefix` (con `name`) enlazado por `create(persist=)` o
        `connect(persist=)`; `None` si el sandbox no persiste."""
        return self._persist

    @property
    def transfer(self) -> S3Staging | None:
        """Misma semántica que `Sandbox.transfer`."""
        return self._transfer

    @property
    def last_restore(self) -> RestoreResult | None:
        """El resultado del restore automático de `create(persist=)`; `None` si
        no hubo (primera vida de ese `name`) o si el sandbox no persiste."""
        return self._last_restore

    # --------------------------------------------------------------- lifecycle

    @class_method_variant("_class_kill")
    async def kill(self) -> bool:
        try:
            return await asyncio.to_thread(self._control_plane.terminate_microvm, self.sandbox_id)
        finally:
            await self.close()

    @classmethod
    async def _class_kill(
        cls,
        sandbox_id: str,
        *,
        region: str | None = None,
        session: boto3.session.Session | None = None,
        control_plane: ControlPlane | None = None,
    ) -> bool:
        plane = resolve_control_plane(control_plane, session, region)
        return await asyncio.to_thread(plane.terminate_microvm, validate_sandbox_id(sandbox_id))

    @class_method_variant("_class_get_info")
    async def get_info(self) -> SandboxInfo:
        """Misma semántica que `Sandbox.get_info`: un `Health` sólo si el
        sandbox está `RUNNING` con plazo lógico gestionado, para refrescarlo."""
        refreshed = await asyncio.to_thread(self._control_plane.get_microvm, self.sandbox_id)
        if deadline_may_have_moved(refreshed.state, self._lifecycle):
            await self._refresh_health()
        self._info = dataclasses.replace(
            refreshed, metadata=self.metadata, lifecycle=self._lifecycle
        )
        return with_guest_facts(self._info, self._guest)

    @classmethod
    async def _class_get_info(
        cls,
        sandbox_id: str,
        *,
        read_metadata: bool = True,
        request_timeout: float = METADATA_PROBE_TIMEOUT_SECONDS,
        region: str | None = None,
        session: boto3.session.Session | None = None,
        control_plane: ControlPlane | None = None,
        transport: TransportSettings | None = None,
    ) -> SandboxInfo:
        plane = resolve_control_plane(control_plane, session, region)
        info = await asyncio.to_thread(plane.get_microvm, validate_sandbox_id(sandbox_id))
        if not read_metadata:
            return info
        probed = await probe_health_async(
            plane, info, transport or TransportSettings(), request_timeout
        )
        return with_guest_facts(info_with_health(info, probed), ready_guest_facts(probed))

    @class_method_variant("_class_pause")
    async def pause(self, *, wait: bool = True) -> bool:
        """Misma semántica que `Sandbox.pause`: mientras la pausa esté
        pendiente ningún stream en curso sondea `Health`."""
        self._info = await asyncio.to_thread(self._control_plane.get_microvm, self.sandbox_id)
        if already_suspended(self._info):
            return False
        suspended = await self._suspend_marking_paused()
        if suspended and wait:
            self._info = await wait_for_state_async(
                self._control_plane, self.sandbox_id, "SUSPENDED", timeout=self._ready_timeout
            )
        return suspended

    @classmethod
    async def _class_pause(
        cls,
        sandbox_id: str,
        *,
        wait: bool = True,
        region: str | None = None,
        session: boto3.session.Session | None = None,
        control_plane: ControlPlane | None = None,
        ready_timeout: float = DEFAULT_READY_TIMEOUT_SECONDS,
    ) -> bool:
        plane = resolve_control_plane(control_plane, session, region)
        info = await asyncio.to_thread(plane.get_microvm, validate_sandbox_id(sandbox_id))
        if already_suspended(info):
            return False
        suspended = await asyncio.to_thread(plane.suspend_microvm, sandbox_id)
        if suspended and wait:
            await wait_for_state_async(plane, sandbox_id, "SUSPENDED", timeout=ready_timeout)
        return suspended

    @class_method_variant("_class_resume")
    async def resume(self, *, wait: bool = True) -> None:
        """Misma semántica que `Sandbox.resume` (incluida la reapertura de un
        sandbox reanudado después de su plazo lógico)."""
        self._paused = False
        await asyncio.to_thread(self._control_plane.resume_microvm, self.sandbox_id)
        await self._refresher.refresh_all()
        if wait:
            await self._wait_until_ready(terminate_on_failure=False)
            await self._extend_after_readiness(None, request_timeout=None)

    @classmethod
    async def _class_resume(
        cls,
        sandbox_id: str,
        *,
        wait: bool = True,
        region: str | None = None,
        session: boto3.session.Session | None = None,
        control_plane: ControlPlane | None = None,
        ready_timeout: float = DEFAULT_READY_TIMEOUT_SECONDS,
    ) -> None:
        plane = resolve_control_plane(control_plane, session, region)
        await asyncio.to_thread(plane.resume_microvm, validate_sandbox_id(sandbox_id))
        if wait:
            await wait_for_state_async(plane, sandbox_id, "RUNNING", timeout=ready_timeout)

    @class_method_variant("_class_set_timeout")
    async def set_timeout(self, timeout: int, *, request_timeout: float | None = None) -> None:
        """Misma semántica que `Sandbox.set_timeout` (`SetTimeout` EXACT)."""
        seconds = validate_set_timeout_seconds(timeout)
        await self._send_set_timeout(
            TimeoutRequest("exact", seconds * 1000), request_timeout=request_timeout
        )

    @classmethod
    async def _class_set_timeout(
        cls,
        sandbox_id: str,
        timeout: int,
        *,
        access_token: str | None = None,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
        region: str | None = None,
        session: boto3.session.Session | None = None,
        control_plane: ControlPlane | None = None,
        transport: TransportSettings | None = None,
    ) -> None:
        """Misma semántica que `Sandbox.set_timeout(sandbox_id, timeout)`:
        nunca despierta un sandbox suspendido."""
        token = require_access_token(access_token, operation="set_timeout(sandbox_id)")
        seconds = validate_set_timeout_seconds(timeout)
        plane = resolve_control_plane(control_plane, session, region)
        info = await asyncio.to_thread(plane.get_microvm, validate_sandbox_id(sandbox_id))
        if info.state in TERMINAL_STATES:
            raise terminal_state_error(info)
        if info.state in SUSPENDED_STATES:
            raise suspended_set_timeout_error(info.sandbox_id)
        request = TimeoutRequest("exact", seconds * 1000)
        try:
            await set_timeout_once_async(
                plane,
                info,
                access_token=token,
                request=request,
                transport=transport or TransportSettings(),
                request_timeout=request_timeout,
            )
        except grpc.RpcError as exc:
            raise translate_set_timeout_error(exc, request) from exc

    async def is_running(self, *, request_timeout: float | None = None) -> bool:
        """Como `Sandbox.is_running`."""
        timeout = min(ReadinessPoll.MAX_RPC_TIMEOUT, self._resolve_request_timeout(request_timeout))
        response = await self._probe_health(timeout)
        return response is not None and response.agent_ready

    async def get_health(self, *, request_timeout: float | None = None) -> SandboxHealth:
        timeout = self._resolve_request_timeout(request_timeout)
        response = await self._translated_unary(
            lambda: self._health.Health(health_pb2.HealthRequest(), timeout=timeout)
        )
        self._record_health(response)
        return health_from_proto(response)

    async def upload_url(
        self,
        path: str,
        user: str | None = None,
        use_signature_expiration: int | None = None,
    ) -> AsyncUploadTicket:
        """Misma semántica que `Sandbox.upload_url`."""
        return await self._filesystem.upload_url(
            path,
            user=user,
            expires_in=expires_in_from_signature_expiration(use_signature_expiration),
        )

    async def download_url(
        self,
        path: str,
        user: str | None = None,
        use_signature_expiration: int | None = None,
    ) -> DownloadLink:
        """Misma semántica que `Sandbox.download_url`."""
        return await self._filesystem.download_url(
            path,
            user=user,
            expires_in=expires_in_from_signature_expiration(use_signature_expiration),
        )

    async def get_host(self, port: int) -> HostAccess:
        validated = validate_host_port(port)
        await self._refresher.ensure(validated)
        return HostAccess(
            self.endpoint, port=validated, token_provider=lambda: self._current_jwe(validated)
        )

    async def get_metrics(self, *, request_timeout: float | None = None) -> SandboxMetrics:
        timeout = self._resolve_request_timeout(request_timeout)
        response = await self._translated_unary(
            lambda: self._health.Metrics(health_pb2.MetricsRequest(), timeout=timeout)
        )
        return metrics_from_proto(response)

    @class_method_variant("_class_get_metrics_history")
    async def get_metrics_history(
        self,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        max_points: int | None = None,
        request_timeout: float | None = None,
    ) -> builtins.list[SandboxMetrics]:
        """Misma semántica que `Sandbox.get_metrics_history`."""
        request = metrics_history_request(start, end, max_points)
        timeout = self._resolve_request_timeout(request_timeout)
        try:
            response = await self._translated_unary(
                lambda: self._health.MetricsHistory(request, timeout=timeout)
            )
        except SandboxException as exc:
            if is_history_unimplemented(exc):
                raise history_unimplemented_error(exc, HISTORY_FEATURE) from exc
            raise
        return metrics_history_from_proto(response)

    @classmethod
    async def _class_get_metrics_history(
        cls,
        sandbox_id: str,
        *,
        access_token: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        max_points: int | None = None,
        request_timeout: float | None = None,
        region: str | None = None,
        session: boto3.session.Session | None = None,
        control_plane: ControlPlane | None = None,
        transport: TransportSettings | None = None,
    ) -> builtins.list[SandboxMetrics]:
        """`AsyncSandbox.get_metrics_history(sandbox_id)`: la variante de clase
        de `Sandbox` con `get-microvm` en un hilo y el canal por `grpc.aio`."""
        validated_id = validate_sandbox_id(sandbox_id)
        token = require_access_token(access_token, operation="get_metrics_history(sandbox_id)")
        request = metrics_history_request(start, end, max_points)
        plane = resolve_control_plane(control_plane, session, region)
        info = await asyncio.to_thread(plane.get_microvm, validated_id)
        ensure_history_readable(info)
        timeout = DEFAULT_REQUEST_TIMEOUT_SECONDS if request_timeout is None else request_timeout
        try:
            response = await call_dedicated_health_async(
                plane,
                info,
                transport or TransportSettings(),
                token,
                lambda stub: stub.MetricsHistory(request, timeout=timeout),
            )
        except SandboxException as exc:
            if is_history_unimplemented(exc):
                raise history_unimplemented_error(exc, CLASS_HISTORY_FEATURE) from exc
            raise
        return metrics_history_from_proto(response)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._deadline_trigger.cancel()
        self._notify_resumed()
        await self._filesystem._stop_watches()
        await self._refresher.stop()
        await self._channel.close(grace=None)
        if self._stream_channel is not None:
            await self._stream_channel.close(grace=None)

    # ----------------------------------------------------------------- network

    @class_method_variant("_class_update_network")
    async def update_network(
        self,
        network: NetworkPolicy | NetworkOptions | None = None,
        *,
        allow_internet_access: bool | None = None,
        request_timeout: float | None = None,
    ) -> NetworkState:
        """Misma semántica que `Sandbox.update_network`: sustituye la política
        entera (`None` o `{}` es sin restricciones) y afecta a las conexiones
        nuevas."""
        policy = update_policy(network, allow_internet_access)
        return await self._send_update_network(
            policy, feature=UPDATE_NETWORK_FEATURE, request_timeout=request_timeout
        )

    @classmethod
    async def _class_update_network(
        cls,
        sandbox_id: str,
        network: NetworkPolicy | NetworkOptions | None = None,
        *,
        allow_internet_access: bool | None = None,
        access_token: str | None = None,
        region: str | None = None,
        session: boto3.session.Session | None = None,
        control_plane: ControlPlane | None = None,
        transport: TransportSettings | None = None,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
    ) -> NetworkState:
        """Misma semántica que `Sandbox.update_network(sandbox_id, network)`:
        `connect()` → `update_network()` → `close()`, nunca `kill()`."""
        policy = update_policy(network, allow_internet_access)
        sandbox = await cls._class_connect(
            sandbox_id,
            access_token=access_token,
            region=region,
            session=session,
            request_timeout=request_timeout,
            control_plane=control_plane,
            transport=transport,
        )
        try:
            return await sandbox._send_update_network(
                policy, feature=UPDATE_NETWORK_FEATURE, request_timeout=request_timeout
            )
        finally:
            await sandbox.close()

    async def get_network(self, *, request_timeout: float | None = None) -> NetworkState:
        """Misma semántica que `Sandbox.get_network`."""
        timeout = self._resolve_request_timeout(request_timeout)
        try:
            response = await self._call_unary(
                lambda: self._network_stub.GetNetwork(
                    network_pb2.GetNetworkRequest(), timeout=timeout
                )
            )
        except grpc.RpcError as exc:
            raise network_rpc_error(exc, feature=GET_NETWORK_FEATURE) from exc
        return state_from_proto(response)

    # ------------------------------------------------------------ sub-clients

    @property
    def commands(self) -> AsyncCommands:
        return self._commands

    @property
    def files(self) -> AsyncFilesystem:
        return self._filesystem

    @property
    def pty(self) -> AsyncPty:
        return self._pty

    @property
    def git(self) -> AsyncGit:
        """El módulo git de E2B 2.x (`clone`, `status`, `commit`, `push`...)
        sobre `commands.run`; se construye en el primer uso."""
        if self._git is None:
            self._git = AsyncGit(self._commands)
        return self._git

    # -------------------------------------------------------------------- code

    async def run_code(
        self,
        code: str,
        *,
        language: str | None = None,
        context: ContextLike | None = None,
        on_stdout: StdoutCallback | None = None,
        on_stderr: StdoutCallback | None = None,
        on_result: ResultCallback | None = None,
        on_error: ErrorCallback | None = None,
        envs: Mapping[str, str] | None = None,
        timeout: float | None = DEFAULT_CODE_TIMEOUT_SECONDS,
        request_timeout: float | None = None,
    ) -> Execution:
        """Misma semántica que `Sandbox.run_code`; los callbacks son síncronos."""
        return await self._code_client.run_code(
            code,
            language=language,
            context=context,
            on_stdout=on_stdout,
            on_stderr=on_stderr,
            on_result=on_result,
            on_error=on_error,
            envs=envs,
            timeout=timeout,
            request_timeout=request_timeout,
        )

    # ------------------------------------------------------------ persistence

    async def checkpoint_files(
        self,
        *,
        target: S3Prefix | None = None,
        exclude: Sequence[str] = (),
        timeout: float = DEFAULT_PERSIST_TIMEOUT_SECONDS,
        on_progress: CheckpointProgressCallback | None = None,
        user: str | None = None,
    ) -> CheckpointResult:
        """Misma semántica que `Sandbox.checkpoint_files`."""
        return await self._persistence.checkpoint(
            target=target,
            bound=self._persist,
            exclude=exclude,
            timeout=timeout,
            on_progress=on_progress,
            user=user,
        )

    async def restore_files(
        self,
        *,
        source: S3Prefix | None = None,
        timeout: float = DEFAULT_PERSIST_TIMEOUT_SECONDS,
        on_progress: RestoreProgressCallback | None = None,
        user: str | None = None,
    ) -> RestoreResult:
        """Misma semántica que `Sandbox.restore_files`."""
        return await self._persistence.restore(
            source=source, bound=self._persist, timeout=timeout, on_progress=on_progress, user=user
        )

    async def reincarnate(
        self,
        *,
        exclude: Sequence[str] = (),
        persist_timeout: float = DEFAULT_PERSIST_TIMEOUT_SECONDS,
    ) -> Self:
        """Misma semántica que `Sandbox.reincarnate`: checkpoint → `create(persist=)`
        con las mismas opciones (restaura) → `kill()` de este sandbox."""
        options = self._launch_options
        if options is None:
            raise reincarnate_requires_create_error()
        persist = self._persist
        if persist is None:
            raise reincarnate_requires_persist_error()
        await self.checkpoint_files(exclude=exclude, timeout=persist_timeout)
        try:
            successor = await type(self).create(
                **launch_kwargs(options), persist=persist, persist_timeout=persist_timeout
            )
        except BaseException as exc:
            add_reincarnate_note(exc, persist.uri)
            raise
        try:
            await self.kill()
        except SandboxNotFoundException:
            self._logger.info("sandbox %s ya no existía al reencarnar", self.sandbox_id)
        return successor

    async def _bind_and_restore(
        self, persist: S3Prefix, persist_timeout: float, *, terminate_on_failure: bool
    ) -> None:
        self._persist = bind_persist(persist, self.sandbox_id)
        if not should_auto_restore(persist):
            return
        try:
            self._last_restore = await self.restore_files(timeout=persist_timeout)
        except NotFoundException:
            self._logger.info(
                "sandbox %s: sin checkpoint en %s", self.sandbox_id, self._persist.uri
            )
            self._last_restore = None
        except BaseException:
            await self.close()
            if terminate_on_failure:
                await asyncio.to_thread(
                    terminate_quietly, self._control_plane, self.sandbox_id, self._logger
                )
            raise

    async def _apply_initial_network(self, launch: NetworkLaunch) -> None:
        """Misma compuerta que `Sandbox._apply_initial_network`: cualquier
        fallo cierra el cliente y termina el VM aunque haya `keep_on_failure`."""
        try:
            await self._enforce_launch_policy(launch)
        except BaseException:
            await self.close()
            await asyncio.to_thread(
                terminate_quietly, self._control_plane, self.sandbox_id, self._logger
            )
            raise

    async def _enforce_launch_policy(self, launch: NetworkLaunch) -> None:
        reported = readiness_enforcement(self._readiness_health)
        refused = egress_gate_error(self.sandbox_id, reported, launch.feature)
        if refused is not None:
            raise refused
        state = await self._send_update_network(
            launch.policy, feature=launch.feature, request_timeout=None
        )
        refused = egress_gate_error(self.sandbox_id, state.enforcement, launch.feature)
        if refused is not None:
            raise refused

    async def _send_update_network(
        self, policy: NetworkPolicy, *, feature: str, request_timeout: float | None
    ) -> NetworkState:
        timeout = self._resolve_request_timeout(request_timeout)
        request = network_pb2.UpdateNetworkRequest(policy=policy_to_proto(policy))
        try:
            response = await self._call_unary(
                lambda: self._network_stub.UpdateNetwork(request, timeout=timeout)
            )
        except grpc.RpcError as exc:
            raise network_rpc_error(exc, feature=feature) from exc
        return state_from_proto(response)

    async def create_code_context(
        self,
        *,
        cwd: str | None = None,
        language: str | None = None,
        envs: Mapping[str, str] | None = None,
        request_timeout: float | None = None,
    ) -> CodeContext:
        """Misma semántica que `Sandbox.create_code_context`, lenguajes y
        variante de imagen incluidos."""
        return await self._code_client.create_context(
            cwd=cwd, language=language, envs=envs, request_timeout=request_timeout
        )

    async def list_code_contexts(
        self, *, request_timeout: float | None = None
    ) -> builtins.list[CodeContext]:
        return await self._code_client.list_contexts(request_timeout=request_timeout)

    async def remove_code_context(
        self, context: ContextLike, *, request_timeout: float | None = None
    ) -> None:
        await self._code_client.remove_context(context, request_timeout=request_timeout)

    async def restart_code_context(
        self, context: ContextLike, *, request_timeout: float | None = None
    ) -> None:
        await self._code_client.restart_context(context, request_timeout=request_timeout)

    # ------------------------------------------------------------- internals

    def _bind_logger(self, logger: logging.Logger | None) -> None:
        """El `logger=` de `create(pool=...)`: la plaza se abrió sin él, así
        que se reenruta aquí, incluido el refresco del JWE."""
        if logger is None:
            return
        self._custom_logger = logger
        self._logger = logger
        self._refresher.route_logs_to(logger)

    def _logger_or(self, fallback: logging.Logger) -> logging.Logger:
        """El logger de un sub-cliente (`commands`, `files`, `pty`, código,
        persistencia, transferencias): el del usuario si lo dio, el del
        módulo del sub-cliente si no."""
        return fallback if self._custom_logger is None else self._custom_logger

    def _bind_transfer(
        self, staging: S3Staging | None, session: boto3.session.Session | None
    ) -> None:
        """Misma semántica que `Sandbox._bind_transfer`."""
        self._transfer = staging
        self._session = session or control_plane_session(self._control_plane)

    def _current_jwe(self, port: int) -> str:
        jwe = self._refresher.store.jwe_for(port)
        if jwe is None:
            raise SandboxException(
                f"no hay token del proxy para el puerto {port}; llama a get_host"
            )
        return jwe

    async def _call_unary_once(self, call: Callable[[], Awaitable[T]]) -> T:
        try:
            return await call()
        except grpc.RpcError as exc:
            if not is_proxy_forbidden(exc):
                raise
        self._logger.info("el proxy rechazó el token del sandbox %s; reacuñando", self.sandbox_id)
        await self._refresher.refresh_all()
        return await call()

    async def _call_unary(self, call: Callable[[], Awaitable[T]], *, reopen: bool = True) -> T:
        """Misma política que `Sandbox._call_unary`: reintento del 403 y, tras
        un corte reconectable, una reconexión y un reintento; `reopen=False`
        es el `SetTimeout` de la propia reapertura tras la pausa del plazo."""
        seen_generation = self._resume_generation
        seen_reopens = self._reopens
        try:
            return await self._call_unary_once(call)
        except grpc.RpcError as exc:
            if reopen and await self._reopened_after_deadline_pause(exc, seen_reopens):
                return await self._call_unary_once(call)
            if not self._is_reconnectable(exc):
                raise
            reason = exc
        outcome = await self._reconnect(reason, seen_generation)
        if not outcome.resumed:
            raise self._reconnect_error(outcome, reason) from reason
        return await call()

    async def _translated_unary(
        self, call: Callable[[], Awaitable[T]], *, filesystem: bool = False
    ) -> T:
        try:
            return await self._call_unary(call)
        except grpc.RpcError as exc:
            raise translate_rpc_error(exc, filesystem=filesystem) from exc

    def _resolve_request_timeout(self, request_timeout: float | None) -> float:
        return self._request_timeout if request_timeout is None else request_timeout

    async def _process_call(
        self, invoke: Callable[[Any, float], Awaitable[T]], request_timeout: float | None
    ) -> T:
        timeout = self._resolve_request_timeout(request_timeout)
        return await self._translated_unary(lambda: invoke(self._process, timeout))

    async def _pty_call(
        self, invoke: Callable[[Any, float], Awaitable[T]], request_timeout: float | None
    ) -> T:
        timeout = self._resolve_request_timeout(request_timeout)
        return await self._translated_unary(lambda: invoke(self._pty_stub, timeout))

    async def _files_call(
        self, invoke: Callable[[Any, float], Awaitable[T]], request_timeout: float | None
    ) -> T:
        timeout = self._resolve_request_timeout(request_timeout)
        return await self._translated_unary(lambda: invoke(self._files, timeout), filesystem=True)

    async def _code_call(
        self,
        invoke: Callable[[Any, float], Awaitable[T]],
        request_timeout: float | None,
        *,
        default_timeout: float | None = None,
    ) -> T:
        timeout = self._resolve_request_timeout(request_timeout)
        if request_timeout is None and default_timeout is not None:
            timeout = default_timeout
        return await self._translated_unary(lambda: invoke(self._code, timeout))

    def _stub(self, service: StubFactory, *, stream: bool) -> Any:
        if not stream:
            return self._unary_stubs[service]
        if self._stream_channel is None:
            self._stream_channel = self._transport.open_aio_channel(self.endpoint, self._plugin)
        stub = self._stream_stubs.get(service)
        if stub is None:
            stub = service(self._stream_channel)
            self._stream_stubs[service] = stub
        return stub

    async def _open_stream(
        self,
        start: StreamStarter,
        *,
        service: StubFactory,
        stream: bool,
        allow_empty: bool = False,
        filesystem: bool = False,
        reconnect: bool = True,
        translate: Callable[[grpc.RpcError], Exception] | None = None,
    ) -> tuple[Any, Any]:
        """Misma política que `Sandbox._open_stream`: un 403 del proxy antes
        del primer mensaje se reintenta una vez tras reacuñar; un corte
        reconectable espera la reconexión y reintenta una vez (salvo
        `reconnect=False`); `translate` sustituye la tabla unaria para un
        status que no es un corte (persistencia)."""
        stub = self._stub(service, stream=stream)
        seen_generation = self._resume_generation
        seen_reopens = self._reopens
        try:
            return await self._first_message_reminting(start, stub, allow_empty)
        except grpc.RpcError as exc:
            reason = exc
        if not await self._reopened_after_deadline_pause(reason, seen_reopens):
            if not (reconnect and self._is_reconnectable(reason)):
                raise await self._open_failure(reason, filesystem, translate) from reason
            outcome = await self._reconnect(reason, seen_generation)
            if not outcome.resumed:
                raise self._reconnect_error(outcome, reason) from reason
        try:
            return await first_stream_message_async(start(stub), allow_empty=allow_empty)
        except grpc.RpcError as exc:
            raise await self._open_failure(exc, filesystem, translate) from exc

    async def _open_failure(
        self,
        exc: grpc.RpcError,
        filesystem: bool,
        translate: Callable[[grpc.RpcError], Exception] | None,
    ) -> Exception:
        if translate is not None and not is_stream_reset(exc):
            return translate(exc)
        return await self._stream_failure(exc, filesystem=filesystem)

    async def _first_message_reminting(
        self, start: StreamStarter, stub: Any, allow_empty: bool
    ) -> tuple[Any, Any]:
        try:
            return await first_stream_message_async(start(stub), allow_empty=allow_empty)
        except grpc.RpcError as exc:
            if not is_proxy_forbidden(exc):
                raise
        self._logger.info(
            "el proxy rechazó el token del sandbox %s al abrir un stream; reacuñando",
            self.sandbox_id,
        )
        await self._refresher.refresh_all()
        return await first_stream_message_async(start(stub), allow_empty=allow_empty)

    async def _stream_failure(self, exc: grpc.RpcError, *, filesystem: bool = False) -> Exception:
        if not is_stream_reset(exc):
            return translate_rpc_error(exc, filesystem=filesystem)
        health_ok = await self._health_answers()
        state = None if health_ok else await self._state_after_reset()
        return stream_failure_exception(exc, health_ok=health_ok, state=state)

    async def _health_answers(self) -> bool:
        try:
            return await self._probe_health(STREAM_PROBE_TIMEOUT_SECONDS) is not None
        except Exception:
            self._logger.debug("Health no respondió tras un corte de stream", exc_info=True)
            return False

    async def _state_after_reset(self) -> str | None:
        try:
            self._info = await asyncio.to_thread(self._control_plane.get_microvm, self.sandbox_id)
        except SandboxNotFoundException:
            return "TERMINATED"
        except SandboxException:
            return None
        return self._info.state

    async def _probe_health(self, timeout: float) -> health_pb2.HealthResponse | None:
        try:
            response = await self._call_unary_once(
                lambda: self._health.Health(health_pb2.HealthRequest(), timeout=timeout)
            )
            return cast("health_pb2.HealthResponse", response)
        except grpc.RpcError as exc:
            if is_not_yet_reachable(exc):
                self._logger.debug(
                    "sandbox %s: Health aún no alcanzable (%s)", self.sandbox_id, rpc_status(exc)
                )
                return None
            raise translate_rpc_error(exc) from exc

    async def _wait_until_ready(
        self, *, terminate_on_failure: bool, readiness: type[ReadinessPoll] = ReadinessPoll
    ) -> health_pb2.HealthResponse:
        poll = readiness(timeout=self._ready_timeout)
        while True:
            response = await self._probe_health(poll.rpc_timeout())
            if response is not None and health_ready(response):
                self._record_health(response)
                self._readiness_health = health_from_proto(response)
                return response
            if poll.should_check_state():
                await self._fail_if_terminal()
            if poll.timed_out():
                raise await self._not_ready(terminate_on_failure)
            await asyncio.sleep(poll.next_delay())

    def _record_health(self, response: health_pb2.HealthResponse) -> None:
        self._metadata = metadata_from_health(response)
        self._guest = guest_facts_from_health(response)
        if self._ready_uptime_ms is None:
            self._ready_uptime_ms = int(response.uptime_ms)
        self._warn_hardening(response)
        self._record_lifecycle(lifecycle_from_proto(response))
        generation = int(response.resume_generation)
        if generation == self._resume_generation:
            return
        self._resume_generation = generation
        self._paused = False
        self._notify_resumed()
        if response.kernel_state_lost:
            self._logger.warning(
                "sandbox %s: un kernel perdió su estado en el resume %s",
                self.sandbox_id,
                generation,
            )
        offset = int(response.clock_offset_ms)
        if abs(offset) > CLOCK_OFFSET_WARN_MS:
            self._logger.warning(
                "sandbox %s: desfase de reloj de %s ms tras el resume %s",
                self.sandbox_id,
                offset,
                generation,
            )

    def _warn_hardening(self, response: health_pb2.HealthResponse) -> None:
        """Una vez por generación cuando el agente contó hooks anómalos (alguien
        posee un token `allPorts` de la VM) y una sola vez cuando el sandbox
        tiene execution role y, pasado el presupuesto de verificación de
        `rayd` (`imds_open_warning_due`), IMDS sigue sin bloquear para uid
        1000."""
        generation = int(response.resume_generation)
        anomalies = int(response.hook_anomalies)
        if anomalies > 0 and self._anomalies_warned_generation != generation:
            self._anomalies_warned_generation = generation
            self._logger.warning(HOOK_ANOMALIES_WARNING, self.sandbox_id, anomalies)
        if not self._imds_warned and imds_open_warning_due(
            response,
            execution_role_arn=self._info.execution_role_arn,
            ready_uptime_ms=self._ready_uptime_ms,
        ):
            self._imds_warned = True
            self._logger.warning(IMDS_OPEN_WARNING, self.sandbox_id)

    def _record_lifecycle(self, lifecycle: SandboxLifecycle | None) -> None:
        self._lifecycle = lifecycle
        self._deadline_trigger.arm(pause_trigger_delay(lifecycle, unix_ms_now()))

    async def _refresh_health(self) -> None:
        response = await self._probe_health(
            min(ReadinessPoll.MAX_RPC_TIMEOUT, self._request_timeout)
        )
        if response is not None:
            self._record_health(response)

    async def _send_set_timeout(
        self, request: TimeoutRequest, *, request_timeout: float | None, reopen: bool = True
    ) -> None:
        timeout = self._resolve_request_timeout(request_timeout)
        try:
            state = await self._call_unary(
                lambda: self._lifecycle_stub.SetTimeout(request.to_proto(), timeout=timeout),
                reopen=reopen,
            )
        except grpc.RpcError as exc:
            raise translate_set_timeout_error(exc, request) from exc
        self._record_lifecycle(lifecycle_from_state(state))

    async def _extend_after_readiness(
        self, requested: int | None, *, request_timeout: float | None
    ) -> None:
        request = connect_extension(self._lifecycle, requested, unix_ms_now())
        if request is not None:
            await self._send_set_timeout(request, request_timeout=request_timeout)

    async def _on_deadline(self) -> None:
        """Misma política que `Sandbox._on_deadline`: fallos registrados y
        tragados, nunca un `Health` a un sandbox que no está `RUNNING`."""
        try:
            await self._suspend_if_expired()
        except Exception as exc:
            self._logger.warning(
                "sandbox %s: el disparador del plazo falló (%s); la política de idle de la "
                "plataforma lo suspenderá",
                self.sandbox_id,
                type(exc).__name__,
            )

    async def _suspend_if_expired(self) -> None:
        if self._closed:
            return
        info = await asyncio.to_thread(self._control_plane.get_microvm, self.sandbox_id)
        if info.state != "RUNNING":
            return
        response = await self._call_unary_once(
            lambda: self._health.Health(
                health_pb2.HealthRequest(), timeout=ReadinessPoll.MAX_RPC_TIMEOUT
            )
        )
        self._record_health(response)
        if self._lifecycle is not None and self._lifecycle.phase == "expired":
            await self._suspend_for_deadline()

    async def _suspend_for_deadline(self) -> None:
        generation = self._resume_generation
        suspended = await asyncio.to_thread(self._control_plane.suspend_microvm, self.sandbox_id)
        if suspended:
            self._deadline_pause_generation = generation
        self._logger.info(
            "sandbox %s: plazo lógico vencido en modo pause; suspend-microvm %s",
            self.sandbox_id,
            "aceptado" if suspended else "ya no aplicaba",
        )

    async def _reopened_after_deadline_pause(self, exc: grpc.RpcError, seen_reopens: int) -> bool:
        """Misma regla que `Sandbox._reopened_after_deadline_pause`: el primer
        `sandbox_timeout` tras una suspensión por el plazo de este cliente,
        ya reanudado, reabre con `SetTimeout` (`auto_resume_reopen`). Los
        callers concurrentes esperan la misma tarea (`asyncio.shield`: la
        cancelación de uno no la corta para los demás) y quien llega tras
        una reapertura hecha desde que empezó su llamada reintenta sin otro
        `SetTimeout`."""
        if self._closed or not is_sandbox_timeout(exc):
            return False
        if self._reopens > seen_reopens:
            return True
        task = self._reopen_task
        if task is None:
            paused_generation = self._deadline_pause_generation
            if paused_generation is None:
                return False
            task = asyncio.ensure_future(self._reopen_after_deadline_pause(paused_generation))
            self._reopen_task = task
            task.add_done_callback(self._forget_reopen_task)
        return await asyncio.shield(task)

    def _forget_reopen_task(self, task: asyncio.Task[bool]) -> None:
        if self._reopen_task is task:
            self._reopen_task = None

    async def _reopen_after_deadline_pause(self, paused_generation: int) -> bool:
        """El cuerpo de la reapertura; la marca sólo se consume cuando la
        `resume_generation` ya avanzó (un `sandbox_timeout` anterior a la
        congelación no la gasta) y el `SetTimeout` respondió."""
        try:
            await self._refresh_health()
            if self._resume_generation <= paused_generation:
                return False
            request = auto_resume_reopen(
                self._lifecycle,
                paused_generation=paused_generation,
                generation=self._resume_generation,
                now_ms=unix_ms_now(),
            )
            if request is None:
                self._consume_deadline_pause(paused_generation)
                return False
            await self._send_set_timeout(request, request_timeout=None, reopen=False)
        except (grpc.RpcError, SandboxException) as failure:
            self._logger.warning(
                "sandbox %s: no se pudo reabrir tras la pausa del plazo (%s)",
                self.sandbox_id,
                type(failure).__name__,
            )
            return False
        self._consume_deadline_pause(paused_generation)
        self._reopens += 1
        self._logger.info(
            "sandbox %s: reanudado tras la pausa del plazo; plazo reabierto a %s s",
            self.sandbox_id,
            request.seconds,
        )
        return True

    def _consume_deadline_pause(self, paused_generation: int) -> None:
        if self._deadline_pause_generation == paused_generation:
            self._deadline_pause_generation = None

    def _is_reconnectable(self, exc: grpc.RpcError) -> bool:
        return not self._closed and is_reconnectable(exc)

    def _reconnect_error(self, outcome: ReconnectOutcome, reason: Exception) -> Exception:
        return reason if outcome.error is None else outcome.error

    async def _suspend_marking_paused(self) -> bool:
        was_paused = self._paused
        self._paused = True
        try:
            suspended = await asyncio.to_thread(
                self._control_plane.suspend_microvm, self.sandbox_id
            )
        except BaseException:
            self._paused = was_paused
            raise
        if not suspended:
            self._paused = was_paused
        return suspended

    def _foreground_stream_wakes(self) -> bool:
        return not self._paused

    async def _reconnect(
        self, reason: Exception, seen_generation: int, *, wake: bool = True
    ) -> ReconnectOutcome:
        """Misma política que `Sandbox._reconnect`: fase dormida fuera del
        lock sin `wake`, sondeo de `Health` bajo `asyncio.Lock`, y un
        `asyncio.Event` que `_record_health` y `close()` disparan."""
        while True:
            outcome = self._already_back(seen_generation)
            if outcome is None and not wake:
                outcome = await self._sleep_while_suspended(reason, seen_generation)
            if outcome is None:
                outcome = await self._poll_holding_the_lock(reason, seen_generation, wake)
            if outcome is not None:
                return outcome

    def _already_back(self, seen_generation: int) -> ReconnectOutcome | None:
        if self._resume_generation > seen_generation:
            return ReconnectOutcome(True, True, self._resume_generation)
        if self._closed:
            return self._failed_reconnect(closed_during_reconnect(self.sandbox_id))
        return None

    async def _sleep_while_suspended(
        self, reason: Exception, seen_generation: int
    ) -> ReconnectOutcome | None:
        await self._wait_for_resume(ReconnectPoll.INITIAL_DELAY)
        while True:
            outcome = self._already_back(seen_generation)
            if outcome is not None:
                return outcome
            failure = await self._check_state(reason, wake=False)
            if failure is not None:
                return self._failed_reconnect(failure)
            if self._info.state not in SUSPENDED_STATES:
                return None
            await self._wait_for_resume(ReconnectPoll.STATE_CHECK_INTERVAL)

    async def _poll_holding_the_lock(
        self, reason: Exception, seen_generation: int, wake: bool
    ) -> ReconnectOutcome | None:
        async with self._reconnect_lock:
            outcome = self._already_back(seen_generation)
            if outcome is not None:
                return outcome
            self._logger.info(
                "sandbox %s: stream/unario cortado (%s); esperando al agente hasta %g s",
                self.sandbox_id,
                type(reason).__name__,
                self._reconnect_timeout,
            )
            return await self._poll_until_back(reason, seen_generation, wake)

    async def _poll_until_back(
        self, reason: Exception, seen_generation: int, wake: bool
    ) -> ReconnectOutcome | None:
        poll = ReconnectPoll(timeout=self._reconnect_timeout)
        suspending = is_suspending_reason(reason)
        state_read_after_cut = not wake
        while True:
            outcome = self._already_back(seen_generation)
            if outcome is not None:
                return outcome
            if poll.timed_out():
                return self._failed_reconnect(self._reconnect_timed_out(reason))
            if wake and poll.should_check_state():
                failure = await self._check_state(reason, wake=True)
                if failure is not None:
                    return self._failed_reconnect(failure)
                state_read_after_cut = True
            try:
                response = await self._probe_health(poll.rpc_timeout())
            except Exception as exc:
                return self._failed_reconnect(self._probe_failure(exc))
            running = state_read_after_cut and self._info.state == "RUNNING"
            if health_reconnected(
                response, seen_generation=seen_generation, suspending=suspending, running=running
            ):
                return self._reconnected(response, seen_generation, poll.elapsed())
            if not wake:
                failure = await self._check_state(reason, wake=False)
                if failure is not None:
                    return self._failed_reconnect(failure)
                if self._info.state in SUSPENDED_STATES:
                    return None
            await self._wait_for_resume(poll.next_delay())

    async def _wait_for_resume(self, timeout: float) -> None:
        """Duerme como mucho `timeout`; una generación nueva o `close()` lo
        interrumpen. El event se reemplaza al dispararse, así cada espera
        ve sólo los avisos posteriores a su inicio."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._resumed.wait(), timeout)

    def _notify_resumed(self) -> None:
        self._resumed.set()
        self._resumed = asyncio.Event()

    def _probe_failure(self, exc: Exception) -> Exception:
        return closed_during_reconnect(self.sandbox_id) if self._closed else exc

    async def _check_state(self, reason: Exception, *, wake: bool) -> Exception | None:
        try:
            self._info = await asyncio.to_thread(self._control_plane.get_microvm, self.sandbox_id)
        except SandboxNotFoundException as exc:
            return exc
        except SandboxException:
            self._logger.debug("get-microvm falló durante la reconexión", exc_info=True)
            return None
        return reconnect_failure(reason, info=self._info, wake=wake)

    def _reconnect_timed_out(self, reason: Exception) -> Exception:
        failure = reconnect_failure(reason, timeout=self._reconnect_timeout)
        return reason if failure is None else failure

    def _reconnected(self, response: Any, seen_generation: int, elapsed: float) -> ReconnectOutcome:
        self._record_health(response)
        generation = int(response.resume_generation)
        self._logger.info(
            "sandbox %s: reconectado en %.1f s (resume_generation %s -> %s)",
            self.sandbox_id,
            elapsed,
            seen_generation,
            generation,
        )
        return ReconnectOutcome(True, generation != seen_generation, generation)

    def _failed_reconnect(self, error: Exception) -> ReconnectOutcome:
        self._logger.warning("sandbox %s: reconexión fallida: %s", self.sandbox_id, error)
        return ReconnectOutcome(False, False, self._resume_generation, error)

    async def _fail_if_terminal(self) -> None:
        self._info = await asyncio.to_thread(self._control_plane.get_microvm, self.sandbox_id)
        if self._info.state in TERMINAL_STATES:
            raise terminated_during_boot_error(self._info)

    async def _not_ready(self, terminate: bool) -> SandboxException:
        info: SandboxInfo | None
        try:
            info = await asyncio.to_thread(self._control_plane.get_microvm, self.sandbox_id)
        except SandboxException:
            info = None
        terminated = False
        if terminate and (info is None or info.state not in TERMINAL_STATES):
            await asyncio.to_thread(self._control_plane.terminate_microvm, self.sandbox_id)
            terminated = True
        return not_ready_error(info, ready_timeout=self._ready_timeout, terminated=terminated)

    # --------------------------------------------------------- context manager

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.kill()

    def __repr__(self) -> str:
        return f"AsyncSandbox(sandbox_id={self.sandbox_id!r}, state={self._info.state!r})"
