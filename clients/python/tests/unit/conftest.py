"""Fixtures compartidas: un `rayd` falso en proceso y un plano de control con Stubber."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from concurrent import futures
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast

import boto3
import grpc
import grpc.aio
import pytest
from botocore.config import Config
from botocore.stub import Stubber

from rayito._aws import LambdaMicrovmsControlPlane, sandbox_info_from_response
from rayito._payload import encode_access_token
from rayito._transport import ProxyAuthPlugin, TransportSettings, metadata_dict
from rayito.v1 import (
    code_pb2_grpc,
    filesystem_pb2_grpc,
    health_pb2,
    health_pb2_grpc,
    process_pb2_grpc,
    pty_pb2_grpc,
)

from .fake_code import FakeCodeService
from .fake_filesystem import FakeFilesystemService
from .fake_process import FakeProcessService, installed_token_sha256, presented_token_sha256
from .fake_pty import FakePtyService

REGION = "us-east-1"
ACCOUNT_ID = "123456789012"
IMAGE_NAME = "rayito-base-2gb"
IMAGE_ARN = f"arn:aws:lambda:{REGION}:{ACCOUNT_ID}:microvm-image:{IMAGE_NAME}"
SANDBOX_ID = "microvm-00000000-0000-0000-0000-000000000001"
JWE = "eyJhbGciOiJkaXIiLCJlbmMiOiJBMjU2R0NNIn0..fake.jwe"
ACCESS_TOKEN_SECRET = b"unit-test-access-token-32-bytes!"
ACCESS_TOKEN = encode_access_token(ACCESS_TOKEN_SECRET)
STARTED_AT = datetime(2026, 9, 15, 14, 39, 2, tzinfo=UTC)


class FakeClock:
    """Reloj manual para token buckets y refreshers."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakeRpcError(grpc.RpcError):
    """Un `RpcError` con la forma que `grpc` da a los errores del proxy."""

    def __init__(self, code: grpc.StatusCode, *, details: str = "", debug: str = "") -> None:
        super().__init__(details)
        self._code = code
        self._details = details
        self._debug = debug

    def code(self) -> grpc.StatusCode:
        return self._code

    def details(self) -> str:
        return self._details

    def debug_error_string(self) -> str:
        return self._debug


METRICS_TIMESTAMP_UNIX_MS = 1_789_000_000_123
FAKE_SERVER_WORKERS = 16
DEADLINE_KEY = "x-test-time-remaining"


def token_sha256_for_tests() -> str:
    return installed_token_sha256(ACCESS_TOKEN)


