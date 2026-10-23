"""Dobles de prueba de `AgentFleet` (ADR-029): un backend que simula los hosts de
los VMs (salud, ocupación, sesiones), una clase `Sandbox` con `create()`,
`connect()`, `list()` y `kill()` que reparte sandboxes con id propio, y un
transporte HTTP que contesta por VM. Nunca tocan red, `grpc` ni AWS.

Las dos formas (síncrona y asíncrona) comparten el estado: sólo cambia si cada
método es una corrutina."""

from __future__ import annotations

import asyncio
import builtins
import json
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from rayito._agent._fleet import AgentFleet, SandboxDirectory
from rayito._agent._fleet_async import AsyncAgentFleet, AsyncSandboxDirectory
from rayito._agent._project_base import AGENT_SERVE_TAG
from rayito._agent._project_service import ProjectService
from rayito._agent._project_service_async import AsyncProjectService
from rayito._agent._scaling_domain import ScalingOptions
from rayito._limits import AGENT_SERVICE_READY_MARKER_PATH
from rayito._models import HostAccess
from rayito._payload import generate_access_token
from rayito._secret_gateway._domain import GatewayStatus
from rayito.exceptions import InvalidArgumentException
from tests.unit.fake_agent_project import (
    ENDPOINT,
    GATEWAY_PORT,
    AsyncFakeAgent,
    AsyncFakeCommands,
    AsyncFakeFiles,
    AsyncFakeRefresher,
    AsyncScriptedTransport,
    FakeAgent,
    FakeCommands,
    FakeFiles,
    FakeRefresher,
    FakeToken,
    Scripted,
    ScriptedTransport,
    json_body,
    project,
    sse,
)

#: Ahora: el tope de un VM listado (`listed_cap`) se compara con el reloj real.
START = datetime.now(UTC).replace(microsecond=0)
ACCESS_TOKEN = "fake-fleet-token"
FLEET_TOKEN = generate_access_token()


def sandbox_id_of(number: int) -> str:
    return f"microvm-00000000-0000-0000-0000-{number:012d}"


@dataclass
class FleetHost:
    """El host de un VM: lo que contesta `/ping` y `/invocations`."""

    ping: str = "Healthy"
    invoke_status: int = 200
    ping_status: int = 200
    invocations: list[dict[str, Any]] = field(default_factory=list)
    busy_answers: int = 0
    pings: int = 0

    def respond(
        self, method: str, path: str, body: bytes | None, accept: str = "application/json"
    ) -> Scripted:
        if method == "GET" and path == "/ping":
            self.pings += 1
            return Scripted(self.ping_status, json_body({"status": self.ping}))
        payload = json.loads(body or b"{}")
        self.invocations.append(payload)
        if self.busy_answers > 0:
            self.busy_answers -= 1
            return Scripted(429, b"{}")
        if self.invoke_status != 200:
            return Scripted(self.invoke_status, b"{}")
        session = payload.get("session_id") or f"ses_{len(self.invocations):026d}"
        if accept == "text/event-stream":
            return Scripted(
                200,
                sse(
                    ("session", {"session_id": session}),
                    ("step_started", {"index": 1}),
                    ("text", {"text": f"respuesta a {payload['prompt']}"}),
                    ("done", {"session_id": session}),
                ),
            )
        return Scripted(
            200,
            json_body(
                {
                    "v": 1,
                    "text": f"respuesta a {payload['prompt']}",
                    "session_id": session,
                    "usage": {"input": 1, "output": 1},
                    "steps": 1,
                    "failed": None,
                }
            ),
        )


class FleetBackend:
    """Los hosts de todos los VMs, por id de sandbox."""

    def __init__(self) -> None:
        self.hosts: dict[str, FleetHost] = {}

    def host(self, sandbox_id: str) -> FleetHost:
        return self.hosts.setdefault(sandbox_id, FleetHost())

    def sandbox_of(self, host_access: HostAccess) -> str:
        return host_access.host.split(".", maxsplit=1)[0]

    def factory(self, host_access: HostAccess) -> Any:
        return FleetTransport(self.host(self.sandbox_of(host_access)))

    def async_factory(self, host_access: HostAccess) -> Any:
        return AsyncFleetTransport(self.host(self.sandbox_of(host_access)))


