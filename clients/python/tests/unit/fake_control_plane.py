"""Plano de control falso en memoria para los tests del pool.

A diferencia del Stubber (una cola fija de respuestas), es una máquina de
estados por MicroVM que sirve un número variable de llamadas: `run_microvm`
arranca un `rayd` falso por VM (con el `token_sha256` del `runHookPayload`
de esa plaza y su propio `sandbox_id` en `Health`), `suspend`/`resume`/
`terminate` mueven el estado, `list_microvms` refleja el mapa con el filtro
por defecto del SDK y cada operación pasa por un `TokenBucket` real sobre
un `FakeClock` (así los tests miden el ritmo de los buckets sin esperar).
Cada llamada queda en `calls` con su instante; `fail(operation, index,
outcome)` programa un fallo (una excepción o `False` para un conflicto) en
la llamada número `index` (desde 1) de esa operación.
"""

from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast

import grpc
import grpc.aio

from rayito._aws import LaunchRequest, PortSpec, TokenBucket
from rayito._limits import API_TPS, TERMINAL_STATES
from rayito._models import MicrovmListPage, SandboxInfo, SandboxListItem
from rayito._transport import ProxyAuthPlugin
from rayito.exceptions import SandboxNotFoundException

from .conftest import (
    ACCOUNT_ID,
    IMAGE_ARN,
    JWE,
    REGION,
    FakeClock,
    FakeRayd,
    RaydEndpoint,
    TrackedAioChannel,
    TrackedChannel,
    TrackingTransport,
    start_fake_rayd,
    token_sha256_for_tests,
)

FailureOutcome = Exception | bool
PAGE_TOKEN_PREFIX = "page-"


@dataclass(frozen=True)
class PageCall:
    """Los argumentos de una llamada a `list_microvms_page`."""

    image_arn: str | None
    image_version: str | None
    max_results: int
    next_token: str | None


@dataclass(frozen=True)
class FakeCall:
    operation: str
    sandbox_id: str | None
    at: float


@dataclass
class FakeMicrovm:
    sandbox_id: str
    state: str
    endpoint: str
    request: LaunchRequest
    started_at: datetime
    rayd: RaydEndpoint
    pending_gets: int = 0
    state_reason: str | None = None

    @property
    def token_sha256(self) -> str:
        return str(json.loads(self.request.run_hook_payload)["token_sha256"])