@dataclass
class FakeRayd(health_pb2_grpc.HealthServiceServicer):
    """`HealthService` como lo implementa `rayd` en M4.

    `unavailable_calls` responde UNAVAILABLE las primeras N veces (502 del
    proxy mientras se restaura el snapshot); `not_ready_calls` responde
    `agent_ready=False` las siguientes N y `kernel_not_ready_calls` responde
    `kernel_ready=False` (sidecar arrancando o rotando el kernel por defecto)
    las siguientes N. `Metrics` exige `x-access-token` y lo verifica como
    `rayd`: decodifica base64url y compara el sha256 con el hash instalado
    desde el `runHookPayload`.
    """

    token_sha256: str = field(default_factory=token_sha256_for_tests)
    sandbox_id: str = SANDBOX_ID
    agent_version: str = "test"
    unavailable_calls: int = 0
    not_ready_calls: int = 0
    kernel_not_ready_calls: int = 0
    health_calls: list[dict[str, str]] = field(default_factory=list)
    metrics_calls: list[dict[str, str]] = field(default_factory=list)
    resume_generation: int = 0
    clock_offset_ms: int = 0
    kernel_state_lost: bool = False
    uptime_ms: int = 12_345
    metadata: dict[str, str] = field(default_factory=dict)
    imds_blocked: bool = False
    hook_anomalies: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def Health(self, request: Any, context: grpc.ServicerContext) -> health_pb2.HealthResponse:
        metadata = metadata_dict(context.invocation_metadata())
        with self.lock:
            self.health_calls.append(metadata)
            if self.unavailable_calls > 0:
                self.unavailable_calls -= 1
                context.abort(grpc.StatusCode.UNAVAILABLE, "snapshot restoring")
            ready = self.not_ready_calls <= 0
            if not ready:
                self.not_ready_calls -= 1
            kernel_ready = ready and self.kernel_not_ready_calls <= 0
            if ready and not kernel_ready:
                self.kernel_not_ready_calls -= 1
        return health_pb2.HealthResponse(
            agent_ready=ready,
            kernel_ready=kernel_ready,
            agent_version=self.agent_version,
            uptime_ms=self.uptime_ms,
            sandbox_id=self.sandbox_id,
            resume_generation=self.resume_generation,
            clock_offset_ms=self.clock_offset_ms,
            kernel_state_lost=self.kernel_state_lost,
            metadata=self.metadata,
            imds_blocked=self.imds_blocked,
            hook_anomalies=self.hook_anomalies,
        )

    def Metrics(self, request: Any, context: grpc.ServicerContext) -> health_pb2.MetricsResponse:
        metadata = metadata_dict(context.invocation_metadata())
        metadata[DEADLINE_KEY] = f"{context.time_remaining():.3f}"
        with self.lock:
            self.metrics_calls.append(metadata)
        if presented_token_sha256(metadata) != self.token_sha256:
            context.abort(grpc.StatusCode.UNAUTHENTICATED, "x-access-token ausente o inválido")
        return health_pb2.MetricsResponse(
            cpu_used_pct=12.5,
            mem_used_bytes=512 * 1024 * 1024,
            mem_total_bytes=2 * 1024 * 1024 * 1024,
            disk_used_bytes=1_000_000,
            disk_total_bytes=8_000_000,
            cpu_count=1,
            timestamp_unix_ms=METRICS_TIMESTAMP_UNIX_MS,
        )


@dataclass(frozen=True)
class RaydEndpoint:
    servicer: FakeRayd
    process: FakeProcessService
    filesystem: FakeFilesystemService
    code: FakeCodeService
    pty: FakePtyService
    host: str
    port: int

    @property
    def transport(self) -> TransportSettings:
        return TransportSettings(
            channel_credentials=grpc.local_channel_credentials(grpc.LocalConnectionType.LOCAL_TCP),
            port=self.port,
            options=(),
        )

    def suspend(self) -> None:
        """`/suspend` del `rayd` falso: cada servicio cierra sus streams con
        su forma (`EndEvent{suspending}`, `PtyExited{suspending}`,
        `UNAVAILABLE suspending`) y rechaza los nuevos con el phase gate."""
        self.process.suspend()
        self.pty.suspend()
        self.filesystem.suspend()
        self.code.suspend()

    def resume(self, *, clock_offset_ms: int = 0, kernel_state_lost: bool = False) -> None:
        """`/resume` del `rayd` falso: la generación avanza y la fase vuelve a
        aceptar streams; procesos, PTY, watches y celdas siguieron vivos."""
        with self.servicer.lock:
            self.servicer.resume_generation += 1
            self.servicer.clock_offset_ms = clock_offset_ms
            self.servicer.kernel_state_lost = kernel_state_lost
        self.process.resume()
        self.filesystem.resume()
        self.code.resume()

    def suspend_resume(
        self,
        unavailable_calls: int = 0,
        *,
        clock_offset_ms: int = 0,
        kernel_state_lost: bool = False,
    ) -> None:
        """Un ciclo suspend/resume visto por el cliente: los streams se
        cierran, `Health` responde `UNAVAILABLE` `unavailable_calls` veces (el
        proxy mientras el VM está congelado) y después con la generación
        nueva."""
        self.suspend()
        with self.servicer.lock:
            self.servicer.unavailable_calls = unavailable_calls
        self.resume(clock_offset_ms=clock_offset_ms, kernel_state_lost=kernel_state_lost)