class FleetTransport(ScriptedTransport):
    def __init__(self, host: FleetHost) -> None:
        super().__init__()
        self._host = host

    def request(
        self, method: str, path: str, headers: Mapping[str, str], body: bytes | None, timeout: float
    ) -> Any:
        self.responses.append(self._host.respond(method, path, body, headers.get("Accept", "")))
        return self._next(method, path, headers, body, timeout)


class AsyncFleetTransport(AsyncScriptedTransport):
    def __init__(self, host: FleetHost) -> None:
        super().__init__()
        self._host = host

    async def request(  # type: ignore[override]
        self, method: str, path: str, headers: Mapping[str, str], body: bytes | None, timeout: float
    ) -> Any:
        self.responses.append(self._host.respond(method, path, body, headers.get("Accept", "")))
        return self._next(method, path, headers, body, timeout)


class FleetFiles(FakeFiles):
    """`exists()` del marcador de listo contesta si el host del VM arrancó."""

    def __init__(self, owner: FleetSandbox) -> None:
        super().__init__()
        self.owner = owner

    def exists(self, path: str) -> bool:
        if path == AGENT_SERVICE_READY_MARKER_PATH:
            return self.owner.served
        return super().exists(path)


class AsyncFleetFiles(AsyncFakeFiles):
    def __init__(self, owner: FleetSandbox) -> None:
        super().__init__()
        self.owner = owner

    async def exists(self, path: str) -> bool:  # type: ignore[override]
        if path == AGENT_SERVICE_READY_MARKER_PATH:
            return self.owner.served
        return FakeFiles.exists(self, path)


class FleetCommands(FakeCommands):
    def __init__(self, owner: FleetSandbox) -> None:
        super().__init__()
        self.owner = owner

    def run(self, cmd: str, **kwargs: Any) -> Any:
        if kwargs.get("tag") == AGENT_SERVE_TAG:
            self.owner.served = True
        return super().run(cmd, **kwargs)


class AsyncFleetCommands(AsyncFakeCommands):
    def __init__(self, owner: FleetSandbox) -> None:
        super().__init__()
        self.owner = owner

    async def run(self, cmd: str, **kwargs: Any) -> Any:  # type: ignore[override]
        if kwargs.get("tag") == AGENT_SERVE_TAG:
            self.owner.served = True
        return FakeCommands.run(self, cmd, **kwargs)


