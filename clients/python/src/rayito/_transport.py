"""Transporte gRPC hacia `rayd` a través del proxy de AWS.

Contrato (ARCHITECTURE.md, Capa 3): metadata en minúsculas en cada RPC
(`x-aws-proxy-auth`, `x-aws-proxy-port`, `x-aws-proxy-force-h2`,
`x-access-token`) inyectada por un `AuthMetadataPlugin`, así rotar el JWE
nunca reconstruye canales ni corta streams. Como máximo dos canales HTTP/2
por sandbox: el de unarios y el de streams largos. El backoff de reconexión
de grpc-core se acota a 2 s para que un canal cuya conexión murió con una
pausa vuelva a intentar en segundos, no en los 120 s por defecto.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

import grpc
import grpc.aio

from rayito._aws import PortSpec
from rayito._limits import (
    DEFAULT_PORT,
    ENDPOINT_TLS_PORT,
    TOKEN_REFRESH_AFTER_MINUTES,
    TOKEN_REFRESH_RETRY_SECONDS,
)
from rayito.exceptions import (
    AuthenticationException,
    DiskFullException,
    FileNotFoundException,
    InvalidArgumentException,
    NotFoundException,
    RateLimitException,
    SandboxException,
    SandboxStateException,
    TimeoutException,
)

module_logger = logging.getLogger("rayito.transport")

PROXY_AUTH_KEY: Final = "x-aws-proxy-auth"
PROXY_PORT_KEY: Final = "x-aws-proxy-port"
PROXY_FORCE_H2_KEY: Final = "x-aws-proxy-force-h2"
ACCESS_TOKEN_KEY: Final = "x-access-token"

PROXY_FORBIDDEN_MARKER: Final = "Received http2 header with status: 403"
STREAM_RESET_MARKERS: Final = (
    "RST_STREAM",
    "GOAWAY",
    "Received RST",
    "Socket closed",
    "Connection reset",
)
PHASE_GATE_DETAILS: Final = ("suspending", "terminating")
DISK_FULL_DETAILS: Final = ("disk_reserve", "disk_full")
KERNEL_GATE_PREFIX: Final = "kernel not ready"
SANDBOX_TIMEOUT_DETAIL: Final = "sandbox_timeout"
SANDBOX_TIMEOUT_MESSAGE: Final = "el sandbox alcanzó su timeout (sandbox_timeout)"
TOKEN_REFRESH_AFTER_SECONDS: Final = TOKEN_REFRESH_AFTER_MINUTES * 60

CHANNEL_OPTIONS: Final[tuple[tuple[str, int], ...]] = (
    ("grpc.keepalive_time_ms", 30_000),
    ("grpc.keepalive_timeout_ms", 10_000),
    ("grpc.keepalive_permit_without_calls", 1),
    ("grpc.max_receive_message_length", 64 * 1024 * 1024),
    ("grpc.initial_reconnect_backoff_ms", 500),
    ("grpc.min_reconnect_backoff_ms", 500),
    ("grpc.max_reconnect_backoff_ms", 2_000),
)

WallClock = Callable[[], float]
TokenMinter = Callable[[Sequence[PortSpec]], str]


@dataclass(frozen=True)
class ProxyToken:
    """Un JWE de `create-microvm-auth-token` y los puertos que cubre."""

    jwe: str
    ports: tuple[PortSpec, ...]
    minted_at: float

    def covers(self, port: int) -> bool:
        return any(spec.covers(port) for spec in self.ports)

    def refresh_due(self, now: float) -> bool:
        return now - self.minted_at >= TOKEN_REFRESH_AFTER_SECONDS

    def seconds_until_refresh(self, now: float) -> float:
        return max(0.0, self.minted_at + TOKEN_REFRESH_AFTER_SECONDS - now)


class TokenStore:
    """Tokens vivos de un sandbox, indexados por el conjunto de puertos que cubren."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tokens: list[ProxyToken] = []

    def put(self, token: ProxyToken) -> None:
        with self._lock:
            self._tokens = [t for t in self._tokens if t.ports != token.ports]
            self._tokens.append(token)

    def token_for(self, port: int) -> ProxyToken | None:
        with self._lock:
            for token in self._tokens:
                if token.covers(port):
                    return token
        return None

    def jwe_for(self, port: int) -> str | None:
        token = self.token_for(port)
        return token.jwe if token else None

    def tokens(self) -> tuple[ProxyToken, ...]:
        with self._lock:
            return tuple(self._tokens)

    def clear(self) -> None:
        with self._lock:
            self._tokens = []


