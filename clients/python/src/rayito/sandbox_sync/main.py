"""`Sandbox` síncrono: plano de control por boto3, `HealthService`,
`ProcessService`, `FilesystemService`, `CodeService` y `PtyService` por `grpc`.

Alcance M5: ciclo de vida completo (`create`, `connect`, `kill`, `list`,
`get_info`, `is_running`, `get_host`, `pause`, `resume`, `get_health`),
`commands`, `files`, `pty`, `run_code` con sus contextos, `get_metrics` y el
contrato de reconexión: un stream cortado por un `/suspend` (o por el proxy)
sondea `Health` hasta que el agente vuelve y cada handle se reengancha por
su cuenta (`Connect(from_seq)`, `Pty.Connect`, `WatchDir` de nuevo,
`Reattach`); un unario cortado se reintenta una vez. Como máximo dos canales
HTTP/2 por sandbox: el de unarios (comandos en foreground, `Write`, `Read`,
`Execute`, `Reattach`) y, abierto perezosamente, el de streams largos
(background, `connect`, `watch_dir` y las PTY).
"""

from __future__ import annotations

import builtins
import dataclasses
import logging
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from datetime import datetime
from types import TracebackType
from typing import TYPE_CHECKING, Any, Self, TypeVar, cast

import boto3
import grpc

from rayito._aws import ControlPlane, PortSpec, control_plane_session, shared_control_plane
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
    UploadTicket,
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
    TimeoutException,
)
from rayito.sandbox_sync.code import CodeClient
from rayito.sandbox_sync.commands import Commands, StreamStarter
from rayito.sandbox_sync.filesystem import Filesystem
from rayito.sandbox_sync.git import Git
from rayito.sandbox_sync.lifecycle import DeadlineTrigger, set_timeout_once
from rayito.sandbox_sync.listing import ListingIo, SandboxListPaginator, stream_listing
from rayito.sandbox_sync.persistence import PersistenceClient
from rayito.sandbox_sync.pty import Pty
from rayito.sandbox_sync.transfer import Transfers
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
    from rayito.sandbox_sync.pool import SandboxPool

logger = logging.getLogger("rayito.sandbox")

T = TypeVar("T")
StubFactory = Callable[[Any], Any]

STATE_POLL_INTERVAL_SECONDS = 0.5


def resolve_control_plane(
    control_plane: ControlPlane | None,
    session: boto3.session.Session | None,
    region: str | None,
) -> ControlPlane:
    return control_plane or shared_control_plane(session, region=region)


def terminate_quietly(
    control_plane: ControlPlane, sandbox_id: str, log: logging.Logger = logger
) -> None:
    """Limpieza best-effort de un MicroVM que no llegó a estar listo: el error
    original es el que importa, así que un fallo aquí sólo se loguea (en el
    logger del sandbox, `rayito.sandbox` por defecto)."""
    try:
        control_plane.terminate_microvm(sandbox_id)
    except Exception:
        log.warning(
            "no se pudo terminar el sandbox %s tras un fallo de arranque", sandbox_id, exc_info=True
        )


def first_stream_message(call: Any, *, allow_empty: bool = False) -> tuple[Any, Any]:
    """Consume el primer mensaje de un server-stream síncrono. `Start`,
    `Connect` y `WatchDir` empiezan siempre con un mensaje; sólo `Read` (un
    fichero vacío) puede terminar sin ninguno, y entonces devuelve `None`."""
    try:
        first = next(call)
    except StopIteration:
        if allow_empty:
            return call, None
        raise SandboxException("el stream terminó antes del primer mensaje") from None
    return call, first