class FleetSandbox:
    """Un sandbox de doble con su propio id y su host. `served` dice si el
    host del agente arrancó (una plaza aparcada aún no). `managed` dice si
    se creó con plazo lógico (`max_lifetime`/`on_timeout`): sin él,
    `set_timeout` falla como en `rayd` (`lifecycle_unmanaged`)."""

    def __init__(
        self,
        number: int,
        *,
        asynchronous: bool,
        metadata: Mapping[str, str] | None = None,
        served: bool = True,
        state: str = "RUNNING",
        managed: bool = True,
    ) -> None:
        self.number = number
        self.managed = managed
        self.asynchronous = asynchronous
        self.sandbox_id = sandbox_id_of(number)
        self.access_token = ACCESS_TOKEN
        self.metadata = dict(metadata or {})
        self.served = served
        self.state = state
        self.gateways = {"bedrock": GatewayStatus(port=GATEWAY_PORT)}
        self.token = FakeToken()
        self._refresher = (AsyncFakeRefresher if asynchronous else FakeRefresher)(self.token)
        self.files: Any = AsyncFleetFiles(self) if asynchronous else FleetFiles(self)
        self.commands: Any = AsyncFleetCommands(self) if asynchronous else FleetCommands(self)
        self.agent = AsyncFakeAgent() if asynchronous else FakeAgent()
        self.kills = 0
        self.timeouts: list[int] = []
        self.closed = False
        self.kill_error: Exception | None = None
        self.configured: list[tuple[Any, ...]] = []
        self.configure_error: Exception | None = None

    def _host(self, port: int) -> HostAccess:
        return HostAccess(
            f"{self.sandbox_id}.{ENDPOINT}", port=port, token_provider=lambda: self.token.jwe
        )

    def _kill(self) -> bool:
        self.kills += 1
        self.state = "TERMINATED"
        if self.kill_error is not None:
            raise self.kill_error
        return True

    def close(self) -> None:
        self.closed = True

    def _configure(self, planned: Any, terminate_on_failure: bool) -> None:
        """`ConfigureSandbox` de doble: anota las secciones (las pasarelas que
        la flota abre en una plaza aparcada) o falla si se le pide."""
        if self.configure_error is not None:
            raise self.configure_error
        self.configured.append(tuple(planned))

    def _apply_configure_sections(
        self, planned: Any, *, timeout: float, terminate_on_failure: bool
    ) -> Any:
        if self.asynchronous:
            return self._async_call(lambda: self._configure(planned, terminate_on_failure))
        return self._configure(planned, terminate_on_failure)

    def get_host(self, port: int) -> Any:
        if self.asynchronous:
            return self._async_value(self._host(port))
        return self._host(port)

    def kill(self) -> Any:
        return self._async_call(self._kill) if self.asynchronous else self._kill()

    def set_timeout(self, timeout: int) -> Any:
        if not self.managed:
            error = InvalidArgumentException("set_timeout no aplica: el sandbox no tiene plazo")
            if self.asynchronous:
                return self._async_raise(error)
            raise error
        self.timeouts.append(timeout)
        return self._async_value(None) if self.asynchronous else None

    @staticmethod
    async def _async_raise(error: Exception) -> Any:
        raise error

    def pause(self, **kwargs: Any) -> Any:
        self.state = "SUSPENDED"
        return self._async_value(True) if self.asynchronous else True

    @staticmethod
    async def _async_value(value: Any) -> Any:
        return value

    @staticmethod
    async def _async_call(call: Any) -> Any:
        return call()


@dataclass(frozen=True)
class ListedItem:
    sandbox_id: str
    state: str
    started_at: datetime
    metadata: Mapping[str, str] | None = None