MetadataPairs = tuple[tuple[str, str], ...]


class ProxyAuthPlugin(grpc.AuthMetadataPlugin):
    """Añade las cuatro cabeceras del proxy a cada RPC (unarios y streams).

    `access_token=None` omite `x-access-token`: sólo tiene sentido para
    `HealthService.Health`, el único RPC anónimo. `extra` son cabeceras del
    usuario ya validadas (`headers=` del shim de E2B) y van siempre después
    de las reservadas, así que nunca las sustituyen.
    """

    def __init__(
        self,
        store: TokenStore,
        *,
        port: int = DEFAULT_PORT,
        access_token: str | None,
        extra: MetadataPairs = (),
    ) -> None:
        self._store = store
        self._port = port
        self._access_token = access_token
        self._extra = extra

    def __call__(
        self,
        context: grpc.AuthMetadataContext,
        callback: grpc.AuthMetadataPluginCallback,
    ) -> None:
        jwe = self._store.jwe_for(self._port)
        if jwe is None:
            callback(
                (), AuthenticationException(f"no hay token del proxy para el puerto {self._port}")
            )
            return
        metadata: list[tuple[str, str]] = [
            (PROXY_AUTH_KEY, jwe),
            (PROXY_PORT_KEY, str(self._port)),
            (PROXY_FORCE_H2_KEY, "true"),
        ]
        if self._access_token:
            metadata.append((ACCESS_TOKEN_KEY, self._access_token))
        metadata.extend(self._extra)
        callback(tuple(metadata), None)


@dataclass(frozen=True)
class TransportSettings:
    """Cómo se abre el canal al MicroVM. El default es TLS al 443 del proxy.

    Los tests apuntan a un `rayd` falso en loopback con
    `grpc.local_channel_credentials()`. `extra_metadata` viaja en cada RPC
    detrás de las cabeceras reservadas (cada `ProxyAuthPlugin` construido
    con estos ajustes lo recibe) y `http_proxy` (`http://host:puerto`, sólo
    HTTP CONNECT) se pasa a grpc-core como `grpc.http_proxy`; ninguno de los
    dos se loguea.
    """

    channel_credentials: grpc.ChannelCredentials = field(
        default_factory=grpc.ssl_channel_credentials
    )
    port: int = ENDPOINT_TLS_PORT
    options: tuple[tuple[str, int | str], ...] = CHANNEL_OPTIONS
    extra_metadata: MetadataPairs = field(default=(), repr=False)
    http_proxy: str | None = field(default=None, repr=False)

    def target(self, host: str) -> str:
        return f"{host}:{self.port}"

    def credentials(self, plugin: ProxyAuthPlugin) -> grpc.ChannelCredentials:
        return grpc.composite_channel_credentials(
            self.channel_credentials, grpc.metadata_call_credentials(plugin)
        )

    def channel_options(self) -> list[tuple[str, int | str]]:
        proxy: list[tuple[str, int | str]] = (
            [] if self.http_proxy is None else [("grpc.http_proxy", self.http_proxy)]
        )
        return [*self.options, *proxy]

    def open_channel(self, host: str, plugin: ProxyAuthPlugin) -> grpc.Channel:
        return grpc.secure_channel(
            self.target(host), self.credentials(plugin), options=self.channel_options()
        )

    def open_aio_channel(self, host: str, plugin: ProxyAuthPlugin) -> grpc.aio.Channel:
        return grpc.aio.secure_channel(
            self.target(host), self.credentials(plugin), options=self.channel_options()
        )