def wait_for_state(
    control_plane: ControlPlane,
    sandbox_id: str,
    wanted: str,
    *,
    timeout: float,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> SandboxInfo:
    """Sondea `get-microvm` hasta `wanted`. Un estado terminal es fatal."""
    deadline = monotonic() + timeout
    while True:
        info = control_plane.get_microvm(sandbox_id)
        if info.state == wanted:
            return info
        if info.state in TERMINAL_STATES:
            raise terminal_state_error(info)
        if monotonic() >= deadline:
            raise TimeoutException(
                f"el sandbox {sandbox_id} sigue {info.state} tras {timeout:g} s esperando {wanted}"
            )
        sleep(STATE_POLL_INTERVAL_SECONDS)


def closed_during_reconnect(sandbox_id: str) -> SandboxException:
    return SandboxException(f"el sandbox {sandbox_id} fue cerrado durante la reconexión")


def probe_health(
    control_plane: ControlPlane,
    info: SandboxInfo,
    transport: TransportSettings,
    request_timeout: float,
) -> health_pb2.HealthResponse | None:
    """Un `Health` a un sandbox `RUNNING` sin su access token: acuña un JWE
    para el puerto 8080, abre un canal dedicado (cerrado al salir), manda
    **un** `Health` (con el reintento tras un 403 del proxy) y devuelve la
    respuesta cruda. Cualquier otro estado devuelve `None` sin tocar el
    endpoint (una sonda despertaría un sandbox suspendido). Un `Health` que
    falla es `SandboxException` con el id del sandbox. Cuenta como tráfico
    para la política de idle del sandbox."""
    if info.state != "RUNNING":
        return None
    refresher = TokenRefresher(
        TokenStore(), lambda ports: control_plane.create_auth_token(info.sandbox_id, ports)
    )
    refresher.mint((PortSpec.single(DEFAULT_PORT),))
    plugin = ProxyAuthPlugin(
        refresher.store,
        port=DEFAULT_PORT,
        access_token=None,
        extra=transport.extra_metadata,
    )
    channel = transport.open_channel(info.endpoint, plugin)
    try:
        response = probe_health_reminting(
            health_pb2_grpc.HealthServiceStub(channel), refresher, request_timeout
        )
    except grpc.RpcError as exc:
        raise metadata_probe_failure(info.sandbox_id, exc) from exc
    finally:
        channel.close()
    return cast("health_pb2.HealthResponse", response)


def probe_metadata(
    control_plane: ControlPlane,
    info: SandboxInfo,
    transport: TransportSettings,
    request_timeout: float,
) -> dict[str, str] | None:
    """Los metadatos de un sandbox `RUNNING` vía `probe_health`: el mapa si
    `agent_ready`, `None` si el agente aún arranca o el estado no es
    `RUNNING`. Un `Health` que falla es `SandboxException` con el id del
    sandbox: nunca un hueco silencioso."""
    response = probe_health(control_plane, info, transport, request_timeout)
    if response is None:
        return None
    if not response.agent_ready:
        logger.info("sandbox %s aún arrancando: sin metadatos", info.sandbox_id)
        return None
    return metadata_from_health(response)


def probe_health_reminting(stub: Any, refresher: TokenRefresher, timeout: float) -> Any:
    """`Health` con un reintento tras un 403 del proxy (JWE rechazado)."""
    try:
        return stub.Health(health_pb2.HealthRequest(), timeout=timeout)
    except grpc.RpcError as exc:
        if not is_proxy_forbidden(exc):
            raise
    refresher.refresh_all()
    return stub.Health(health_pb2.HealthRequest(), timeout=timeout)


def call_dedicated_health(
    control_plane: ControlPlane,
    info: SandboxInfo,
    transport: TransportSettings,
    access_token: str,
    invoke: Callable[[health_pb2_grpc.HealthServiceStub], T],
) -> T:
    """Una llamada de `HealthService` con el access token a un sandbox
    `RUNNING` sin handle: acuña un JWE de un solo puerto (8080, sin
    refresco), abre un canal dedicado, invoca una vez (con un reintento
    tras un 403 del proxy) y cierra el canal al salir. Los errores de gRPC
    salen con la traducción unaria."""
    refresher = TokenRefresher(
        TokenStore(), lambda ports: control_plane.create_auth_token(info.sandbox_id, ports)
    )
    refresher.mint((PortSpec.single(DEFAULT_PORT),))
    plugin = ProxyAuthPlugin(
        refresher.store,
        port=DEFAULT_PORT,
        access_token=access_token,
        extra=transport.extra_metadata,
    )
    channel = transport.open_channel(info.endpoint, plugin)
    try:
        return invoke_reminting(invoke, health_pb2_grpc.HealthServiceStub(channel), refresher)
    except grpc.RpcError as exc:
        raise translate_rpc_error(exc) from exc
    finally:
        channel.close()


def invoke_reminting(invoke: Callable[[Any], T], stub: Any, refresher: TokenRefresher) -> T:
    """Un unario con un reintento tras un 403 del proxy (JWE rechazado)."""
    try:
        return invoke(stub)
    except grpc.RpcError as exc:
        if not is_proxy_forbidden(exc):
            raise
    refresher.refresh_all()
    return invoke(stub)


class Sandbox:
    """Un MicroVM con `rayd` dentro. Se crea con `create()` o `connect()`."""

    def __init__(
        self,
        *,
        info: SandboxInfo,
        access_token: str,
        control_plane: ControlPlane,
        transport: TransportSettings,
        refresher: TokenRefresher,
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
        self._reconnect_lock = threading.Lock()
        self._resumed = threading.Condition()
        self._plugin = ProxyAuthPlugin(
            refresher.store,
            port=DEFAULT_PORT,
            access_token=access_token,
            extra=transport.extra_metadata,
        )
        self._channel = transport.open_channel(info.endpoint, self._plugin)
        self._stream_channel: grpc.Channel | None = None
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
        self._reopen_lock = threading.Lock()
        self._reopens = 0
        self._deadline_trigger = DeadlineTrigger(
            self._on_deadline, name=f"rayito-deadline-{info.sandbox_id}"
        )
        self._unary_stubs: dict[StubFactory, Any] = {
            process_pb2_grpc.ProcessServiceStub: self._process,
            filesystem_pb2_grpc.FilesystemServiceStub: self._files,
            code_pb2_grpc.CodeServiceStub: self._code,
            pty_pb2_grpc.PtyServiceStub: self._pty_stub,
        }
        self._stream_stubs: dict[StubFactory, Any] = {}
        self._commands = Commands(self)
        self._filesystem = Filesystem(self)
        self._code_client = CodeClient(self)
        self._pty = Pty(self)
        self._persistence = PersistenceClient(self)
        self._persist: S3Prefix | None = None
        self._last_restore: RestoreResult | None = None
        self._launch_options: LaunchOptions | None = None
        self._transfer: S3Staging | None = None
        self._session: boto3.session.Session | None = None
        self._transfers = Transfers(self)
        self._git: Git | None = None

    # ------------------------------------------------------------------ create

    @classmethod
    def create(
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
        pool: SandboxPool | None = None,
        transfer: S3Staging | None = None,
        logger: logging.Logger | None = None,
    ) -> Self:
        """`run-microvm` → token del proxy → sondeo de `Health` hasta
        `agent_ready` y `kernel_ready` (el kernel por defecto ya rotado).

        Sin `max_lifetime` ni `on_timeout`, `timeout` es la vida máxima
        (running + suspended, tope 8 h) y no se puede cambiar después
        (ADR-007). Con cualquiera de los dos (ADR-011, exige una imagen M9),
        `timeout` es el plazo lógico que impone `rayd` aunque el cliente
        muera, movible con `set_timeout()` y `connect(timeout=)`, y
        `max_lifetime` (`120..=28800`, por defecto `timeout + 60`) es
        `maximumDurationInSeconds`: el plazo nunca pasa de `max_lifetime -
        60 s` desde el arranque. Al vencer, `on_timeout='kill'` (por defecto)
        termina el sandbox y `on_timeout='pause'` lo suspende (necesita
        `idle`; `idle.auto_resume` decide si una petición posterior lo
        reanuda con un plazo nuevo de `max(timeout, 300 s)`). Una imagen
        anterior a M9 falla con `LifecycleUnsupportedException` y el VM se
        termina (salvo `keep_on_failure`). `idle=None` desactiva la
        auto-suspensión. Sin `execution_role_arn` no hay logs de runtime.
        `reconnect_timeout` acota cuánto espera el SDK a que el agente vuelva
        tras un corte (pausa, auto-resume, 502 del proxy) antes de fallar.
        `metadata` son etiquetas no secretas (viajan en el `runHookPayload`
        junto a `envs`, dentro de sus 4096 caracteres) que el agente devuelve
        en `Health`: inmutables durante la vida del sandbox, legibles con
        `sbx.metadata`, `get_info()` y `Sandbox.list(metadata=...)`.
        `cpu_time_limit` son segundos de CPU, no de pared (`1..=28800`): cada
        proceso y PTY del sandbox recibe `RLIMIT_CPU` con ese tope (`SIGXCPU`
        al alcanzarlo, `SIGKILL` 5 s después); un proceso dormido no lo
        consume y el kernel de `run_code` nunca lo recibe. Para un tope de
        pared usa `timeout` en cada comando.

        `pool=` es azúcar de `pool.take()`: el sandbox sale de una plaza
        suspendida del `SandboxPool` (ya arrancado) con la configuración de
        lanzamiento de su `PoolConfig`; cualquier otro kwarg de lanzamiento o
        de plano distinto de su valor por defecto es `InvalidArgumentException`.
        Sólo `ready_timeout`, `request_timeout` y `reconnect_timeout` pasan.

        `persist=S3Prefix(...)` (requiere `execution_role_arn`) enlaza el
        `HOME` del sandbox a `s3://bucket/prefix/name/`: con `name` dado, tras
        `Health` el SDK restaura el checkpoint que haya bajo el prefijo
        (`sbx.last_restore`; `None` si no había ninguno) y sin `name` lo fija al
        `sandbox_id`. `persist_timeout` es el deadline del restore automático
        y de `reincarnate()`. Un restore que falla (salvo "no hay checkpoint")
        cierra y termina el sandbox como un fallo de readiness.

        `allow_internet_access=False` y `network={"allow_out", "deny_out",
        "egress_proxy"}` (ADR-012, semántica de E2B: sin `deny_out` todo está
        permitido y `allow_out` gana a `deny_out`; `ALL_TRAFFIC` es todo) se
        aplican en el guest y exigen una imagen M9 de `rayito-base-caps`:
        `rayd` instala deny-all antes de `/run` y `create()` manda la política
        real con `UpdateNetwork` antes de devolver. Sobre una imagen que no la
        aplica (`Health.egress_enforcement` `NONE`), el SDK termina el VM
        **aunque** haya `keep_on_failure` y lanza `UnimplementedError`. Una
        entrada de nombre de host (`"api.example.com"`, `"*.example.com"`) o
        `egress_proxy` activan el proxy local de `rayd`, que sólo alcanzan los
        clientes que respetan `HTTPS_PROXY`/`ALL_PROXY`; el resto falla cerrado.

        `transfer=S3Staging(...)` (o `RAYITO_TRANSFER_BUCKET` con `None`) es el
        bucket de `files.upload_url`/`download_url` y de los ficheros grandes
        (ADR-010): configuración del cliente, nada viaja al VM, también con
        `pool=`. Con `persist` en el mismo bucket, los prefijos deben ser
        disjuntos o es `InvalidArgumentException` antes de llamar a AWS.
        """
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
                pool.take(
                    ready_timeout=ready_timeout,
                    request_timeout=request_timeout,
                    reconnect_timeout=reconnect_timeout,
                ),
            )
            taken._bind_transfer(staging, pool.session)
            taken._bind_logger(logger)
            return taken
        plane = resolve_control_plane(control_plane, session, region)
        image_arn = plane.resolve_template_arn(resolve_template(template))
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
        info = plane.run_microvm(plan.request)
        sandbox_logger(logger).info("run-microvm aceptado: %s (%s)", info.sandbox_id, info.state)
        sandbox = cls._open(
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
            sandbox._apply_initial_network(launch)
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
            sandbox._bind_and_restore(
                persist, persist_timeout, terminate_on_failure=not keep_on_failure
            )
        return sandbox

    @class_method_variant("_class_connect")
    def connect(
        self, *, timeout: int | None = None, request_timeout: float | None = None
    ) -> Sandbox:
        """Reabre este handle: `get-microvm`, `resume-microvm` si está
        `SUSPENDED` sin auto-resume, el sondeo de `Health` y la extensión del
        plazo de `Sandbox.connect(sandbox_id, timeout=)`. Devuelve `self`."""
        info = self._control_plane.get_microvm(self.sandbox_id)
        if info.state in TERMINAL_STATES:
            raise terminal_state_error(info)
        self._info = info
        self._paused = False
        if needs_explicit_resume(info):
            self._control_plane.resume_microvm(self.sandbox_id)
            self._refresher.refresh_all()
        self._wait_until_ready(terminate_on_failure=False)
        self._extend_after_readiness(timeout, request_timeout=request_timeout)
        return self

    @classmethod
    def _class_connect(
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
        """Se engancha a un sandbox existente.
        `persist` (con `name`) sólo enlaza el prefijo para `checkpoint_files()`;
        no restaura nada y `reincarnate()` sigue exigiendo un `create()`.
        `transfer` como en `create()` (`None` lee `RAYITO_TRANSFER_BUCKET`).

        Necesita el `access_token` que generó `create()` (o `RAYITO_ACCESS_TOKEN`).
        Un sandbox `SUSPENDED` sin auto-resume se reanuda explícitamente; con
        auto-resume el propio sondeo de `Health` lo despierta.

        Nunca acorta el plazo lógico (ADR-011): con `timeout` manda
        `SetTimeout(AT_LEAST)` (el plazo pasa a ser al menos ahora +
        `timeout`); sin él, un sandbox reanudado después de su plazo
        (`resume_grace` o `expired`) se reabre con su propio `timeout`. Sobre
        un sandbox sin `max_lifetime`/`on_timeout` (ADR-007) `timeout` es
        `InvalidArgumentException`, y sobre una imagen anterior a M9
        `LifecycleUnsupportedException`.
        """
        token = require_access_token(access_token)
        bound = None if persist is None else require_named_persist(persist)
        staging = resolve_staging(transfer)
        validate_staging_against_persist(staging, bound)
        plane = resolve_control_plane(control_plane, session, region)
        info = plane.get_microvm(validate_sandbox_id(sandbox_id))
        if info.state in TERMINAL_STATES:
            raise terminal_state_error(info)
        if needs_explicit_resume(info):
            plane.resume_microvm(sandbox_id)
        sandbox = cls._open(
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
            sandbox._extend_after_readiness(timeout, request_timeout=None)
        except BaseException:
            sandbox.close()
            raise
        return sandbox

    @classmethod
    def _open(
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
        """Acuña el JWE, abre el canal y espera a `Health`.

        Con `terminate_on_failure`, cualquier fallo entre `run-microvm` y el
        primer `agent_ready` (token denegado, `Health` con UNAUTHENTICATED,
        Ctrl-C...) termina el MicroVM para no dejarlo facturando hasta
        `timeout`. La única excepción es `SandboxNotReadyException`: el sondeo
        de readiness ya decidió el destino del VM antes de lanzarla.
        `readiness` es el calendario del sondeo: `create()`, `connect()` y
        `resume()` usan el de `ReadinessPoll`; el pool pasa `TakePoll`.
        `require_lifecycle` (el lanzamiento mandó un bloque `lifecycle`)
        exige que el `Health` de readiness lo traiga: un agente anterior a M9
        es `LifecycleUnsupportedException` dentro de este mismo camino de
        fallo, así que nunca queda un sandbox con un timeout sin imponer.
        """
        refresher = TokenRefresher(
            TokenStore(),
            lambda ports: control_plane.create_auth_token(info.sandbox_id, ports),
            logger=logger,
        )
        sandbox: Self | None = None
        try:
            refresher.mint(proxy_ports)
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
            ready = sandbox._wait_until_ready(
                terminate_on_failure=terminate_on_failure, readiness=readiness
            )
            if require_lifecycle and lifecycle_from_proto(ready) is None:
                raise older_agent_error(info.template_name, str(ready.agent_version))
        except BaseException as exc:
            if sandbox is not None:
                sandbox.close()
            if terminate_on_failure and not isinstance(exc, SandboxNotReadyException):
                terminate_quietly(control_plane, info.sandbox_id, sandbox_logger(logger))
            raise
        refresher.start()
        return sandbox

    @classmethod
    def list(
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
    ) -> Iterator[SandboxListItem]:
        """Pagina `list-microvms` (páginas de 50) de forma perezosa. Sin
        `states` omite `TERMINATING|TERMINATED`, que AWS sigue listando ~20 min
        después de morir. `template` viaja como filtro de servidor; `states` y
        `started_after` ("en o después") se filtran en cliente.

        Con `metadata` filtra en cliente y es **O(n)**: `list-microvms` no
        conoce los metadatos, así que por cada sandbox `RUNNING` hace, en
        orden y uno a uno, `get-microvm` + `create-microvm-auth-token` + un
        `Health` (5 s de plazo, canal dedicado) ≈ 0,5-1 s por sandbox, y cada
        sonda cuenta como tráfico para la política de idle de ese sandbox
        (pospone su auto-suspensión una ventana). Devuelve sólo los items
        cuyo agente respondió `agent_ready` y cuyos metadatos contienen cada
        par pedido (subconjunto exacto), con `metadata` relleno; un sandbox
        que dejó de estar `RUNNING` o que aún arranca se omite; un `Health`
        que falla es `SandboxException` con su id (nunca una lista incompleta
        en silencio). `states` sólo admite `RUNNING`: sondear un sandbox
        suspendido lo despertaría. Filtra por `template` antes si hay muchos.

        `order` (`"asc"`/`"desc"` por `startedAt`) se calcula en cliente: AWS
        no ordena, así que recorre todas las páginas antes del primer item.
        Para reanudar un listado usa `paginate()`.
        """
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
        image_arn = plane.resolve_template_arn(template) if template else None
        io = ListingIo(plane, probe_metadata, transport or TransportSettings(), request_timeout)
        return stream_listing(io, request, image_arn)

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
    ) -> SandboxListPaginator:
        """Los filtros de `list()` como un listado reanudable: cada
        `next_items()` devuelve hasta `limit` items y `next_token` (opaco,
        ligado a estos filtros) continúa en un `paginate()` nuevo. Valida todo
        sin llamar a AWS; el template se resuelve en el primer `next_items()`.

        Al reanudar vuelve a pedir la página del cursor y salta por identidad
        los items ya servidos de ella. Con `order` el primer `next_items()`
        recorre todas las páginas (O(páginas)) y la reanudación es por clave
        (`startedAt`, id)."""
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
        io = ListingIo(plane, probe_metadata, transport or TransportSettings(), request_timeout)
        return SandboxListPaginator(
            io=io, request=request, resolve_template_arn=plane.resolve_template_arn
        )

    # -------------------------------------------------------------- properties

    @property
    def sandbox_id(self) -> str:
        return self._info.sandbox_id

    @property
    def access_token(self) -> str:
        """Guárdalo junto al `sandbox_id` para poder hacer `connect()` después."""
        return self._access_token

    @property
    def endpoint(self) -> str:
        return self._info.endpoint

    @property
    def endpoint_url(self) -> str:
        return self._info.endpoint_url

    @property
    def info(self) -> SandboxInfo:
        """Última `SandboxInfo` conocida sin llamar a AWS; `get_info()` la refresca."""
        return self._info

    @property
    def launch_info(self) -> SandboxInfo:
        """La `SandboxInfo` con la que se abrió el handle (la respuesta de
        `run-microvm` en `create()`, el `get-microvm` de `connect()`), nunca
        refrescada y sin llamar a AWS."""
        return self._launch_info

    @property
    def region(self) -> str:
        return self._control_plane.region

    @property
    def resume_generation(self) -> int:
        """Última generación de resume vista en `Health` (cambia con cada `/resume`)."""
        return self._resume_generation

    @property
    def metadata(self) -> dict[str, str]:
        """Metadatos de `create(metadata=)` tal como los devolvió el último
        `Health`; inmutables durante la vida del sandbox, así que son
        definitivos desde el primer `agent_ready` (vacíos sobre una imagen
        anterior a M6)."""
        return dict(self._metadata)

    @property
    def persist(self) -> S3Prefix | None:
        """El `S3Prefix` (con `name`) enlazado por `create(persist=)` o
        `connect(persist=)`; `None` si el sandbox no persiste."""
        return self._persist

    @property
    def transfer(self) -> S3Staging | None:
        """El `S3Staging` de `create(transfer=)`/`connect(transfer=)` o de
        `RAYITO_TRANSFER_BUCKET`; `None` sin bucket de transferencias."""
        return self._transfer

    @property
    def last_restore(self) -> RestoreResult | None:
        """El resultado del restore automático de `create(persist=)`; `None` si
        no hubo (primera vida de ese `name`) o si el sandbox no persiste."""
        return self._last_restore

    # --------------------------------------------------------------- lifecycle

    @class_method_variant("_class_kill")
    def kill(self) -> bool:
        """`terminate-microvm` (idempotente). False sólo si ya no existe."""
        try:
            return self._control_plane.terminate_microvm(self.sandbox_id)
        finally:
            self.close()

    @classmethod
    def _class_kill(
        cls,
        sandbox_id: str,
        *,
        region: str | None = None,
        session: boto3.session.Session | None = None,
        control_plane: ControlPlane | None = None,
    ) -> bool:
        plane = resolve_control_plane(control_plane, session, region)
        return plane.terminate_microvm(validate_sandbox_id(sandbox_id))

    @class_method_variant("_class_get_info")
    def get_info(self) -> SandboxInfo:
        """`get-microvm` fresco y, si el sandbox está `RUNNING` con plazo
        lógico gestionado (ADR-011), un `Health` (registrado) que lo
        refresca: `expires_at` es el plazo vigente aunque otro cliente lo
        haya movido. `metadata` y los hechos del guest vienen del último
        `Health` sin RPC extra (quedan fijos en `/run`), y nunca se sondea
        un sandbox que no está `RUNNING` (la sonda lo despertaría). Los
        hechos del guest sólo van en el valor devuelto: `sbx.info` los deja
        en `None`."""
        info = self._control_plane.get_microvm(self.sandbox_id)
        if deadline_may_have_moved(info.state, self._lifecycle):
            self._refresh_health()
        self._info = dataclasses.replace(info, metadata=self.metadata, lifecycle=self._lifecycle)
        return with_guest_facts(self._info, self._guest)

    @classmethod
    def _class_get_info(
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
        """`Sandbox.get_info(sandbox_id)`: `get-microvm` y, con `read_metadata`
        sobre un sandbox `RUNNING`, un JWE más un `Health` (canal dedicado,
        cerrado después) para rellenar `metadata` y `lifecycle`; en cualquier
        otro estado `metadata` es `None` sin tocar el endpoint."""
        plane = resolve_control_plane(control_plane, session, region)
        info = plane.get_microvm(validate_sandbox_id(sandbox_id))
        if not read_metadata:
            return info
        probed = probe_health(plane, info, transport or TransportSettings(), request_timeout)
        return with_guest_facts(info_with_health(info, probed), ready_guest_facts(probed))

    @class_method_variant("_class_pause")
    def pause(self, *, wait: bool = True) -> bool:
        """`suspend-microvm` (2 TPS). False si ya estaba `SUSPENDING|SUSPENDED`
        (`get-microvm` se lee antes: la API acepta un `suspend` repetido).

        El agente cierra todos los streams abiertos con un final `suspending`
        y los handles vivos se reenganchan solos tras `resume()` (o tras el
        auto-resume) la próxima vez que se lean. Mientras este `Sandbox` tenga
        la pausa pendiente, ningún stream en curso (ni siquiera un `run_code`
        o un `run` en foreground) sondea `Health`: la pausa no se deshace sola.
        """
        self._info = self._control_plane.get_microvm(self.sandbox_id)
        if already_suspended(self._info):
            return False
        suspended = self._suspend_marking_paused()
        if suspended and wait:
            self._info = wait_for_state(
                self._control_plane, self.sandbox_id, "SUSPENDED", timeout=self._ready_timeout
            )
        return suspended

    @classmethod
    def _class_pause(
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
        if already_suspended(plane.get_microvm(validate_sandbox_id(sandbox_id))):
            return False
        suspended = plane.suspend_microvm(sandbox_id)
        if suspended and wait:
            wait_for_state(plane, sandbox_id, "SUSPENDED", timeout=ready_timeout)
        return suspended

    @class_method_variant("_class_resume")
    def resume(self, *, wait: bool = True) -> None:
        """`resume-microvm`, reacuña el JWE y espera a `Health` con
        `kernel_ready`, registrando la nueva `resume_generation`.

        Un `ConflictException` (el sandbox ya estaba `RUNNING`, por ejemplo
        porque el auto-resume se adelantó) no es un error. Los handles,
        watches y `run_code` en curso se reenganchan por su cuenta. Con
        `wait`, un sandbox reanudado después de su plazo lógico (`resume_grace`
        o `expired`) se reabre con su propio `timeout`, como `connect()`.
        """
        self._paused = False
        self._control_plane.resume_microvm(self.sandbox_id)
        self._refresher.refresh_all()
        if wait:
            self._wait_until_ready(terminate_on_failure=False)
            self._extend_after_readiness(None, request_timeout=None)

    @classmethod
    def _class_resume(
        cls,
        sandbox_id: str,
        *,
        wait: bool = True,
        region: str | None = None,
        session: boto3.session.Session | None = None,
        control_plane: ControlPlane | None = None,
        ready_timeout: float = DEFAULT_READY_TIMEOUT_SECONDS,
    ) -> None:
        """Sin token del agente sólo se puede esperar el estado de `get-microvm`."""
        plane = resolve_control_plane(control_plane, session, region)
        plane.resume_microvm(validate_sandbox_id(sandbox_id))
        if wait:
            wait_for_state(plane, sandbox_id, "RUNNING", timeout=ready_timeout)

    @class_method_variant("_class_set_timeout")
    def set_timeout(self, timeout: int, *, request_timeout: float | None = None) -> None:
        """Fija el plazo lógico en ahora + `timeout` segundos (`SetTimeout`
        EXACT, ADR-011): puede alargarlo o acortarlo, y reabre un sandbox en
        modo `pause` cuyo plazo venció pero que aún no se suspendió. El tope
        es `max_lifetime` (fijo desde `create()`, como mucho 28800 s): más
        allá, `InvalidArgumentException` con el plazo intacto y
        `reincarnate()` como salida. Sin `max_lifetime`/`on_timeout` en
        `create()` también es `InvalidArgumentException`."""
        seconds = validate_set_timeout_seconds(timeout)
        self._send_set_timeout(
            TimeoutRequest("exact", seconds * 1000), request_timeout=request_timeout
        )

    @classmethod
    def _class_set_timeout(
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
        """`Sandbox.set_timeout(sandbox_id, timeout)`: el mismo `SetTimeout`
        EXACT sin handle. Exige el access token (o `RAYITO_ACCESS_TOKEN`);
        un sandbox terminado es `SandboxNotFoundException` y uno suspendido
        `SandboxStateException` sin despertarlo (`connect()` lo reanuda).
        Acuña un JWE y usa un canal dedicado que cierra al terminar."""
        token = require_access_token(access_token, operation="set_timeout(sandbox_id)")
        seconds = validate_set_timeout_seconds(timeout)
        plane = resolve_control_plane(control_plane, session, region)
        info = plane.get_microvm(validate_sandbox_id(sandbox_id))
        if info.state in TERMINAL_STATES:
            raise terminal_state_error(info)
        if info.state in SUSPENDED_STATES:
            raise suspended_set_timeout_error(info.sandbox_id)
        request = TimeoutRequest("exact", seconds * 1000)
        try:
            set_timeout_once(
                plane,
                info,
                access_token=token,
                request=request,
                transport=transport or TransportSettings(),
                request_timeout=request_timeout,
            )
        except grpc.RpcError as exc:
            raise translate_set_timeout_error(exc, request) from exc

    def is_running(self, *, request_timeout: float | None = None) -> bool:
        """True si `rayd` responde a `Health` con `agent_ready`.
        `request_timeout` acota esa sonda (nunca por encima de 5 s)."""
        timeout = min(ReadinessPoll.MAX_RPC_TIMEOUT, self._resolve_request_timeout(request_timeout))
        response = self._probe_health(timeout)
        return response is not None and response.agent_ready

    def get_health(self, *, request_timeout: float | None = None) -> SandboxHealth:
        """`HealthService.Health`: readiness, `resume_generation`,
        `clock_offset_ms` y `kernel_state_lost` del agente."""
        timeout = self._resolve_request_timeout(request_timeout)
        response = self._translated_unary(
            lambda: self._health.Health(health_pb2.HealthRequest(), timeout=timeout)
        )
        self._record_health(response)
        return health_from_proto(response)

    def upload_url(
        self,
        path: str,
        user: str | None = None,
        use_signature_expiration: int | None = None,
    ) -> UploadTicket:
        """La forma de E2B de `files.upload_url`: `use_signature_expiration`
        son los segundos de vida de la URL (3600 con `None`; `<= 0` es
        `InvalidArgumentException`, una URL de S3 siempre caduca)."""
        return self._filesystem.upload_url(
            path,
            user=user,
            expires_in=expires_in_from_signature_expiration(use_signature_expiration),
        )

    def download_url(
        self,
        path: str,
        user: str | None = None,
        use_signature_expiration: int | None = None,
    ) -> DownloadLink:
        """La forma de E2B de `files.download_url`, con la misma regla para
        `use_signature_expiration` que `upload_url`."""
        return self._filesystem.download_url(
            path,
            user=user,
            expires_in=expires_in_from_signature_expiration(use_signature_expiration),
        )

    def get_host(self, port: int) -> HostAccess:
        """Hostname + cabeceras del proxy para `port` (token acuñado bajo demanda)."""
        validated = validate_host_port(port)
        self._refresher.ensure(validated)
        return HostAccess(
            self.endpoint, port=validated, token_provider=lambda: self._current_jwe(validated)
        )

    def get_metrics(self, *, request_timeout: float | None = None) -> SandboxMetrics:
        """`HealthService.Metrics` (procfs). Cuesta ≈ 100 ms: el agente toma dos
        muestras de `/proc/stat` para calcular `cpu_used_pct`."""
        timeout = self._resolve_request_timeout(request_timeout)
        response = self._translated_unary(
            lambda: self._health.Metrics(health_pb2.MetricsRequest(), timeout=timeout)
        )
        return metrics_from_proto(response)

    @class_method_variant("_class_get_metrics_history")
    def get_metrics_history(
        self,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        max_points: int | None = None,
        request_timeout: float | None = None,
    ) -> builtins.list[SandboxMetrics]:
        """`HealthService.MetricsHistory`: la serie que `rayd` muestrea cada
        5 s desde `/run` (un anillo de 8 h), en orden ascendente.

        `start` y `end` son inclusivos (un `datetime` naive es hora local);
        `max_points` reduce la serie a ese número de tramos: cada punto es la
        última muestra de su tramo con `cpu_used_pct` promediado. En cada
        muestra `cpu_used_pct` es la media desde la anterior (≈ 5 s), no la
        ventana de 100 ms de `get_metrics()`. Mientras el sandbox está
        suspendido no se muestrea: la serie tiene un hueco. Una imagen
        anterior a M9 es `UnimplementedError`."""
        request = metrics_history_request(start, end, max_points)
        timeout = self._resolve_request_timeout(request_timeout)
        try:
            response = self._translated_unary(
                lambda: self._health.MetricsHistory(request, timeout=timeout)
            )
        except SandboxException as exc:
            if is_history_unimplemented(exc):
                raise history_unimplemented_error(exc, HISTORY_FEATURE) from exc
            raise
        return metrics_history_from_proto(response)

    @classmethod
    def _class_get_metrics_history(
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
        """`Sandbox.get_metrics_history(sandbox_id)`: el historial sin handle.
        `rayd` exige el access token del sandbox (explícito o
        `RAYITO_ACCESS_TOKEN`); sin él es `AuthenticationException` sin llamar
        a AWS. Sólo lee un sandbox `RUNNING`: en otro estado falla sin acuñar
        ningún JWE, porque la llamada lo despertaría. Cada llamada acuña un
        JWE y abre un canal dedicado."""
        validated_id = validate_sandbox_id(sandbox_id)
        token = require_access_token(access_token, operation="get_metrics_history(sandbox_id)")
        request = metrics_history_request(start, end, max_points)
        plane = resolve_control_plane(control_plane, session, region)
        info = plane.get_microvm(validated_id)
        ensure_history_readable(info)
        timeout = DEFAULT_REQUEST_TIMEOUT_SECONDS if request_timeout is None else request_timeout
        try:
            response = call_dedicated_health(
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

    def close(self) -> None:
        """Libera recursos locales (hilo del refresher, canales). No toca el
        MicroVM. Una reconexión en curso termina con `SandboxException`."""
        if self._closed:
            return
        self._closed = True
        self._deadline_trigger.cancel()
        self._notify_resumed()
        self._filesystem._stop_watches()
        self._refresher.stop()
        self._channel.close()
        if self._stream_channel is not None:
            self._stream_channel.close()

    # ----------------------------------------------------------------- network

    @class_method_variant("_class_update_network")
    def update_network(
        self,
        network: NetworkPolicy | NetworkOptions | None = None,
        *,
        allow_internet_access: bool | None = None,
        request_timeout: float | None = None,
    ) -> NetworkState:
        """`NetworkService.UpdateNetwork` (ADR-012): sustituye la política
        entera de forma atómica y afecta a las conexiones nuevas (las abiertas
        siguen). `None` o `{}` es sin restricciones; `allow_internet_access=
        False` añade `ALL_TRAFFIC` a `deny_out`. Una imagen sin `CAP_NET_ADMIN`
        o con un `rayd` anterior a M9 es `UnimplementedError`; una entrada mal
        formada, `InvalidArgumentException` con la lista y el índice."""
        policy = update_policy(network, allow_internet_access)
        return self._send_update_network(
            policy, feature=UPDATE_NETWORK_FEATURE, request_timeout=request_timeout
        )

    @classmethod
    def _class_update_network(
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
        """`Sandbox.update_network(sandbox_id, network)`: `connect()` (exige el
        access token o `RAYITO_ACCESS_TOKEN`), `update_network()` y `close()`;
        nunca termina el sandbox."""
        policy = update_policy(network, allow_internet_access)
        sandbox = cls._class_connect(
            sandbox_id,
            access_token=access_token,
            region=region,
            session=session,
            request_timeout=request_timeout,
            control_plane=control_plane,
            transport=transport,
        )
        try:
            return sandbox._send_update_network(
                policy, feature=UPDATE_NETWORK_FEATURE, request_timeout=request_timeout
            )
        finally:
            sandbox.close()

    def get_network(self, *, request_timeout: float | None = None) -> NetworkState:
        """`NetworkService.GetNetwork`: la política vigente y cómo se aplica,
        sin la dirección ni las credenciales del proxy del operador."""
        timeout = self._resolve_request_timeout(request_timeout)
        try:
            response = self._call_unary(
                lambda: self._network_stub.GetNetwork(
                    network_pb2.GetNetworkRequest(), timeout=timeout
                )
            )
        except grpc.RpcError as exc:
            raise network_rpc_error(exc, feature=GET_NETWORK_FEATURE) from exc
        return state_from_proto(response)

    # ------------------------------------------------------------ sub-clients

    @property
    def commands(self) -> Commands:
        """`ProcessService`: `run`, `list`, `kill`, `connect`, `send_stdin`, `close_stdin`."""
        return self._commands

    @property
    def files(self) -> Filesystem:
        """`FilesystemService`: `read`, `write`, `write_files`, `list`, `exists`,
        `get_info`, `remove`, `rename`, `make_dir`, `watch_dir`."""
        return self._filesystem

    @property
    def pty(self) -> Pty:
        """`PtyService`: `create`, `connect`, `send_input`, `resize`, `kill`."""
        return self._pty

    @property
    def git(self) -> Git:
        """El módulo git de E2B 2.x (`clone`, `status`, `commit`, `push`...)
        sobre `commands.run`; se construye en el primer uso."""
        if self._git is None:
            self._git = Git(self._commands)
        return self._git

    # -------------------------------------------------------------------- code

    def run_code(
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
        """Ejecuta código en un kernel Jupyter con estado (`CodeService.Execute`).

        `language` elige el kernel: `python` (por defecto), `bash`,
        `javascript` (alias `js`) o `typescript` (alias `ts`). `bash` y los
        dos últimos, que sirve el kernel Jupyter de Deno, viven en la variante
        de imagen `rayito-base-poly` y arrancan en la primera celda de ese
        lenguaje (≈ 1 s dentro del `timeout`); en `rayito-base` la llamada
        falla con `InvalidArgumentException` (`grpc_code` `UNIMPLEMENTED`,
        mensaje que nombra `rayito-base-poly`). `language` y `context` son
        excluyentes; `envs` por ejecución sólo en contextos Python.

        Devuelve una `Execution` con `results` (mime bundles: `text`, `png`,
        `chart`, `data`...), `logs.stdout`/`logs.stderr`, `error` y
        `execution_count`; `execution.text` es el valor de la última expresión.
        Un error del kernel es dato en `error`, nunca excepción. `timeout`
        (300 s por defecto) lo impone el agente sobre el reloj corrido (una
        pausa no lo consume) y termina en `error.name == "ExecutionTimeout"`
        con el contexto intacto salvo que el kernel no responda al interrupt.
        `context` es un `CodeContext` o su id; `None` es el contexto por
        defecto. Si el sandbox se pausa a mitad de la celda, el SDK espera al
        resume y continúa con `Reattach` sin volver a ejecutarla.
        """
        return self._code_client.run_code(
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

    def checkpoint_files(
        self,
        *,
        target: S3Prefix | None = None,
        exclude: Sequence[str] = (),
        timeout: float = DEFAULT_PERSIST_TIMEOUT_SECONDS,
        on_progress: CheckpointProgressCallback | None = None,
        user: str | None = None,
    ) -> CheckpointResult:
        """`FilesystemService.Checkpoint`: `rayd` empaqueta el `HOME` del usuario
        (tar.gz, sin `.cache`, `__pycache__`, `.ipynb_checkpoints` ni el runtime
        de Jupyter) y lo sube como root con el execution role a
        `s3://bucket/prefix/name/home.tar.gz` + `manifest.json`. `target`
        por defecto es `sbx.persist`. `exclude` son directorios relativos al
        `HOME` (sin globs, máx. 64). Un solo checkpoint o restore a la vez por
        sandbox (`PersistenceException(code="failed_precondition")`). Los
        procesos que escriban durante el checkpoint pueden dejar copias
        truncadas: pausa tu trabajo antes."""
        return self._persistence.checkpoint(
            target=target,
            bound=self._persist,
            exclude=exclude,
            timeout=timeout,
            on_progress=on_progress,
            user=user,
        )

    def restore_files(
        self,
        *,
        source: S3Prefix | None = None,
        timeout: float = DEFAULT_PERSIST_TIMEOUT_SECONDS,
        on_progress: RestoreProgressCallback | None = None,
        user: str | None = None,
    ) -> RestoreResult:
        """`FilesystemService.Restore`: descarga el checkpoint de `source` (por
        defecto `sbx.persist`) y lo extrae sobre el `HOME` (los ficheros
        existentes se sobrescriben, los directorios se fusionan, nada sale del
        `HOME`). `NotFoundException` si no hay checkpoint. Un fallo a mitad
        deja el `HOME` parcialmente restaurado: `kill()` + `create(persist=)`
        es la recuperación."""
        return self._persistence.restore(
            source=source, bound=self._persist, timeout=timeout, on_progress=on_progress, user=user
        )

    def reincarnate(
        self,
        *,
        exclude: Sequence[str] = (),
        persist_timeout: float = DEFAULT_PERSIST_TIMEOUT_SECONDS,
    ) -> Self:
        """La respuesta a lo que `set_timeout` no puede dar, pasar de
        `max_lifetime` (ADR-011): `checkpoint_files()` → `create(persist=)`
        con las mismas opciones de lanzamiento (que restaura) → `kill()` de este
        sandbox, y devuelve el nuevo. El nuevo tiene 8 h frescas, otro
        `sandbox_id`, otro access token (salvo que el original fuera explícito)
        y los mismos `metadata`; las variables del kernel, los procesos y las
        PTY no sobreviven (ADR-007), sólo los ficheros del `HOME`. Si el
        `create()` falla, este sandbox sigue vivo y la excepción lleva una nota
        con la `uri` del checkpoint ya completo. Sólo sobre un sandbox de
        `create(persist=)`: un handle de `connect()` no conoce el lanzamiento."""
        options = self._launch_options
        if options is None:
            raise reincarnate_requires_create_error()
        persist = self._persist
        if persist is None:
            raise reincarnate_requires_persist_error()
        self.checkpoint_files(exclude=exclude, timeout=persist_timeout)
        try:
            successor = type(self).create(
                **launch_kwargs(options), persist=persist, persist_timeout=persist_timeout
            )
        except BaseException as exc:
            add_reincarnate_note(exc, persist.uri)
            raise
        try:
            self.kill()
        except SandboxNotFoundException:
            self._logger.info("sandbox %s ya no existía al reencarnar", self.sandbox_id)
        return successor

    def _bind_and_restore(
        self, persist: S3Prefix, persist_timeout: float, *, terminate_on_failure: bool
    ) -> None:
        """Reglas 2 y 3 de D9: enlaza el prefijo y, si el caller dio `name`,
        restaura lo que haya; "no hay checkpoint" es la primera vida del nombre."""
        self._persist = bind_persist(persist, self.sandbox_id)
        if not should_auto_restore(persist):
            return
        try:
            self._last_restore = self.restore_files(timeout=persist_timeout)
        except NotFoundException:
            self._logger.info(
                "sandbox %s: sin checkpoint en %s", self.sandbox_id, self._persist.uri
            )
            self._last_restore = None
        except BaseException:
            self.close()
            if terminate_on_failure:
                terminate_quietly(self._control_plane, self.sandbox_id, self._logger)
            raise

    def _apply_initial_network(self, launch: NetworkLaunch) -> None:
        """Paso 5 de la compuerta de `create()` (ADR-012): cualquier fallo
        (imagen que no aplica la política, error del RPC, Ctrl-C) cierra el
        cliente y termina el VM **aunque** haya `keep_on_failure`: un sandbox
        cuya política pedida no se aplica nunca queda corriendo."""
        try:
            self._enforce_launch_policy(launch)
        except BaseException:
            self.close()
            terminate_quietly(self._control_plane, self.sandbox_id, self._logger)
            raise

    def _enforce_launch_policy(self, launch: NetworkLaunch) -> None:
        reported = readiness_enforcement(self._readiness_health)
        refused = egress_gate_error(self.sandbox_id, reported, launch.feature)
        if refused is not None:
            raise refused
        state = self._send_update_network(
            launch.policy, feature=launch.feature, request_timeout=None
        )
        refused = egress_gate_error(self.sandbox_id, state.enforcement, launch.feature)
        if refused is not None:
            raise refused

    def _send_update_network(
        self, policy: NetworkPolicy, *, feature: str, request_timeout: float | None
    ) -> NetworkState:
        timeout = self._resolve_request_timeout(request_timeout)
        request = network_pb2.UpdateNetworkRequest(policy=policy_to_proto(policy))
        try:
            response = self._call_unary(
                lambda: self._network_stub.UpdateNetwork(request, timeout=timeout)
            )
        except grpc.RpcError as exc:
            raise network_rpc_error(exc, feature=feature) from exc
        return state_from_proto(response)

    def create_code_context(
        self,
        *,
        cwd: str | None = None,
        language: str | None = None,
        envs: Mapping[str, str] | None = None,
        request_timeout: float | None = None,
    ) -> CodeContext:
        """Kernel nuevo con su propio scope, `cwd` y `envs` (máximo 8 por
        sandbox); `language` es `python`, `bash`, `javascript` (alias `js`)
        o `typescript` (alias `ts`). Los tres últimos sólo existen en
        `rayito-base-poly` (JS y TS con el kernel de Deno, arrancado al
        pedirlo, nunca antes de `/ready`)."""
        return self._code_client.create_context(
            cwd=cwd, language=language, envs=envs, request_timeout=request_timeout
        )

    def list_code_contexts(
        self, *, request_timeout: float | None = None
    ) -> builtins.list[CodeContext]:
        return self._code_client.list_contexts(request_timeout=request_timeout)

    def remove_code_context(
        self, context: ContextLike, *, request_timeout: float | None = None
    ) -> None:
        """Mata el kernel del contexto; el `default` no se puede borrar."""
        self._code_client.remove_context(context, request_timeout=request_timeout)

    def restart_code_context(
        self, context: ContextLike, *, request_timeout: float | None = None
    ) -> None:
        """Kernel nuevo con el mismo id; el estado del contexto se pierde."""
        self._code_client.restart_context(context, request_timeout=request_timeout)

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
        """El bucket de transferencias y la sesión boto3 con la que el SDK
        firma sus URLs: la de `create`/`connect`, si no la del plano de
        control (`LambdaMicrovmsControlPlane.session`), y si tampoco hay, la
        cadena por defecto."""
        self._transfer = staging
        self._session = session or control_plane_session(self._control_plane)

    def _current_jwe(self, port: int) -> str:
        return self._refresher.store.jwe_for(port) or self._refresher.ensure(port).jwe

    def _call_unary_once(self, call: Callable[[], T]) -> T:
        """Reintenta una vez tras un 403 del proxy (JWE caducado). Es lo que
        usa la propia sonda de `Health`, que nunca debe reconectar."""
        try:
            return call()
        except grpc.RpcError as exc:
            if not is_proxy_forbidden(exc):
                raise
        self._logger.info("el proxy rechazó el token del sandbox %s; reacuñando", self.sandbox_id)
        self._refresher.refresh_all()
        return call()

    def _call_unary(self, call: Callable[[], T], *, reopen: bool = True) -> T:
        """Un unario con el reintento del 403 y, tras un corte reconectable
        (502, reset, `suspending`), una reconexión y **un** reintento.
        `reopen=False` es el `SetTimeout` de la propia reapertura tras la
        pausa del plazo, que no puede volver a disparar otra.

        Un `send_stdin`/`send_input` cortado después de que `rayd` lo aplicara
        puede aplicarse dos veces (el mismo trato que hace E2B). Nunca streams.
        """
        seen_generation = self._resume_generation
        seen_reopens = self._reopens
        try:
            return self._call_unary_once(call)
        except grpc.RpcError as exc:
            if reopen and self._reopened_after_deadline_pause(exc, seen_reopens):
                return self._call_unary_once(call)
            if not self._is_reconnectable(exc):
                raise
            reason = exc
        outcome = self._reconnect(reason, seen_generation)
        if not outcome.resumed:
            raise self._reconnect_error(outcome, reason) from reason
        return call()

    def _translated_unary(self, call: Callable[[], T], *, filesystem: bool = False) -> T:
        try:
            return self._call_unary(call)
        except grpc.RpcError as exc:
            raise translate_rpc_error(exc, filesystem=filesystem) from exc

    def _resolve_request_timeout(self, request_timeout: float | None) -> float:
        return self._request_timeout if request_timeout is None else request_timeout

    def _process_call(self, invoke: Callable[[Any, float], T], request_timeout: float | None) -> T:
        """Unario de `ProcessService` en el canal de unarios, con el reintento
        de 403 y los errores traducidos."""
        timeout = self._resolve_request_timeout(request_timeout)
        return self._translated_unary(lambda: invoke(self._process, timeout))

    def _pty_call(self, invoke: Callable[[Any, float], T], request_timeout: float | None) -> T:
        """Unario de `PtyService` en el canal de unarios."""
        timeout = self._resolve_request_timeout(request_timeout)
        return self._translated_unary(lambda: invoke(self._pty_stub, timeout))

    def _files_call(self, invoke: Callable[[Any, float], T], request_timeout: float | None) -> T:
        """Unario (o `Write`, cuyo iterador de requests se recrea en el
        reintento) de `FilesystemService` en el canal de unarios; `NOT_FOUND`
        es `FileNotFoundException`."""
        timeout = self._resolve_request_timeout(request_timeout)
        return self._translated_unary(lambda: invoke(self._files, timeout), filesystem=True)

    def _code_call(
        self,
        invoke: Callable[[Any, float], T],
        request_timeout: float | None,
        *,
        default_timeout: float | None = None,
    ) -> T:
        """Unario de `CodeService` en el canal de unarios. `default_timeout`
        sustituye al `request_timeout` del sandbox cuando el RPC arranca un
        kernel (`CreateContext`, `RestartContext`: 90 s)."""
        timeout = self._resolve_request_timeout(request_timeout)
        if request_timeout is None and default_timeout is not None:
            timeout = default_timeout
        return self._translated_unary(lambda: invoke(self._code, timeout))

    def _stub(self, service: StubFactory, *, stream: bool) -> Any:
        """El canal de streams se abre en el primer uso: es el segundo y último
        canal del sandbox y comparte el plugin, así la rotación del JWE le llega."""
        if not stream:
            return self._unary_stubs[service]
        if self._stream_channel is None:
            self._stream_channel = self._transport.open_channel(self.endpoint, self._plugin)
        stub = self._stream_stubs.get(service)
        if stub is None:
            stub = service(self._stream_channel)
            self._stream_stubs[service] = stub
        return stub

    def _open_stream(
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
        """Abre un server-stream y consume su primer mensaje.

        Un 403 del proxy antes del primer mensaje se reintenta una vez tras
        reacuñar: el proxy nunca reenvió la petición a `rayd`, así que nada se
        ejecutó dos veces. Un corte reconectable antes del primer mensaje
        (502, reset, phase gate `suspending`) espera la reconexión y reintenta
        una vez, salvo `reconnect=False` (`Execute`: reabrirlo podría correr
        la celda dos veces). Después del primer mensaje no hay 403 posible: el
        proxy sólo evalúa el token al abrir la conexión. `translate` sustituye
        la tabla unaria para un status que no es un corte (persistencia).
        """
        stub = self._stub(service, stream=stream)
        seen_generation = self._resume_generation
        seen_reopens = self._reopens
        try:
            return self._first_message_reminting(start, stub, allow_empty)
        except grpc.RpcError as exc:
            reason = exc
        if not self._reopened_after_deadline_pause(reason, seen_reopens):
            if not (reconnect and self._is_reconnectable(reason)):
                raise self._open_failure(reason, filesystem, translate) from reason
            outcome = self._reconnect(reason, seen_generation)
            if not outcome.resumed:
                raise self._reconnect_error(outcome, reason) from reason
        try:
            return first_stream_message(start(stub), allow_empty=allow_empty)
        except grpc.RpcError as exc:
            raise self._open_failure(exc, filesystem, translate) from exc

    def _open_failure(
        self,
        exc: grpc.RpcError,
        filesystem: bool,
        translate: Callable[[grpc.RpcError], Exception] | None,
    ) -> Exception:
        if translate is not None and not is_stream_reset(exc):
            return translate(exc)
        return self._stream_failure(exc, filesystem=filesystem)

    def _first_message_reminting(
        self, start: StreamStarter, stub: Any, allow_empty: bool
    ) -> tuple[Any, Any]:
        try:
            return first_stream_message(start(stub), allow_empty=allow_empty)
        except grpc.RpcError as exc:
            if not is_proxy_forbidden(exc):
                raise
        self._logger.info(
            "el proxy rechazó el token del sandbox %s al abrir un stream; reacuñando",
            self.sandbox_id,
        )
        self._refresher.refresh_all()
        return first_stream_message(start(stub), allow_empty=allow_empty)

    def _stream_failure(self, exc: grpc.RpcError, *, filesystem: bool = False) -> Exception:
        """Clasificación de M2 para los cortes que no reconectan (`Read` a
        mitad de fichero, o cuando el sondeo de reconexión ya falló): un
        reset se clasifica sondeando `Health` (5 s) y, si no responde, con un
        `get-microvm`; cualquier otro status sigue la tabla unaria."""
        if not is_stream_reset(exc):
            return translate_rpc_error(exc, filesystem=filesystem)
        health_ok = self._health_answers()
        state = None if health_ok else self._state_after_reset()
        return stream_failure_exception(exc, health_ok=health_ok, state=state)

    def _health_answers(self) -> bool:
        try:
            return self._probe_health(STREAM_PROBE_TIMEOUT_SECONDS) is not None
        except Exception:
            self._logger.debug("Health no respondió tras un corte de stream", exc_info=True)
            return False

    def _state_after_reset(self) -> str | None:
        try:
            self._info = self._control_plane.get_microvm(self.sandbox_id)
        except SandboxNotFoundException:
            return "TERMINATED"
        except SandboxException:
            return None
        return self._info.state

    def _probe_health(self, timeout: float) -> health_pb2.HealthResponse | None:
        """None significa "aún no alcanzable" (UNAVAILABLE / 502 / timeout)."""
        try:
            response = self._call_unary_once(
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

    def _wait_until_ready(
        self, *, terminate_on_failure: bool, readiness: type[ReadinessPoll] = ReadinessPoll
    ) -> health_pb2.HealthResponse:
        """Listo significa `agent_ready` y `kernel_ready`: el sidecar arrancó y
        el kernel por defecto ya fue rotado tras `/run`."""
        poll = readiness(timeout=self._ready_timeout)
        while True:
            response = self._probe_health(poll.rpc_timeout())
            if response is not None and health_ready(response):
                self._record_health(response)
                self._readiness_health = health_from_proto(response)
                return response
            if poll.should_check_state():
                self._fail_if_terminal()
            if poll.timed_out():
                raise self._not_ready(terminate_on_failure)
            time.sleep(poll.next_delay())

    def _record_health(self, response: health_pb2.HealthResponse) -> None:
        """Todo `Health` pasa por aquí: guarda los metadatos, fija la
        generación de resume, da por terminada la pausa pendiente, despierta a
        los pollers dormidos y avisa, una vez por generación nueva, de un
        kernel reiniciado o de un desfase de reloj mayor que 5 s."""
        self._metadata = metadata_from_health(response)
        self._guest = guest_facts_from_health(response)
        if self._ready_uptime_ms is None:
            self._ready_uptime_ms = int(response.uptime_ms)
        self._warn_hardening(response)
        self._record_lifecycle(lifecycle_from_proto(response))
        generation = int(response.resume_generation)
        if generation == self._resume_generation:
            return
        with self._resumed:
            self._resume_generation = generation
            self._paused = False
            self._resumed.notify_all()
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
        """Todo plazo leído (de `Health` o de `SetTimeout`) rearma el
        disparador del modo `pause` (design D7)."""
        self._lifecycle = lifecycle
        self._deadline_trigger.arm(pause_trigger_delay(lifecycle, unix_ms_now()))

    def _refresh_health(self) -> None:
        response = self._probe_health(min(ReadinessPoll.MAX_RPC_TIMEOUT, self._request_timeout))
        if response is not None:
            self._record_health(response)

    def _send_set_timeout(
        self, request: TimeoutRequest, *, request_timeout: float | None, reopen: bool = True
    ) -> None:
        timeout = self._resolve_request_timeout(request_timeout)
        try:
            state = self._call_unary(
                lambda: self._lifecycle_stub.SetTimeout(request.to_proto(), timeout=timeout),
                reopen=reopen,
            )
        except grpc.RpcError as exc:
            raise translate_set_timeout_error(exc, request) from exc
        self._record_lifecycle(lifecycle_from_state(state))

    def _extend_after_readiness(
        self, requested: int | None, *, request_timeout: float | None
    ) -> None:
        """Lo que `connect()` y `resume()` mandan tras la readiness, bien
        dentro de los 30 s de gracia de un sandbox reanudado tras su plazo."""
        request = connect_extension(self._lifecycle, requested, unix_ms_now())
        if request is not None:
            self._send_set_timeout(request, request_timeout=request_timeout)

    def _on_deadline(self) -> None:
        """El disparador del modo `pause`: `rayd` sigue siendo la fuente de
        verdad. Nunca sondea `Health` si `get-microvm` no dice `RUNNING` (lo
        despertaría) y sólo suspende si `Health` dice `expired`. Cualquier
        fallo se registra y se traga: la política de idle es el respaldo."""
        try:
            self._suspend_if_expired()
        except Exception as exc:
            self._logger.warning(
                "sandbox %s: el disparador del plazo falló (%s); la política de idle de la "
                "plataforma lo suspenderá",
                self.sandbox_id,
                type(exc).__name__,
            )

    def _suspend_if_expired(self) -> None:
        if self._closed:
            return
        info = self._control_plane.get_microvm(self.sandbox_id)
        if info.state != "RUNNING":
            return
        response = self._call_unary_once(
            lambda: self._health.Health(
                health_pb2.HealthRequest(), timeout=ReadinessPoll.MAX_RPC_TIMEOUT
            )
        )
        self._record_health(response)
        if self._lifecycle is not None and self._lifecycle.phase == "expired":
            self._suspend_for_deadline()

    def _suspend_for_deadline(self) -> None:
        """`suspend-microvm` (el bucket de 2 TPS del plano de control) sin la
        marca de pausa pendiente: la próxima petición auto-reanuda el sandbox
        igual que tras una suspensión por idle (AWS_API_NOTES.md Q40)."""
        generation = self._resume_generation
        suspended = self._control_plane.suspend_microvm(self.sandbox_id)
        if suspended:
            self._deadline_pause_generation = generation
        self._logger.info(
            "sandbox %s: plazo lógico vencido en modo pause; suspend-microvm %s",
            self.sandbox_id,
            "aceptado" if suspended else "ya no aplicaba",
        )

    def _reopened_after_deadline_pause(self, exc: grpc.RpcError, seen_reopens: int) -> bool:
        """Tras una suspensión por el plazo de este cliente, el primer
        `sandbox_timeout` de un sandbox ya reanudado aplica la regla del
        auto-resume con `SetTimeout` (`auto_resume_reopen`): `rayd` no la
        aplica si la congelación duró menos de 2 s.

        Una sola reapertura por suspensión, compartida: el lock serializa a
        los callers concurrentes y quien llega tras una reapertura hecha
        desde que empezó su llamada (`seen_reopens`) reintenta sin mandar
        otro `SetTimeout`. La marca sólo se consume cuando la
        `resume_generation` ya avanzó: un `sandbox_timeout` que llega antes
        de la congelación no la gasta. Si no toca o falla, el caller ve el
        error original."""
        if self._closed or not is_sandbox_timeout(exc):
            return False
        with self._reopen_lock:
            if self._reopens > seen_reopens:
                return True
            paused_generation = self._deadline_pause_generation
            if paused_generation is None:
                return False
            return self._reopen_after_deadline_pause(paused_generation)

    def _reopen_after_deadline_pause(self, paused_generation: int) -> bool:
        """El cuerpo de la reapertura, bajo `_reopen_lock`."""
        try:
            self._refresh_health()
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
            self._send_set_timeout(request, request_timeout=None, reopen=False)
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
        """Borra la marca sólo si sigue siendo la de esta suspensión: una
        suspensión posterior del disparador pone la suya."""
        if self._deadline_pause_generation == paused_generation:
            self._deadline_pause_generation = None

    def _is_reconnectable(self, exc: grpc.RpcError) -> bool:
        return not self._closed and is_reconnectable(exc)

    def _reconnect_error(self, outcome: ReconnectOutcome, reason: Exception) -> Exception:
        return reason if outcome.error is None else outcome.error

    def _suspend_marking_paused(self) -> bool:
        """La marca se pone antes de `suspend-microvm`: el final `suspending`
        de un stream puede llegar antes de que la API responda."""
        was_paused = self._paused
        self._paused = True
        try:
            suspended = self._control_plane.suspend_microvm(self.sandbox_id)
        except BaseException:
            self._paused = was_paused
            raise
        if not suspended:
            self._paused = was_paused
        return suspended

    def _foreground_stream_wakes(self) -> bool:
        """Si un stream en foreground cortado (un `run` o un `run_code` en
        curso) puede ser la petición que despierte al VM: sí, salvo que este
        `Sandbox` tenga una pausa pendiente; entonces espera al `resume()`."""
        return not self._paused

    def _reconnect(
        self, reason: Exception, seen_generation: int, *, wake: bool = True
    ) -> ReconnectOutcome:
        """Espera a que el agente vuelva tras un corte.

        Si otro hilo ya reconectó (la generación avanzó desde
        `seen_generation`) vuelve al instante. Con `wake` (un unario, un
        stream que el caller abre, un `run`/`run_code` en foreground sin
        pausa pendiente) sondea `Health` bajo el lock (un solo sondeo por
        sandbox) con `ReconnectPoll` hasta `agent_ready` y `kernel_ready` (y,
        si el motivo fue un `/suspend`, una generación nueva), parando en un
        estado terminal, en `SUSPENDED` sin auto-resume o al agotar
        `reconnect_timeout`; el propio sondeo es la petición que despierta a
        un VM con auto-resume (AWS_API_NOTES.md Q4). Sin `wake` (un handle en
        background, una PTY, un watch, o un stream en foreground cortado por
        un `pause()` de este `Sandbox`) el caller duerme fuera del lock
        mientras `get-microvm` diga `SUSPENDING|SUSPENDED`, sin tocar
        `Health` ni consumir `reconnect_timeout`: leer un handle no despierta
        un sandbox suspendido; el `resume()`, el auto-resume de otra llamada
        o el fin del VM lo despiertan. El presupuesto de `reconnect_timeout`
        empieza cuando el estado deja de ser suspendido.
        """
        while True:
            outcome = self._already_back(seen_generation)
            if outcome is None and not wake:
                outcome = self._sleep_while_suspended(reason, seen_generation)
            if outcome is None:
                outcome = self._poll_holding_the_lock(reason, seen_generation, wake)
            if outcome is not None:
                return outcome

    def _already_back(self, seen_generation: int) -> ReconnectOutcome | None:
        if self._resume_generation > seen_generation:
            return ReconnectOutcome(True, True, self._resume_generation)
        if self._closed:
            return self._failed_reconnect(closed_during_reconnect(self.sandbox_id))
        return None

    def _sleep_while_suspended(
        self, reason: Exception, seen_generation: int
    ) -> ReconnectOutcome | None:
        """Fase dormida: `get-microvm` cada `STATE_CHECK_INTERVAL` (o antes,
        si alguien registra una generación nueva o cierra el sandbox). El
        primer estado se lee tras `INITIAL_DELAY` para no leer un `RUNNING`
        rancio justo después del final `suspending`. `None` cuando el VM no
        está suspendido y toca sondear `Health`."""
        self._wait_for_resume(ReconnectPoll.INITIAL_DELAY, seen_generation)
        while True:
            outcome = self._already_back(seen_generation)
            if outcome is not None:
                return outcome
            failure = self._check_state(reason, wake=False)
            if failure is not None:
                return self._failed_reconnect(failure)
            if self._info.state not in SUSPENDED_STATES:
                return None
            self._wait_for_resume(ReconnectPoll.STATE_CHECK_INTERVAL, seen_generation)

    def _poll_holding_the_lock(
        self, reason: Exception, seen_generation: int, wake: bool
    ) -> ReconnectOutcome | None:
        with self._reconnect_lock:
            outcome = self._already_back(seen_generation)
            if outcome is not None:
                return outcome
            self._logger.info(
                "sandbox %s: stream/unario cortado (%s); esperando al agente hasta %g s",
                self.sandbox_id,
                type(reason).__name__,
                self._reconnect_timeout,
            )
            return self._poll_until_back(reason, seen_generation, wake)

    def _poll_until_back(
        self, reason: Exception, seen_generation: int, wake: bool
    ) -> ReconnectOutcome | None:
        """Sondeo activo de `Health`. Sin `wake` el estado se comprueba tras
        cada sonda fallida y `None` devuelve al caller a la fase dormida si el
        VM volvió a estar suspendido."""
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
                failure = self._check_state(reason, wake=True)
                if failure is not None:
                    return self._failed_reconnect(failure)
                state_read_after_cut = True
            try:
                response = self._probe_health(poll.rpc_timeout())
            except Exception as exc:
                return self._failed_reconnect(self._probe_failure(exc))
            running = state_read_after_cut and self._info.state == "RUNNING"
            if health_reconnected(
                response, seen_generation=seen_generation, suspending=suspending, running=running
            ):
                return self._reconnected(response, seen_generation, poll.elapsed())
            if not wake:
                failure = self._check_state(reason, wake=False)
                if failure is not None:
                    return self._failed_reconnect(failure)
                if self._info.state in SUSPENDED_STATES:
                    return None
            self._wait_for_resume(poll.next_delay(), seen_generation)

    def _wait_for_resume(self, timeout: float, seen_generation: int) -> None:
        """Duerme como mucho `timeout`; `_record_health` con una generación
        nueva o `close()` lo interrumpen."""
        with self._resumed:
            if self._resume_generation == seen_generation and not self._closed:
                self._resumed.wait(timeout)

    def _notify_resumed(self) -> None:
        with self._resumed:
            self._resumed.notify_all()

    def _probe_failure(self, exc: Exception) -> Exception:
        return closed_during_reconnect(self.sandbox_id) if self._closed else exc

    def _check_state(self, reason: Exception, *, wake: bool) -> Exception | None:
        """`get-microvm`: para en un estado terminal o, con `wake`, en
        `SUSPENDED` sin auto-resume. Un error transitorio del plano de
        control (throttling) no detiene el sondeo."""
        try:
            self._info = self._control_plane.get_microvm(self.sandbox_id)
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

    def _fail_if_terminal(self) -> None:
        self._info = self._control_plane.get_microvm(self.sandbox_id)
        if self._info.state in TERMINAL_STATES:
            raise terminated_during_boot_error(self._info)

    def _not_ready(self, terminate: bool) -> SandboxException:
        info: SandboxInfo | None
        try:
            info = self._control_plane.get_microvm(self.sandbox_id)
        except SandboxException:
            info = None
        terminated = False
        if terminate and (info is None or info.state not in TERMINAL_STATES):
            self._control_plane.terminate_microvm(self.sandbox_id)
            terminated = True
        return not_ready_error(info, ready_timeout=self._ready_timeout, terminated=terminated)

    # --------------------------------------------------------- context manager

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.kill()

    def __repr__(self) -> str:
        return f"Sandbox(sandbox_id={self.sandbox_id!r}, state={self._info.state!r})"