class FleetSandboxClass:
    """`Sandbox` de doble: `create()` reparte sandboxes numerados, `connect()`
    devuelve uno existente, `list()` los vivos de la imagen y `kill()` termina
    uno por id. Lo que cada llamada recibió queda anotado."""

    def __init__(self, backend: FleetBackend, *, asynchronous: bool = False) -> None:
        self.backend = backend
        self.asynchronous = asynchronous
        self.sandboxes: dict[str, FleetSandbox] = {}
        self.created: list[tuple[str, dict[str, Any]]] = []
        self.connected: list[str] = []
        self.listed: list[dict[str, Any]] = []
        self.session_hits: dict[str, str] = {}
        self.create_error: Exception | None = None
        self._next = 1

    def add(
        self,
        *,
        served: bool = True,
        state: str = "RUNNING",
        foreign: bool = False,
        managed: bool = True,
    ) -> FleetSandbox:
        """Un VM que ya existía antes de la flota (reconexión, plaza aparcada);
        `foreign` es uno de la misma imagen que no es de un agente (sin las
        etiquetas del despliegue)."""
        sandbox = FleetSandbox(
            self._next,
            asynchronous=self.asynchronous,
            served=served,
            state=state,
            managed=managed,
            metadata={}
            if foreign
            else {"rayito.agent.deployment": "support-bot", "rayito.agent.port": "8088"},
        )
        self._next += 1
        self.sandboxes[sandbox.sandbox_id] = sandbox
        return sandbox

    def _create(self, template: str, kwargs: dict[str, Any]) -> FleetSandbox:
        self.created.append((template, kwargs))
        if self.create_error is not None:
            raise self.create_error
        managed = kwargs.get("max_lifetime") is not None or kwargs.get("on_timeout") is not None
        sandbox = self.add(served=False, managed=managed)
        sandbox.metadata = dict(kwargs.get("metadata") or {})
        return sandbox

    def _list(self, kwargs: dict[str, Any]) -> list[ListedItem]:
        self.listed.append(kwargs)
        wanted = kwargs.get("states")
        label = (kwargs.get("metadata") or {}).get("rayito.agent.session")
        if label is not None:
            hit = self.session_hits.get(label)
            return [ListedItem(hit, "RUNNING", START)] if hit else []
        probing = kwargs.get("metadata") is not None
        return [
            ListedItem(
                sbx.sandbox_id,
                sbx.state,
                START + timedelta(seconds=sbx.number),
                dict(sbx.metadata) if probing else None,
            )
            for sbx in self.sandboxes.values()
            if wanted is None or sbx.state in wanted
        ]

    def _connect(self, sandbox_id: str) -> FleetSandbox:
        self.connected.append(sandbox_id)
        sandbox = self.sandboxes[sandbox_id]
        sandbox.state = "RUNNING"
        return sandbox

    def _kill(self, sandbox_id: str) -> bool:
        sandbox = self.sandboxes.get(sandbox_id)
        if sandbox is None:
            return False
        sandbox._kill()
        return True

    def create(self, template: str, **kwargs: Any) -> Any:
        if self.asynchronous:
            return FleetSandbox._async_call(lambda: self._create(template, kwargs))
        return self._create(template, kwargs)

    def connect(self, sandbox_id: str, **kwargs: Any) -> Any:
        if self.asynchronous:
            return FleetSandbox._async_call(lambda: self._connect(sandbox_id))
        return self._connect(sandbox_id)

    def list(self, **kwargs: Any) -> Any:
        if self.asynchronous:
            return FleetSandbox._async_call(lambda: self._list(kwargs))
        return iter(self._list(kwargs))

    def kill(self, sandbox_id: str, **kwargs: Any) -> Any:
        if self.asynchronous:
            return FleetSandbox._async_call(lambda: self._kill(sandbox_id))
        return self._kill(sandbox_id)

    @property
    def live(self) -> builtins.list[FleetSandbox]:
        return [sbx for sbx in self.sandboxes.values() if sbx.state != "TERMINATED"]

    def served(self) -> Iterator[FleetSandbox]:
        return (sbx for sbx in self.live if sbx.served)


@dataclass
class FakeClock:
    """Reloj y espera de doble: dormir avanza el reloj."""

    now: float = 1000.0
    sleeps: list[float] = field(default_factory=list)

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    async def asleep(self, seconds: float) -> None:
        self.sleep(seconds)


@dataclass
class FakePoolSlot:
    sandbox_id: str


@dataclass
class FakePoolStats:
    ready: int
    slots: Sequence[FakePoolSlot] = ()


class FakePool:
    """Un `SandboxPool` de doble: `take()` entrega un sandbox aparcado (o
    crea uno) con las pasarelas pedidas; `stats()` informa de las plazas."""

    def __init__(self, config: Any, classes: FleetSandboxClass, **options: Any) -> None:
        self.config = config
        self.classes = classes
        self.options = options
        self.started = False
        self.closed: list[bool] = []
        self.taken: list[dict[str, Any]] = []
        self.managed = config.on_timeout is not None
        self.parked = [
            classes.add(served=False, state="SUSPENDED", managed=self.managed)
            for _ in range(config.size)
        ]

    def start(self) -> FakePool:
        self.started = True
        return self

    def take(self, **kwargs: Any) -> Any:
        self.taken.append(kwargs)
        sandbox = (
            self.parked.pop(0)
            if self.parked
            else self.classes.add(served=False, managed=self.managed)
        )
        sandbox.state = "RUNNING"
        return sandbox

    def stats(self) -> FakePoolStats:
        return FakePoolStats(
            ready=len(self.parked), slots=[FakePoolSlot(sbx.sandbox_id) for sbx in self.parked]
        )

    def close(self, *, drain: bool = True) -> None:
        self.closed.append(drain)
        if drain:
            for sandbox in self.parked:
                sandbox._kill()