class TokenRefresher:
    """Renueva los JWE del `TokenStore` a los 45 min (TTL duro de 60).

    `refresh_due` es síncrono y determinista (reloj inyectable) para poder
    testearlo; `start()` lo ejecuta en un hilo daemon que reintenta cada 60 s
    cuando AWS falla. El refresher no se para en pausa: la expiración del
    token es en reloj de pared.
    """

    def __init__(
        self,
        store: TokenStore,
        mint: TokenMinter,
        *,
        clock: WallClock = time.time,
        logger: logging.Logger | None = None,
    ) -> None:
        self._store = store
        self._mint = mint
        self._clock = clock
        self._logger = module_logger if logger is None else logger
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def store(self) -> TokenStore:
        return self._store

    def route_logs_to(self, logger: logging.Logger) -> None:
        """Los avisos de refresco fallido van a `logger` (el del sandbox)."""
        self._logger = logger

    def mint(self, ports: Sequence[PortSpec]) -> ProxyToken:
        token = ProxyToken(jwe=self._mint(ports), ports=tuple(ports), minted_at=self._clock())
        self._store.put(token)
        return token

    def ensure(self, port: int) -> ProxyToken:
        """Reutiliza un token que ya cubra el puerto o acuña uno de un solo puerto."""
        existing = self._store.token_for(port)
        return existing or self.mint((PortSpec.single(port),))

    def refresh_all(self) -> None:
        for token in self._store.tokens():
            self.mint(token.ports)

    def refresh_due(self, now: float | None = None) -> bool:
        """Reacuña los tokens vencidos; False si alguno falló (se reintenta)."""
        current = self._clock() if now is None else now
        ok = True
        for token in self._store.tokens():
            if not token.refresh_due(current):
                continue
            try:
                self.mint(token.ports)
            except Exception:
                self._logger.warning(
                    "no se pudo renovar el token del proxy; reintento en %s s",
                    TOKEN_REFRESH_RETRY_SECONDS,
                    exc_info=True,
                )
                ok = False
        return ok

    def seconds_until_next_refresh(self, now: float | None = None) -> float:
        current = self._clock() if now is None else now
        tokens = self._store.tokens()
        if not tokens:
            return float(TOKEN_REFRESH_AFTER_SECONDS)
        return min(token.seconds_until_refresh(current) for token in tokens)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="rayito-token-refresher", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)

    def _run(self) -> None:
        wait = self.seconds_until_next_refresh()
        while not self._stop.wait(timeout=wait):
            refreshed = self.refresh_due()
            wait = self.seconds_until_next_refresh() if refreshed else TOKEN_REFRESH_RETRY_SECONDS


AsyncSleeper = Callable[[float], Awaitable[None]]


class AsyncTokenRefresher:
    """La misma política que `TokenRefresher` como task de asyncio.

    Las llamadas a boto3 van por `asyncio.to_thread`; `sleep` es inyectable
    para testear el calendario sin esperar 45 minutos.
    """

    def __init__(self, refresher: TokenRefresher, *, sleep: AsyncSleeper = asyncio.sleep) -> None:
        self._refresher = refresher
        self._sleep = sleep
        self._task: asyncio.Task[None] | None = None

    @property
    def store(self) -> TokenStore:
        return self._refresher.store

    def route_logs_to(self, logger: logging.Logger) -> None:
        self._refresher.route_logs_to(logger)

    async def mint(self, ports: Sequence[PortSpec]) -> ProxyToken:
        return await asyncio.to_thread(self._refresher.mint, ports)

    async def ensure(self, port: int) -> ProxyToken:
        return await asyncio.to_thread(self._refresher.ensure, port)

    async def refresh_all(self) -> None:
        await asyncio.to_thread(self._refresher.refresh_all)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _run(self) -> None:
        wait = self._refresher.seconds_until_next_refresh()
        while True:
            await self._sleep(wait)
            refreshed = await asyncio.to_thread(self._refresher.refresh_due)
            wait = (
                self._refresher.seconds_until_next_refresh()
                if refreshed
                else TOKEN_REFRESH_RETRY_SECONDS
            )