class FakeControlPlane:
    """`ControlPlane` en memoria; `close()` para los `rayd` falsos que arrancó."""

    def __init__(
        self,
        *,
        clock: FakeClock | None = None,
        now: Any = None,
        pending_gets: int = 0,
        region: str = REGION,
    ) -> None:
        self.clock = clock or FakeClock()
        self._now = now or (lambda: datetime.now(UTC))
        self._pending_gets = pending_gets
        self._region = region
        self.microvms: dict[str, FakeMicrovm] = {}
        self.calls: list[FakeCall] = []
        self._servers: list[grpc.Server] = []
        self._failures: dict[str, dict[int, FailureOutcome]] = {}
        self._holds: dict[str, int] = {}
        self._releases: dict[str, threading.Event] = {}
        self._counts: dict[str, int] = {}
        self._buckets = {
            operation: TokenBucket(rate, clock=self.clock, sleep=self.clock.sleep)
            for operation, rate in API_TPS.items()
        }
        self._mints = 0
        self._lock = threading.Lock()
        self.scripted_pages: list[tuple[SandboxListItem, ...]] | None = None
        self.page_requests: list[PageCall] = []

    # ------------------------------------------------------------- scripting

    def script_pages(self, *pages: Sequence[SandboxListItem]) -> None:
        """Lo que `list_microvms_page` sirve desde ahora: la página `i` responde
        al token `page-i` (la primera a `None`) con `nextToken` `page-{i+1}`
        salvo la última. Sin guion, las páginas salen del mapa de MicroVMs."""
        self.scripted_pages = [tuple(page) for page in pages]

    def add_listed_sandbox(
        self,
        *,
        metadata: dict[str, str] | None = None,
        state: str = "RUNNING",
        started_at: datetime | None = None,
    ) -> str:
        """Un MicroVM de `IMAGE_ARN` con su `rayd` falso (metadatos en `Health`)
        para los listados que sondean metadatos; no cuenta como `RunMicrovm`."""
        request = LaunchRequest(
            image_arn=IMAGE_ARN,
            maximum_duration_seconds=900,
            run_hook_payload=json.dumps({"token_sha256": token_sha256_for_tests()}),
            client_token=uuid.uuid4().hex,
            logging={"disabled": {}},
        )
        sandbox_id = f"microvm-{uuid.uuid4()}"
        server, rayd = start_fake_rayd(
            FakeRayd(sandbox_id=sandbox_id, metadata=dict(metadata or {}))
        )
        with self._lock:
            self._servers.append(server)
            self.microvms[sandbox_id] = FakeMicrovm(
                sandbox_id=sandbox_id,
                state=state,
                endpoint=f"{rayd.host}:{rayd.port}",
                request=request,
                started_at=started_at or self._now(),
                rayd=rayd,
            )
        return sandbox_id

    def fail(self, operation: str, index: int, outcome: FailureOutcome) -> None:
        """Programa la llamada número `index` (desde 1) de `operation`: una
        excepción se lanza, `False` se devuelve como conflicto."""
        self._failures.setdefault(operation, {})[index] = outcome

    def hold(self, operation: str, count: int) -> None:
        """Las siguientes `count` llamadas a `operation` se quedan bloqueadas
        (ya anotadas en `calls`) hasta `release(operation)`; las demás pasan."""
        with self._lock:
            self._holds[operation] = count
            self._releases[operation] = threading.Event()

    def release(self, operation: str) -> None:
        with self._lock:
            event = self._releases.get(operation)
        if event is not None:
            event.set()

    def release_all(self) -> None:
        with self._lock:
            events = list(self._releases.values())
        for event in events:
            event.set()

    def set_state(self, sandbox_id: str, state: str, *, reason: str | None = None) -> None:
        vm = self.microvms[sandbox_id]
        vm.state = state
        vm.state_reason = reason

    def calls_to(self, operation: str) -> list[FakeCall]:
        return [call for call in self.calls if call.operation == operation]

    def timestamps(self, operation: str) -> list[float]:
        return [call.at for call in self.calls_to(operation)]

    def close(self) -> None:
        self.release_all()
        for server in self._servers:
            server.stop(grace=None)
        self._servers.clear()

    @property
    def live_ids(self) -> list[str]:
        return [vm.sandbox_id for vm in self.microvms.values() if vm.state not in TERMINAL_STATES]

    # ------------------------------------------------------------ ControlPlane

    @property
    def region(self) -> str:
        return self._region

    def resolve_template_arn(self, template: str) -> str:
        if template.startswith("arn:"):
            return template
        return f"arn:aws:lambda:{self._region}:{ACCOUNT_ID}:microvm-image:{template}"

    def run_microvm(self, request: LaunchRequest) -> SandboxInfo:
        self._enter("RunMicrovm", None)
        token_sha256 = str(json.loads(request.run_hook_payload)["token_sha256"])
        sandbox_id = f"microvm-{uuid.uuid4()}"
        server, rayd = start_fake_rayd(FakeRayd(token_sha256=token_sha256, sandbox_id=sandbox_id))
        vm = FakeMicrovm(
            sandbox_id=sandbox_id,
            state="PENDING" if self._pending_gets > 0 else "RUNNING",
            endpoint=f"{rayd.host}:{rayd.port}",
            request=request,
            started_at=self._now(),
            rayd=rayd,
            pending_gets=self._pending_gets,
        )
        with self._lock:
            self._servers.append(server)
            self.microvms[sandbox_id] = vm
        return self._info(vm)

    def get_microvm(self, sandbox_id: str) -> SandboxInfo:
        self._enter("GetMicrovm", sandbox_id)
        vm = self._require(sandbox_id)
        with self._lock:
            if vm.state == "PENDING":
                vm.pending_gets -= 1
                if vm.pending_gets <= 0:
                    vm.state = "RUNNING"
        return self._info(vm)

    def list_microvms(
        self,
        *,
        image_arn: str | None = None,
        image_version: str | None = None,
        states: Iterable[str] | None = None,
    ) -> Iterator[SandboxListItem]:
        self._enter("ListMicrovms", None)
        wanted = frozenset(states) if states is not None else None
        for vm in list(self.microvms.values()):
            if image_arn is not None and vm.request.image_arn != image_arn:
                continue
            if wanted is None and vm.state in TERMINAL_STATES:
                continue
            if wanted is not None and vm.state not in wanted:
                continue
            yield self._list_item(vm)

    def list_microvms_page(
        self,
        *,
        image_arn: str | None,
        image_version: str | None,
        max_results: int,
        next_token: str | None,
    ) -> MicrovmListPage:
        """Una página como la de AWS: filtra imagen y versión en servidor, nunca
        por estado. Sirve el guion de `script_pages` o, sin guion, el mapa de
        MicroVMs troceado en páginas de `max_results`."""
        self._enter("ListMicrovms", None)
        self.page_requests.append(PageCall(image_arn, image_version, max_results, next_token))
        pages = self._listing_pages(max_results)
        index = 0 if next_token is None else int(next_token.removeprefix(PAGE_TOKEN_PREFIX))
        items = tuple(
            item
            for item in pages[index]
            if (image_arn is None or item.template == image_arn)
            and (image_version is None or item.template_version == image_version)
        )
        following = f"{PAGE_TOKEN_PREFIX}{index + 1}" if index + 1 < len(pages) else None
        return MicrovmListPage(items=items, next_token=following)

    def _listing_pages(self, max_results: int) -> list[tuple[SandboxListItem, ...]]:
        if self.scripted_pages is not None:
            return self.scripted_pages or [()]
        items = [self._list_item(vm) for vm in list(self.microvms.values())]
        chunks = [
            tuple(items[start : start + max_results]) for start in range(0, len(items), max_results)
        ]
        return chunks or [()]

    def _list_item(self, vm: FakeMicrovm) -> SandboxListItem:
        return SandboxListItem(
            sandbox_id=vm.sandbox_id,
            state=vm.state,
            template=vm.request.image_arn,
            template_version=vm.request.image_version or "1.0",
            started_at=vm.started_at,
        )

    def terminate_microvm(self, sandbox_id: str) -> bool:
        outcome = self._enter("TerminateMicrovm", sandbox_id)
        if outcome is False:
            return False
        vm = self.microvms.get(sandbox_id)
        if vm is None:
            return False
        vm.state = "TERMINATED"
        vm.state_reason = vm.state_reason or "Success."
        return True

    def suspend_microvm(self, sandbox_id: str) -> bool:
        outcome = self._enter("SuspendMicrovm", sandbox_id)
        if outcome is False:
            return False
        vm = self._require(sandbox_id)
        if vm.state != "RUNNING":
            return False
        vm.state = "SUSPENDED"
        return True

    def resume_microvm(self, sandbox_id: str) -> bool:
        outcome = self._enter("ResumeMicrovm", sandbox_id)
        if outcome is False:
            return False
        vm = self._require(sandbox_id)
        if vm.state != "SUSPENDED":
            return False
        vm.state = "RUNNING"
        return True

    def create_auth_token(self, sandbox_id: str, ports: Sequence[PortSpec]) -> str:
        self._enter("CreateMicrovmAuthToken", sandbox_id)
        self._require(sandbox_id)
        with self._lock:
            self._mints += 1
            return f"{JWE}.{self._mints}"

    # --------------------------------------------------------------- internals

    def _enter(self, operation: str, sandbox_id: str | None) -> FailureOutcome | None:
        bucket = self._buckets.get(operation)
        if bucket is not None:
            bucket.acquire()
        with self._lock:
            self._counts[operation] = self._counts.get(operation, 0) + 1
            index = self._counts[operation]
            self.calls.append(FakeCall(operation, sandbox_id, self.clock()))
            outcome = self._failures.get(operation, {}).pop(index, None)
            gate = self._take_hold_locked(operation)
        if gate is not None:
            gate.wait()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def _take_hold_locked(self, operation: str) -> threading.Event | None:
        if self._holds.get(operation, 0) <= 0:
            return None
        self._holds[operation] -= 1
        return self._releases[operation]

    def _require(self, sandbox_id: str) -> FakeMicrovm:
        vm = self.microvms.get(sandbox_id)
        if vm is None:
            raise SandboxNotFoundException(f"MicroVM {sandbox_id} no existe")
        return vm

    def _info(self, vm: FakeMicrovm) -> SandboxInfo:
        return SandboxInfo(
            sandbox_id=vm.sandbox_id,
            state=vm.state,
            endpoint=vm.endpoint,
            template=vm.request.image_arn,
            template_version=vm.request.image_version or "1.0",
            started_at=vm.started_at,
            maximum_duration_seconds=vm.request.maximum_duration_seconds,
            state_reason=vm.state_reason,
            idle=vm.request.idle,
            execution_role_arn=vm.request.execution_role_arn,
            ingress=vm.request.ingress_connectors,
            egress=vm.request.egress_connectors,
        )