class AsyncFakePool(FakePool):
    async def start(self) -> AsyncFakePool:  # type: ignore[override]
        self.started = True
        return self

    async def take(self, **kwargs: Any) -> Any:
        return FakePool.take(self, **kwargs)

    async def close(self, *, drain: bool = True) -> None:  # type: ignore[override]
        FakePool.close(self, drain=drain)


class FleetFlavor:
    """Las dos superficies de la flota con una API síncrona: `call()` espera
    la corrutina de la asíncrona."""

    def __init__(self, name: str) -> None:
        self.asynchronous = name == "async"
        self.backend = FleetBackend()
        self.classes = FleetSandboxClass(self.backend, asynchronous=self.asynchronous)
        self.clock = FakeClock()
        self.pools: list[FakePool] = []

    def call(self, result: Any) -> Any:
        return asyncio.run(result) if self.asynchronous else result

    def _pool_factory(self, config: Any, **options: Any) -> FakePool:
        pool_class = AsyncFakePool if self.asynchronous else FakePool
        pool = pool_class(config, self.classes, **options)
        self.pools.append(pool)
        return pool

    def service(self, the_project: Any) -> Any:
        """El `ProjectService` (o su espejo) sobre los sandboxes de doble."""
        factory = self.backend.async_factory if self.asynchronous else self.backend.factory
        if self.asynchronous:
            return AsyncProjectService(
                the_project,
                sandbox_class=self.classes,
                transport_factory=factory,
                sleep=self.clock.asleep,
                clock=self.clock,
            )
        return ProjectService(
            the_project,
            sandbox_class=self.classes,
            transport_factory=factory,
            sleep=self.clock.sleep,
            clock=self.clock,
        )

    def fleet(
        self,
        scaling: ScalingOptions | None = None,
        *,
        the_project: Any = None,
        **options: Any,
    ) -> Any:
        the_project = the_project or project()
        factory = self.backend.async_factory if self.asynchronous else self.backend.factory
        fleet_class: Any = AsyncAgentFleet if self.asynchronous else AgentFleet
        sleep: Any = self.clock.asleep if self.asynchronous else self.clock.sleep
        return fleet_class(
            the_project,
            access_token=FLEET_TOKEN,
            scaling=scaling,
            service=options.pop("service", self.service(the_project)),
            directory=options.pop("directory", self.classes_directory()),
            pool_factory=self._pool_factory,
            transport_factory=factory,
            clock=self.clock,
            sleep=sleep,
            **options,
        )

    def classes_directory(self) -> Any:
        directory_class = AsyncSandboxDirectory if self.asynchronous else SandboxDirectory
        return directory_class(sandbox_class=self.classes)

    def started(self, scaling: ScalingOptions | None = None, **options: Any) -> Any:
        fleet = self.fleet(scaling, **options)
        self.call(fleet.start())
        return fleet

    def stream_events(self, fleet: Any, prompt: str, session_id: str | None = None) -> Any:
        """Abre el stream, lo lee entero y devuelve (eventos, stream)."""
        if self.asynchronous:

            async def run() -> tuple[list[Any], Any]:
                stream = await fleet.stream(prompt, session_id)
                events = [event async for event in stream]
                return events, stream

            return asyncio.run(run())
        stream = fleet.stream(prompt, session_id)
        return list(stream), stream

    def open_stream(self, fleet: Any, prompt: str, session_id: str | None = None) -> Any:
        return (
            self.call(fleet.stream(prompt, session_id))
            if self.asynchronous
            else fleet.stream(prompt, session_id)
        )

    def close_stream(self, stream: Any) -> None:
        self.call(stream.close()) if self.asynchronous else stream.close()