def rpc_status(exc: grpc.RpcError) -> grpc.StatusCode | None:
    code = getattr(exc, "code", None)
    return code() if callable(code) else None


def rpc_details(exc: grpc.RpcError) -> str:
    details = getattr(exc, "details", None)
    text = details() if callable(details) else None
    return str(text or exc)


def rpc_debug_string(exc: grpc.RpcError) -> str:
    debug = getattr(exc, "debug_error_string", None)
    return str(debug() if callable(debug) else "")


def is_proxy_forbidden(exc: grpc.RpcError) -> bool:
    """Un 403 del proxy no lleva `grpc-status`: grpc lo presenta como
    PERMISSION_DENIED con el status HTTP en la cadena de depuración."""
    return rpc_status(
        exc
    ) is grpc.StatusCode.PERMISSION_DENIED and PROXY_FORBIDDEN_MARKER in rpc_debug_string(exc)


def is_not_yet_reachable(exc: grpc.RpcError) -> bool:
    """502/503 del proxy y timeouts del propio sondeo cuentan como "aún no"."""
    return rpc_status(exc) in (grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.DEADLINE_EXCEEDED)


def is_phase_gate(exc: grpc.RpcError) -> bool:
    """`rayd` responde `UNAVAILABLE` con `suspending`/`terminating` a `Start`
    y `Connect` mientras cambia de fase: el agente vive, no hay nada que
    sondear ni pid al que reengancharse."""
    return rpc_status(exc) is grpc.StatusCode.UNAVAILABLE and rpc_details(exc) in PHASE_GATE_DETAILS


def is_kernel_gate(exc: grpc.RpcError) -> bool:
    """`rayd` responde `UNAVAILABLE` con `kernel not ready: <motivo>` a
    `CodeService` mientras el sidecar arranca, se relanza o rota el kernel por
    defecto: el agente vive y no hay nada que sondear."""
    return rpc_status(exc) is grpc.StatusCode.UNAVAILABLE and rpc_details(exc).startswith(
        KERNEL_GATE_PREFIX
    )


def is_sandbox_timeout(exc: grpc.RpcError) -> bool:
    """`rayd` responde `FAILED_PRECONDITION sandbox_timeout` a todo RPC salvo
    `Health` y `SetTimeout` con el plazo lógico vencido (ADR-011), y cierra
    así los streams abiertos. No es `UNAVAILABLE` a propósito: el agente
    vive y reconectar no cambia nada."""
    return (
        rpc_status(exc) is grpc.StatusCode.FAILED_PRECONDITION
        and rpc_details(exc) == SANDBOX_TIMEOUT_DETAIL
    )


def is_stream_reset(exc: grpc.RpcError) -> bool:
    """Un stream cortado por debajo de gRPC: `UNAVAILABLE` (proxy o conexión
    caída) o `INTERNAL` con las marcas de reset HTTP/2 que grpc-core deja en
    `details`/`debug_error_string`. Se clasifica sondeando `Health`. Los
    `UNAVAILABLE` del phase gate y del kernel gate de `rayd` no son resets."""
    code = rpc_status(exc)
    if code is grpc.StatusCode.UNAVAILABLE:
        return not (is_phase_gate(exc) or is_kernel_gate(exc))
    if code is not grpc.StatusCode.INTERNAL:
        return False
    text = rpc_details(exc) + rpc_debug_string(exc)
    return any(marker in text for marker in STREAM_RESET_MARKERS)