class CountingChannel(TrackedChannel):
    """Un canal que descuenta del contador de abiertos al cerrarse."""

    def __init__(self, channel: grpc.Channel, closed: list[bool], on_close: Any) -> None:
        super().__init__(channel, closed)
        self._on_close = on_close

    def close(self) -> None:
        if not self._closed:
            self._on_close()
        super().close()


class CountingAioChannel(TrackedAioChannel):
    def __init__(self, channel: Any, closed: list[bool], on_close: Any) -> None:
        super().__init__(channel, closed)
        self._on_close = on_close

    async def close(self, grace: float | None = None) -> None:
        if not self._closed:
            self._on_close()
        await super().close(grace)


@dataclass(frozen=True)
class PoolTransport(TrackingTransport):
    """`TrackingTransport` de loopback que además lleva cuántos canales hay
    abiertos a la vez y el máximo alcanzado (`fill_concurrency`)."""

    counters: dict[str, int] = field(default_factory=lambda: {"open": 0, "max": 0})
    counter_lock: threading.Lock = field(default_factory=threading.Lock)

    @classmethod
    def for_pool(cls) -> PoolTransport:
        return cls(
            channel_credentials=grpc.local_channel_credentials(grpc.LocalConnectionType.LOCAL_TCP),
            port=0,
            options=(),
        )

    def open_channel(self, host: str, plugin: ProxyAuthPlugin) -> grpc.Channel:
        closed: list[bool] = []
        self.opened.append(closed)
        with self.counter_lock:
            self.counters["open"] += 1
            self.counters["max"] = max(self.counters["max"], self.counters["open"])
        channel = grpc.secure_channel(self.target(host), self.credentials(plugin), options=[])
        return cast("grpc.Channel", CountingChannel(channel, closed, self._channel_closed))

    def open_aio_channel(self, host: str, plugin: ProxyAuthPlugin) -> grpc.aio.Channel:
        closed: list[bool] = []
        self.opened.append(closed)
        with self.counter_lock:
            self.counters["open"] += 1
            self.counters["max"] = max(self.counters["max"], self.counters["open"])
        channel = grpc.aio.secure_channel(self.target(host), self.credentials(plugin), options=[])
        return cast("grpc.aio.Channel", CountingAioChannel(channel, closed, self._channel_closed))

    def _channel_closed(self) -> None:
        with self.counter_lock:
            self.counters["open"] -= 1

    @property
    def max_open(self) -> int:
        return self.counters["max"]

    @property
    def currently_open(self) -> int:
        return self.counters["open"]


def max_calls_in_window(timestamps: Sequence[float], window: float) -> int:
    """Cuántas llamadas caben como mucho en una ventana deslizante de `window` s."""
    ordered = sorted(timestamps)
    best = 0
    start = 0
    for end, moment in enumerate(ordered):
        while moment - ordered[start] >= window:
            start += 1
        best = max(best, end - start + 1)
    return best