def start_fake_rayd(servicer: FakeRayd | None = None) -> tuple[grpc.Server, RaydEndpoint]:
    """Arranca un `rayd` falso completo en loopback; el caller lo para."""
    servicer = servicer or FakeRayd()
    process = FakeProcessService(token_sha256=servicer.token_sha256)
    filesystem = FakeFilesystemService(token_sha256=servicer.token_sha256)
    code = FakeCodeService(token_sha256=servicer.token_sha256)
    pty = FakePtyService(token_sha256=servicer.token_sha256, processes=process)
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=FAKE_SERVER_WORKERS))
    health_pb2_grpc.add_HealthServiceServicer_to_server(servicer, server)
    process_pb2_grpc.add_ProcessServiceServicer_to_server(process, server)
    filesystem_pb2_grpc.add_FilesystemServiceServicer_to_server(filesystem, server)
    code_pb2_grpc.add_CodeServiceServicer_to_server(code, server)
    pty_pb2_grpc.add_PtyServiceServicer_to_server(pty, server)
    port = server.add_secure_port(
        "127.0.0.1:0", grpc.local_server_credentials(grpc.LocalConnectionType.LOCAL_TCP)
    )
    server.start()
    endpoint = RaydEndpoint(
        servicer=servicer,
        process=process,
        filesystem=filesystem,
        code=code,
        pty=pty,
        host="127.0.0.1",
        port=port,
    )
    return server, endpoint


@pytest.fixture
def fake_rayd() -> Iterator[RaydEndpoint]:
    """Un `rayd` falso con `HealthService`, `ProcessService`,
    `FilesystemService`, `CodeService` y `PtyService`; los streams de
    background, las PTY y los watches ocupan un worker cada uno, de ahí el
    pool holgado."""
    server, endpoint = start_fake_rayd()
    try:
        yield endpoint
    finally:
        server.stop(grace=None)


FakeRaydFactory = Callable[[dict[str, str]], RaydEndpoint]


@pytest.fixture
def fake_rayd_factory() -> Iterator[FakeRaydFactory]:
    """Levanta un `rayd` falso adicional por llamada, cada uno con sus propios
    metadatos en `Health` (los casos de `list(metadata=)` necesitan varios
    sandboxes con endpoints distintos); todos se paran al final del test."""
    servers: list[grpc.Server] = []

    def start(metadata: dict[str, str]) -> RaydEndpoint:
        server, endpoint = start_fake_rayd(FakeRayd(metadata=metadata))
        servers.append(server)
        return endpoint

    try:
        yield start
    finally:
        for server in servers:
            server.stop(grace=None)


class TrackedChannel:
    """Envuelve un canal `grpc` para registrar su `close()`."""

    def __init__(self, channel: grpc.Channel, closed: list[bool]) -> None:
        self._channel = channel
        self._closed = closed

    def close(self) -> None:
        self._closed.append(True)
        self._channel.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._channel, name)


class TrackedAioChannel:
    def __init__(self, channel: Any, closed: list[bool]) -> None:
        self._channel = channel
        self._closed = closed

    async def close(self, grace: float | None = None) -> None:
        self._closed.append(True)
        await self._channel.close(grace)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._channel, name)


@dataclass(frozen=True)
class TrackingTransport(TransportSettings):
    """`TransportSettings` de loopback que cuenta los canales que abre y los
    que se cierran: prueba que cada sonda de metadatos cierra su canal. Los
    endpoints falsos ya llevan `host:port`, así que `target` los deja tal cual."""

    opened: list[list[bool]] = field(default_factory=list)

    @classmethod
    def for_loopback(cls) -> TrackingTransport:
        return cls(
            channel_credentials=grpc.local_channel_credentials(grpc.LocalConnectionType.LOCAL_TCP),
            port=0,
            options=(),
        )

    def target(self, host: str) -> str:
        return host

    def open_channel(self, host: str, plugin: ProxyAuthPlugin) -> grpc.Channel:
        closed: list[bool] = []
        self.opened.append(closed)
        channel = grpc.secure_channel(self.target(host), self.credentials(plugin), options=[])
        return cast("grpc.Channel", TrackedChannel(channel, closed))

    def open_aio_channel(self, host: str, plugin: ProxyAuthPlugin) -> grpc.aio.Channel:
        closed: list[bool] = []
        self.opened.append(closed)
        channel = grpc.aio.secure_channel(self.target(host), self.credentials(plugin), options=[])
        return cast("grpc.aio.Channel", TrackedAioChannel(channel, closed))

    @property
    def open_count(self) -> int:
        return len(self.opened)

    @property
    def all_closed(self) -> bool:
        return all(closed for closed in self.opened)