def is_reconnectable(exc: grpc.RpcError) -> bool:
    """Lo que dispara el contrato de reconexión: un reset por debajo de gRPC
    (proxy 502/503, conexión perdida, `GOAWAY`) o el phase gate `suspending`/
    `terminating`. Nunca `DEADLINE_EXCEEDED` (el caller eligió ese plazo),
    nunca el kernel gate (el agente vive) ni un 403 del proxy (se reacuña)."""
    return is_stream_reset(exc) or is_phase_gate(exc)


def translate_rpc_error(exc: grpc.RpcError, *, filesystem: bool = False) -> Exception:
    """Tabla unaria. `CANCELLED` sólo lo produce el propio cliente (cerrar el
    canal o el stream), nunca un timeout, por eso no es `TimeoutException`."""
    code = rpc_status(exc)
    message = rpc_details(exc)
    if is_phase_gate(exc):
        return SandboxStateException(
            f"el sandbox está {message}; reintenta cuando vuelva a RUNNING", grpc_code=code
        )
    if is_kernel_gate(exc):
        return SandboxException(f"el kernel no está listo: {message}", grpc_code=code)
    if is_sandbox_timeout(exc):
        return TimeoutException(SANDBOX_TIMEOUT_MESSAGE, grpc_code=code)
    if code in (grpc.StatusCode.INVALID_ARGUMENT, grpc.StatusCode.FAILED_PRECONDITION):
        return InvalidArgumentException(message, grpc_code=code)
    if code is grpc.StatusCode.UNAUTHENTICATED:
        return AuthenticationException(message, grpc_code=code)
    if code is grpc.StatusCode.PERMISSION_DENIED:
        return AuthenticationException(
            message, grpc_code=code, proxy_rejected=is_proxy_forbidden(exc)
        )
    if code is grpc.StatusCode.NOT_FOUND:
        if filesystem:
            return FileNotFoundException(message, grpc_code=code)
        return NotFoundException(message, grpc_code=code)
    if code is grpc.StatusCode.OUT_OF_RANGE:
        return NotFoundException(message, grpc_code=code)
    if code is grpc.StatusCode.RESOURCE_EXHAUSTED:
        if message in DISK_FULL_DETAILS:
            return DiskFullException(message, grpc_code=code)
        return RateLimitException(message, grpc_code=code)
    if code is grpc.StatusCode.DEADLINE_EXCEEDED:
        return TimeoutException(message, grpc_code=code)
    if code is grpc.StatusCode.CANCELLED:
        return SandboxException(f"llamada cancelada por el cliente: {message}", grpc_code=code)
    if code is grpc.StatusCode.UNIMPLEMENTED:
        return InvalidArgumentException(message, grpc_code=code)
    return SandboxException(message, grpc_code=code)


def translate_stream_error(code: str, message: str, *, filesystem: bool = False) -> Exception:
    """Mapa de `StreamError.code` (conjunto cerrado de `common.proto`)."""
    if code == "not_found":
        return FileNotFoundException(message) if filesystem else NotFoundException(message)
    if code == "permission_denied":
        return AuthenticationException(message)
    if code == "deadline_exceeded":
        return TimeoutException(message)
    if code == SANDBOX_TIMEOUT_DETAIL:
        return TimeoutException(SANDBOX_TIMEOUT_MESSAGE)
    if code in ("unimplemented", "invalid_argument", "failed_precondition"):
        return InvalidArgumentException(message)
    if code == "resource_exhausted":
        if message in DISK_FULL_DETAILS:
            return DiskFullException(message)
        return RateLimitException(message)
    if code == "suspending":
        return SandboxStateException(message)
    if code == "output_truncated":
        return SandboxException(f"output_truncated: {message}")
    return SandboxException(f"{code}: {message}")


def metadata_dict(metadata: Any) -> dict[str, str]:
    """Convierte `invocation_metadata()` en dict (útil en tests y logs sin valores)."""
    return {str(key): str(value) for key, value in metadata}
