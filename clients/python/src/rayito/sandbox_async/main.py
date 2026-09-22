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
from types import TracebackType
from typing import TYPE_CHECKING, Any, Self, TypeVar, cast

import boto3
import grpc
import grpc.aio

from rayito._aws import ControlPlane, PortSpec
from rayito._code_base import (
    DEFAULT_CODE_TIMEOUT_SECONDS,
    ContextLike,
    ErrorCallback,
    ResultCallback,
    StdoutCallback,
)
from rayito._limits import (
    DEFAULT_PERSIST_TIMEOUT_SECONDS,
    DEFAULT_PORT,
    SUSPENDED_STATES,
    TERMINAL_STATES,
)
from rayito._models import (
    CheckpointResult,
    CodeContext,
    Execution,
    HostAccess,
    IdlePolicy,
    LaunchOptions,
    RestoreResult,
    S3Prefix,
    SandboxHealth,
    SandboxInfo,
    SandboxListItem,
    SandboxMetrics,
)
from rayito._payload import validated_metadata
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
    LoggingOption,
    PortLike,
    ReadinessPoll,
    ReconnectOutcome,
    ReconnectPoll,
    already_suspended,
    build_launch_plan,
    class_method_variant,
    health_from_proto,
    health_reconnected,
    imds_open_warning_due,
    is_suspending_reason,
    list_states_for_metadata,
    metadata_from_health,
    metadata_matches,
    metadata_probe_failure,
    not_ready_error,
    reconnect_failure,
    require_access_token,
    resolve_template,
    terminal_state_error,
    terminated_during_boot_error,
    validate_host_port,
    validate_sandbox_id,
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
from rayito.sandbox_async.persistence import AsyncPersistenceClient
from rayito.sandbox_async.pty import AsyncPty
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
    plugin = ProxyAuthPlugin(refresher.store, port=DEFAULT_PORT, access_token=None)
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
    ) -> None:
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
        self._paused = False
        self._anomalies_warned_generation: int | None = None
        self._imds_warned = False
        self._ready_uptime_ms: int | None = None
        self._reconnect_lock = asyncio.Lock()
        self._resumed = asyncio.Event()
        self._plugin = ProxyAuthPlugin(
            refresher.store, port=DEFAULT_PORT, access_token=access_token
        )
        self._channel = transport.open_aio_channel(info.endpoint, self._plugin)
        self._stream_channel: grpc.aio.Channel | None = None
        self._health = health_pb2_grpc.HealthServiceStub(self._channel)
        self._process = process_pb2_grpc.ProcessServiceStub(self._channel)
        self._files = filesystem_pb2_grpc.FilesystemServiceStub(self._channel)
        self._code = code_pb2_grpc.CodeServiceStub(self._channel)
        self._pty_stub = pty_pb2_grpc.PtyServiceStub(self._channel)
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

    # ------------------------------------------------------------------ create

    @classmethod
    async def create(
        cls,
        template: str | None = None,
        *,
        template_version: str | None = None,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
        idle: IdlePolicy | None = DEFAULT_IDLE_POLICY,
        envs: Mapping[str, str] | None = None,
        metadata: Mapping[str, str] | None = None,
        cpu_time_limit: int | None = None,
        execution_role_arn: str | None = None,
        allowed_ports: Sequence[PortLike] | None = None,
        ingress: Sequence[str] | None = None,
        egress: Sequence[str] | None = None,
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
    ) -> Self:
        """Misma semántica que `Sandbox.create` (incluidos `metadata`, `pool=` y
        `persist=`)."""
        require_role_for_persist(persist, execution_role_arn)
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
                    "idle": idle,
                    "envs": envs,
                    "metadata": metadata,
                    "cpu_time_limit": cpu_time_limit,
                    "execution_role_arn": execution_role_arn,
                    "allowed_ports": allowed_ports,
                    "ingress": ingress,
                    "egress": egress,
                    "logging": logging,
                    "region": region,
                    "session": session,
                    "access_token": access_token,
                    "keep_on_failure": keep_on_failure,
                    "control_plane": control_plane,
                    "transport": transport,
                }
            )
            return cast(
                "Self",
                await pool.take(
                    ready_timeout=ready_timeout,
                    request_timeout=request_timeout,
                    reconnect_timeout=reconnect_timeout,
                ),
            )
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
        )
        info = await asyncio.to_thread(plane.run_microvm, plan.request)
        logger.info("run-microvm aceptado: %s (%s)", info.sandbox_id, info.state)
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
        )
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
        )
        if persist is not None:
            await sandbox._bind_and_restore(
                persist, persist_timeout, terminate_on_failure=not keep_on_failure
            )
        return sandbox

    @classmethod
    async def connect(
        cls,
        sandbox_id: str,
        *,
        access_token: str | None = None,
        region: str | None = None,
        session: boto3.session.Session | None = None,
        ready_timeout: float = DEFAULT_READY_TIMEOUT_SECONDS,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
        reconnect_timeout: float = DEFAULT_RECONNECT_TIMEOUT_SECONDS,
        control_plane: ControlPlane | None = None,
        transport: TransportSettings | None = None,
        persist: S3Prefix | None = None,
    ) -> Self:
        """Misma semántica que `Sandbox.connect` (incluido `persist=`, que sólo
        enlaza el prefijo con `name`)."""
        token = require_access_token(access_token)
        plane = resolve_control_plane(control_plane, session, region)
        bound = None if persist is None else require_named_persist(persist)
        info = await asyncio.to_thread(plane.get_microvm, validate_sandbox_id(sandbox_id))
        if info.state in TERMINAL_STATES:
            raise terminal_state_error(info)
        if info.state == "SUSPENDED" and not (info.idle and info.idle.auto_resume):
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
        )
        sandbox._persist = bound
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
    ) -> Self:
        """Misma política de limpieza que `Sandbox._open`: con
        `terminate_on_failure`, todo fallo previo al primer `agent_ready` que no
        sea `SandboxNotReadyException` termina el MicroVM. `readiness` es el
        calendario del sondeo (`TakePoll` desde el pool)."""
        refresher = AsyncTokenRefresher(
            TokenRefresher(
                TokenStore(),
                lambda ports: control_plane.create_auth_token(info.sandbox_id, ports),
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
            )
            await sandbox._wait_until_ready(
                terminate_on_failure=terminate_on_failure, readiness=readiness
            )
        except BaseException as exc:
            if sandbox is not None:
                await sandbox.close()
            if terminate_on_failure and not isinstance(exc, SandboxNotReadyException):
                await asyncio.to_thread(terminate_quietly, control_plane, info.sandbox_id)
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
        region: str | None = None,
        session: boto3.session.Session | None = None,
        control_plane: ControlPlane | None = None,
        transport: TransportSettings | None = None,
        request_timeout: float = METADATA_PROBE_TIMEOUT_SECONDS,
    ) -> list[SandboxListItem]:
        """Misma semántica y mismo coste O(n) con `metadata` que `Sandbox.list`;
        las sondas de `Health` van por `grpc.aio`, una tras otra."""
        plane = resolve_control_plane(control_plane, session, region)
        wanted = None if metadata is None else validated_metadata(metadata)
        wanted_states = states if wanted is None else list_states_for_metadata(states)

        def collect() -> list[SandboxListItem]:
            image_arn = plane.resolve_template_arn(template) if template else None
            return list(
                plane.list_microvms(
                    image_arn=image_arn, image_version=template_version, states=wanted_states
                )
            )

        candidates = await asyncio.to_thread(collect)
        if wanted is None:
            return candidates
        return await cls._filter_by_metadata(
            plane, candidates, wanted, transport or TransportSettings(), request_timeout
        )

    @classmethod
    async def _filter_by_metadata(
        cls,
        plane: ControlPlane,
        candidates: Iterable[SandboxListItem],
        wanted: Mapping[str, str],
        transport: TransportSettings,
        request_timeout: float,
    ) -> builtins.list[SandboxListItem]:
        matched: builtins.list[SandboxListItem] = []
        for item in candidates:
            try:
                info = await asyncio.to_thread(plane.get_microvm, item.sandbox_id)
            except SandboxNotFoundException:
                continue
            read = await probe_metadata_async(plane, info, transport, request_timeout)
            if metadata_matches(read, wanted):
                matched.append(dataclasses.replace(item, metadata=read))
        return matched

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
        refreshed = await asyncio.to_thread(self._control_plane.get_microvm, self.sandbox_id)
        self._info = dataclasses.replace(refreshed, metadata=self.metadata)
        return self._info

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
        probed = await probe_metadata_async(
            plane, info, transport or TransportSettings(), request_timeout
        )
        return dataclasses.replace(info, metadata=probed)

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
        """Misma semántica que `Sandbox.resume`."""
        self._paused = False
        await asyncio.to_thread(self._control_plane.resume_microvm, self.sandbox_id)
        await self._refresher.refresh_all()
        if wait:
            await self._wait_until_ready(terminate_on_failure=False)

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

    async def is_running(self) -> bool:
        response = await self._probe_health(
            min(ReadinessPoll.MAX_RPC_TIMEOUT, self._request_timeout)
        )
        return response is not None and response.agent_ready

    async def get_health(self, *, request_timeout: float | None = None) -> SandboxHealth:
        timeout = self._resolve_request_timeout(request_timeout)
        response = await self._translated_unary(
            lambda: self._health.Health(health_pb2.HealthRequest(), timeout=timeout)
        )
        self._record_health(response)
        return health_from_proto(response)

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

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._notify_resumed()
        await self._filesystem._stop_watches()
        await self._refresher.stop()
        await self._channel.close(grace=None)
        if self._stream_channel is not None:
            await self._stream_channel.close(grace=None)

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
            logger.info("sandbox %s ya no existía al reencarnar", self.sandbox_id)
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
            logger.info("sandbox %s: sin checkpoint en %s", self.sandbox_id, self._persist.uri)
            self._last_restore = None
        except BaseException:
            await self.close()
            if terminate_on_failure:
                await asyncio.to_thread(terminate_quietly, self._control_plane, self.sandbox_id)
            raise

    async def create_code_context(
        self,
        *,
        cwd: str | None = None,
        language: str | None = None,
        envs: Mapping[str, str] | None = None,
        request_timeout: float | None = None,
    ) -> CodeContext:
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
        logger.info("el proxy rechazó el token del sandbox %s; reacuñando", self.sandbox_id)
        await self._refresher.refresh_all()
        return await call()

    async def _call_unary(self, call: Callable[[], Awaitable[T]]) -> T:
        """Misma política que `Sandbox._call_unary`: reintento del 403 y, tras
        un corte reconectable, una reconexión y un reintento."""
        seen_generation = self._resume_generation
        try:
            return await self._call_unary_once(call)
        except grpc.RpcError as exc:
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
        try:
            return await self._first_message_reminting(start, stub, allow_empty)
        except grpc.RpcError as exc:
            if not (reconnect and self._is_reconnectable(exc)):
                raise await self._open_failure(exc, filesystem, translate) from exc
            reason = exc
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
        logger.info(
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
            logger.debug("Health no respondió tras un corte de stream", exc_info=True)
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
                logger.debug(
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
            if response is not None and response.agent_ready and response.kernel_ready:
                self._record_health(response)
                return response
            if poll.should_check_state():
                await self._fail_if_terminal()
            if poll.timed_out():
                raise await self._not_ready(terminate_on_failure)
            await asyncio.sleep(poll.next_delay())

    def _record_health(self, response: health_pb2.HealthResponse) -> None:
        self._metadata = metadata_from_health(response)
        if self._ready_uptime_ms is None:
            self._ready_uptime_ms = int(response.uptime_ms)
        self._warn_hardening(response)
        generation = int(response.resume_generation)
        if generation == self._resume_generation:
            return
        self._resume_generation = generation
        self._paused = False
        self._notify_resumed()
        if response.kernel_state_lost:
            logger.warning(
                "sandbox %s: un kernel perdió su estado en el resume %s",
                self.sandbox_id,
                generation,
            )
        offset = int(response.clock_offset_ms)
        if abs(offset) > CLOCK_OFFSET_WARN_MS:
            logger.warning(
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
            logger.warning(HOOK_ANOMALIES_WARNING, self.sandbox_id, anomalies)
        if not self._imds_warned and imds_open_warning_due(
            response,
            execution_role_arn=self._info.execution_role_arn,
            ready_uptime_ms=self._ready_uptime_ms,
        ):
            self._imds_warned = True
            logger.warning(IMDS_OPEN_WARNING, self.sandbox_id)

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
            logger.info(
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
            logger.debug("get-microvm falló durante la reconexión", exc_info=True)
            return None
        return reconnect_failure(reason, info=self._info, wake=wake)

    def _reconnect_timed_out(self, reason: Exception) -> Exception:
        failure = reconnect_failure(reason, timeout=self._reconnect_timeout)
        return reason if failure is None else failure

    def _reconnected(self, response: Any, seen_generation: int, elapsed: float) -> ReconnectOutcome:
        self._record_health(response)
        generation = int(response.resume_generation)
        logger.info(
            "sandbox %s: reconectado en %.1f s (resume_generation %s -> %s)",
            self.sandbox_id,
            elapsed,
            seen_generation,
            generation,
        )
        return ReconnectOutcome(True, generation != seen_generation, generation)

    def _failed_reconnect(self, error: Exception) -> ReconnectOutcome:
        logger.warning("sandbox %s: reconexión fallida: %s", self.sandbox_id, error)
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
