"""`AgentProject` contra el `rayd` local (`make local-agent-up`, después
`make local-e2e`): `run()`, `deploy()`, `invoke()`, `stream()` y `stop()` con
el runtime a medida (`CustomRuntime`: un comando que habla v1 y una app HTTP)
y, con el proveedor falso de `make local-providers-up`, con OpenCode y
deepagents. El guest comparte el espacio de red del runner, así que el
cliente del despliegue habla con el host del agente por el loopback, sin TLS;
todo lo demás (el sandbox, las pasarelas, el host en modo servicio, los
límites) es el de producción.

Se prueba una vez, en Python: es el guest y el host, no el SDK
(`parity-checklist.md`). Los dos agentes de ejemplo están en el guest, en
`/opt/agents/app` (`dev/local/agent/apps`). Los secretos son valores
aleatorios en el Secrets Manager de Floci.
"""

from __future__ import annotations

import contextlib
import os
import secrets as stdlib_secrets
import threading
from collections.abc import Iterator
from typing import Any, Final

import boto3
import pytest

from rayito import AgentProject, SecretCache, SecretStore
from rayito._agent._deployment import AgentDeployment, HttpsTransport
from rayito._agent._events import AgentResult, Done, TextDelta
from rayito._agent._project_service import ProjectService
from rayito._limits import AGENT_SERVICE_PORT, TERMINAL_STATES
from rayito.exceptions import (
    AgentException,
    CommandExitException,
    InvalidArgumentException,
    SandboxNotFoundException,
    UnimplementedError,
)

from .conftest import LocalSettings, create_local_sandbox
from .guest import LocalGuestControlPlane
from .providers import PROMPT, Provider, random_key, secret_value_for

pytestmark = pytest.mark.local

custom = pytest.importorskip("rayito._agent._custom")
CustomRuntime = custom.CustomRuntime
HttpApp = custom.HttpApp

LOOPBACK: Final = "127.0.0.1"
GUEST_APP_DIR: Final = "/opt/agents/app"
COMMAND_AGENT: Final = f"{GUEST_APP_DIR}/command_agent.py"
HTTP_AGENT: Final = f"{GUEST_APP_DIR}/http_agent.py"
HTTP_AGENT_PORT: Final = 9100
HOST_FILE: Final = "/opt/agents/rayito/agent_host/__main__.py"
FAKE_UPSTREAM_ADMIN_VAR: Final = "RAYITO_LOCAL_FAKE_UPSTREAM_ADMIN"
#: El texto con el que contesta siempre el upstream falso.
FAKE_REPLY: Final = "rayito-fake-ok"
PRESET: Final = "openai"
RUNTIMES: Final = ("opencode", "deepagents")
SHELL_TIMEOUT_SECONDS: Final = 30
EGRESS_PROBE_TIMEOUT_SECONDS: Final = 5


class LocalSandboxes:
    """La clase de sandbox que `ProjectService` usa en local: `create()` con
    el plano de control, el transporte y la caché de secretos del guest."""

    def __init__(
        self,
        settings: LocalSettings,
        plane: LocalGuestControlPlane,
        template_arn: str,
        secret_cache: SecretCache,
        secret_name: str,
    ) -> None:
        self.secret_name = secret_name
        self._settings = settings
        self._plane = plane
        self._template_arn = template_arn
        self._secret_cache = secret_cache
        self.created: list[Any] = []

    def create(self, template: str, **kwargs: Any) -> Any:
        sandbox = create_local_sandbox(
            self._settings,
            self._plane,
            self._template_arn,
            secret_cache=self._secret_cache,
            **kwargs,
        )
        self.created.append(sandbox)
        return sandbox

    def kill_leftovers(self) -> None:
        for sandbox in self.created:
            with contextlib.suppress(SandboxNotFoundException, InvalidArgumentException):
                sandbox.kill()


def local_transport(access: Any) -> HttpsTransport:
    """El host escucha en el loopback compartido con el runner."""
    return HttpsTransport(LOOPBACK, port=AGENT_SERVICE_PORT, secure=False)


@pytest.fixture(scope="module")
def provider() -> Provider:
    entry = Provider(PRESET, "")
    return Provider(PRESET, secret_value_for(entry.header, random_key()))


@pytest.fixture(scope="module")
def sandboxes(
    local_settings: LocalSettings,
    control_plane: LocalGuestControlPlane,
    template_arn: str,
    aws_session: boto3.session.Session,
    provider: Provider,
) -> Iterator[LocalSandboxes]:
    store = SecretStore(session=aws_session)
    secret_name = f"local-project-{stdlib_secrets.token_hex(4)}"
    store.create(secret_name, provider.secret_value)
    factory = LocalSandboxes(
        local_settings, control_plane, template_arn, SecretCache(store=store), secret_name
    )
    try:
        probe = factory.create("probe", allow_internet_access=False)
    except UnimplementedError:
        store.destroy(secret_name)
        pytest.skip("el guest no aplica egress (sin CAP_NET_ADMIN): make local-agent-up")
    try:
        probe.commands.run(f"test -r {HOST_FILE}", timeout=SHELL_TIMEOUT_SECONDS)
    except CommandExitException:
        store.destroy(secret_name)
        pytest.skip("el guest no trae el host del agente: make local-agent-up")
    finally:
        with contextlib.suppress(SandboxNotFoundException):
            probe.kill()
    try:
        yield factory
    finally:
        factory.kill_leftovers()
        store.destroy(secret_name)