def no_retry_client(service: str) -> Any:
    session = boto3.session.Session(
        region_name=REGION, aws_access_key_id="testing", aws_secret_access_key="testing"
    )
    return session.client(
        service, config=Config(retries={"total_max_attempts": 1, "mode": "standard"})
    )


@dataclass
class StubbedControlPlane:
    plane: LambdaMicrovmsControlPlane
    microvms: Stubber
    sts: Stubber
    clock: FakeClock


@pytest.fixture
def control_plane() -> Iterator[StubbedControlPlane]:
    microvms = no_retry_client("lambda-microvms")
    sts = no_retry_client("sts")
    clock = FakeClock()
    plane = LambdaMicrovmsControlPlane(microvms, sts_client=sts, clock=clock, sleep=clock.sleep)
    with Stubber(microvms) as microvms_stub, Stubber(sts) as sts_stub:
        yield StubbedControlPlane(plane=plane, microvms=microvms_stub, sts=sts_stub, clock=clock)
        microvms_stub.assert_no_pending_responses()
        sts_stub.assert_no_pending_responses()


def microvm_response(
    *,
    state: str = "PENDING",
    endpoint: str = "abc.lambda-microvm.us-east-1.on.aws",
    state_reason: str | None = None,
    idle: dict[str, Any] | None = None,
    maximum_duration: int = 3600,
    terminated_at: datetime | None = None,
) -> dict[str, Any]:
    response: dict[str, Any] = {
        "microvmId": SANDBOX_ID,
        "state": state,
        "endpoint": endpoint,
        "imageArn": IMAGE_ARN,
        "imageVersion": "1.0",
        "maximumDurationInSeconds": maximum_duration,
        "startedAt": STARTED_AT,
    }
    if state_reason is not None:
        response["stateReason"] = state_reason
    if idle is not None:
        response["idlePolicy"] = idle
    if terminated_at is not None:
        response["terminatedAt"] = terminated_at
    return response


def always_running(monkeypatch: pytest.MonkeyPatch, sandbox: Any, host: str) -> None:
    """Sustituye `get_microvm` del sandbox por una respuesta `RUNNING` fija:
    los handles en background consultan el estado antes de cada sonda de
    reconexión y el Stubber no admite un número variable de llamadas."""
    info = sandbox_info_from_response(microvm_response(endpoint=host, state="RUNNING"))
    monkeypatch.setattr(sandbox._control_plane, "get_microvm", lambda sandbox_id: info)


def auth_token_response(jwe: str = JWE) -> dict[str, Any]:
    return {"authToken": {"X-aws-proxy-auth": jwe}}


def list_item(sandbox_id: str, state: str) -> dict[str, Any]:
    return {
        "microvmId": sandbox_id,
        "state": state,
        "imageArn": IMAGE_ARN,
        "imageVersion": "1.0",
        "startedAt": STARTED_AT,
    }


def endpoint_of(endpoint: RaydEndpoint) -> str:
    """`host:port` de un `rayd` falso, tal como lo espera `TrackingTransport`."""
    return f"{endpoint.host}:{endpoint.port}"


def stub_metadata_probe(
    control_plane: StubbedControlPlane,
    sandbox_id: str,
    endpoint: RaydEndpoint | None,
    *,
    state: str = "RUNNING",
    jwe: str = JWE,
) -> None:
    """La secuencia `get-microvm → create-microvm-auth-token` de una sonda de
    metadatos; sin token cuando el estado no es `RUNNING` (no se sondea)."""
    response = microvm_response(state=state, endpoint=endpoint_of(endpoint) if endpoint else "x")
    response["microvmId"] = sandbox_id
    control_plane.microvms.add_response(
        "get_microvm", response, expected_params={"microvmIdentifier": sandbox_id}
    )
    if state != "RUNNING":
        return
    control_plane.microvms.add_response(
        "create_microvm_auth_token",
        auth_token_response(jwe),
        expected_params={
            "microvmIdentifier": sandbox_id,
            "expirationInMinutes": 60,
            "allowedPorts": [{"port": 8080}],
        },
    )