def build_project(
    provider: Provider, sandboxes: LocalSandboxes, runtime: Any, **overrides: Any
) -> AgentProject:
    return AgentProject(
        name="local-agent",
        runtime=runtime,
        spec=provider.spec(),
        gateways={provider.route: provider.gateway(sandboxes.secret_name)},
        **overrides,
    )


def service(project: AgentProject, sandboxes: LocalSandboxes) -> ProjectService:
    return ProjectService(project, sandbox_class=sandboxes, transport_factory=local_transport)


def command_runtime() -> Any:
    return CustomRuntime(command=["python3", COMMAND_AGENT])


def http_runtime() -> Any:
    return CustomRuntime(
        app=HttpApp(command=["python3", HTTP_AGENT, str(HTTP_AGENT_PORT)], port=HTTP_AGENT_PORT)
    )


def sh(deployment: AgentDeployment, cmd: str) -> tuple[int, str]:
    sandbox = deployment._sandbox
    try:
        result = sandbox.commands.run(cmd, timeout=SHELL_TIMEOUT_SECONDS)
    except CommandExitException as exc:
        return exc.exit_code, exc.stdout
    return result.exit_code, result.stdout


def test_a_command_agent_runs_through_run(provider: Provider, sandboxes: LocalSandboxes) -> None:
    result = service(build_project(provider, sandboxes, command_runtime()), sandboxes).run("hola")
    assert isinstance(result, AgentResult)
    assert (result.text, result.steps) == ("eco:hola", 1)
    assert result.usage.total == 5
    assert result.session_id


def test_run_kills_its_sandbox(provider: Provider, sandboxes: LocalSandboxes) -> None:
    before = len(sandboxes.created)
    service(build_project(provider, sandboxes, command_runtime()), sandboxes).run("hola")
    (sandbox,) = sandboxes.created[before:]
    assert sandbox.get_info().state in TERMINAL_STATES


@pytest.mark.parametrize(
    "make_runtime", [command_runtime, http_runtime], ids=["command", "http-app"]
)
def test_deploy_invoke_stream_and_stop(
    provider: Provider, sandboxes: LocalSandboxes, make_runtime: Any
) -> None:
    project = build_project(provider, sandboxes, make_runtime())
    deployment = service(project, sandboxes).deploy()
    try:
        assert deployment.status().state == "healthy"
        result = deployment.invoke("hola")
        assert result.text == "eco:hola"
        with deployment.stream("hola") as events:
            kinds = [event.type for event in events]
        assert kinds[-1] == "done"
        assert deployment.stream("hola").result().text == "eco:hola"
    finally:
        deployment.stop()
    assert deployment.status().state == "unavailable"
    assert deployment._sandbox.get_info().state in TERMINAL_STATES


def test_an_http_app_streams_its_deltas(provider: Provider, sandboxes: LocalSandboxes) -> None:
    deployment = service(build_project(provider, sandboxes, http_runtime()), sandboxes).deploy()
    try:
        with deployment.stream("stream") as events:
            collected = list(events)
        assert [e.text for e in collected if isinstance(e, TextDelta)] == ["eco:", "stream"]
        assert isinstance(collected[-1], Done)
    finally:
        deployment.stop()


def test_a_second_invocation_while_busy_is_refused(
    provider: Provider, sandboxes: LocalSandboxes
) -> None:
    runtime = CustomRuntime(
        command=["python3", "-c", "import time; time.sleep(30)"], protocol="text"
    )
    deployment = service(build_project(provider, sandboxes, runtime), sandboxes).deploy()
    first = threading.Thread(target=lambda: _quiet(deployment))
    try:
        first.start()
        assert _wait_busy(deployment)
        with pytest.raises(AgentException) as raised:
            deployment.invoke("segunda")
        assert raised.value.reason == "busy"
    finally:
        deployment.stop()
        first.join(60)


def _quiet(deployment: AgentDeployment) -> None:
    with contextlib.suppress(Exception):
        deployment.invoke("primera")


def _wait_busy(deployment: AgentDeployment) -> bool:
    for _ in range(100):
        if deployment.status().state == "busy":
            return True
        threading.Event().wait(0.1)
    return False


def test_the_deployment_never_holds_the_credential(
    provider: Provider, sandboxes: LocalSandboxes
) -> None:
    deployment = service(build_project(provider, sandboxes, command_runtime()), sandboxes).deploy()
    try:
        secret = provider.secret_value.removeprefix("Bearer ")
        code, found = sh(
            deployment,
            f"grep -rIl -- {secret} /home/user /opt/agents /proc/[0-9]*/environ 2>/dev/null; true",
        )
        assert (code, found.strip()) == (0, "")
        code, _ = sh(
            deployment,
            f"curl -sS -o /dev/null -m {EGRESS_PROBE_TIMEOUT_SECONDS} https://{provider.host}/",
        )
        assert code != 0
    finally:
        deployment.stop()


def _fake_upstream_enabled() -> bool:
    return bool(os.environ.get(FAKE_UPSTREAM_ADMIN_VAR))


@pytest.mark.skipif(not _fake_upstream_enabled(), reason="make local-providers-up")
@pytest.mark.parametrize("runtime", RUNTIMES)
def test_model_runtimes_answer_through_the_gateway(
    provider: Provider, sandboxes: LocalSandboxes, runtime: str
) -> None:
    project = build_project(provider, sandboxes, runtime)
    ran = service(project, sandboxes).run(PROMPT)
    assert FAKE_REPLY in ran.text
    deployment = service(project, sandboxes).deploy()
    try:
        assert FAKE_REPLY in deployment.invoke(PROMPT).text
        with deployment.stream(PROMPT) as events:
            assert isinstance(list(events)[-1], Done)
    finally:
        deployment.stop()
