"""`build()`, `run()` y `deploy()` de un `AgentProject` (`_project_service.py`
y su espejo asíncrono) sobre un sandbox de doble: los argumentos de
`Sandbox.create()`, lo que `deploy()` escribe y arranca, la espera del
marcador de listo y la limpieza (un `kill()`, nunca dos, nunca ninguno)
ante cualquier fallo. Ninguna prueba llama a AWS."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from rayito._agent._deepagents import DeepAgents
from rayito._agent._deployment import AgentDeployment
from rayito._agent._deployment_async import AsyncAgentDeployment
from rayito._agent._project_base import (
    AGENT_HOST_CONFIG_PATH,
    AGENT_SERVE_TAG,
)
from rayito._agent._project_domain import ServeOptions
from rayito._agent._project_service import ProjectService
from rayito._agent._project_service_async import AsyncProjectService
from rayito._agent._recipes import AGENT_HOST_DIR
from rayito._agent._runtime import HostConfig, RuntimeFiles
from rayito._agent._template import AgentTemplate, AsyncAgentTemplate
from rayito._limits import (
    AGENT_SERVICE_PORT,
    AGENT_SERVICE_READY_MARKER_PATH,
    AGENT_SERVICE_READY_TIMEOUT_SECONDS,
    AGENT_STARTUP_DIAGNOSTICS_MAX_BYTES,
)
from rayito._models import ALL_TRAFFIC, IdlePolicy, NetworkPolicy
from rayito.exceptions import (
    AgentException,
    InvalidArgumentException,
    SandboxException,
    TimeoutException,
)
from tests.unit.fake_agent import FakeAgentRuntime
from tests.unit.fake_agent_project import (
    SANDBOX_ID,
    AsyncFakeAgent,
    AsyncFakeFiles,
    AsyncFakeSandbox,
    AsyncFakeSandboxClass,
    FakeAgent,
    FakeFiles,
    FakeSandbox,
    FakeSandboxClass,
    project,
)


class Flavor:
    """Las dos superficies del servicio con una API síncrona. `result` y
    `error` son lo que hace `agent.stream()`; `events`, lo que emite; `files` son los argumentos del
    doble de `files` (`ready_after`, `failed_code`); `kill_error`, lo que
    lanza `kill()`."""

    def __init__(
        self,
        name: str,
        *,
        result: Any = "resultado",
        error: Exception | None = None,
        files: dict[str, Any] | None = None,
        kill_error: Exception | None = None,
        events: tuple[Any, ...] = (),
    ) -> None:
        self.name = name
        self.asynchronous = name == "async"
        files_class, agent_class = (
            (AsyncFakeFiles, AsyncFakeAgent) if self.asynchronous else (FakeFiles, FakeAgent)
        )
        sandbox_class = AsyncFakeSandbox if self.asynchronous else FakeSandbox
        self.agent = agent_class(error=error, result=result, events=events)
        self.sandbox: FakeSandbox = sandbox_class(
            files=files_class(**(files or {})), agent=self.agent, kill_error=kill_error
        )
        self.klass: FakeSandboxClass = (
            AsyncFakeSandboxClass if self.asynchronous else FakeSandboxClass
        )(self.sandbox)
        self.ticks = 0.0
        self.sleeps: list[float] = []

    def _clock(self) -> float:
        return self.ticks

    def _sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.ticks += seconds

    async def _asleep(self, seconds: float) -> None:
        self._sleep(seconds)

    def service(self, the_project: Any) -> Any:
        if self.asynchronous:
            return AsyncProjectService(
                the_project, sandbox_class=self.klass, sleep=self._asleep, clock=self._clock
            )
        return ProjectService(
            the_project, sandbox_class=self.klass, sleep=self._sleep, clock=self._clock
        )

    def call(self, service: Any, name: str, *args: Any, **kwargs: Any) -> Any:
        result = getattr(service, name)(*args, **kwargs)
        return asyncio.run(result) if self.asynchronous else result


@pytest.fixture(params=["sync", "async"])
def flavor(request: pytest.FixtureRequest) -> Callable[..., Flavor]:
    return lambda **options: Flavor(request.param, **options)


def test_build_composes_the_template_of_the_project(
    flavor: Callable[..., Flavor], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    flav = flavor()
    seen: list[tuple[Any, dict[str, Any]]] = []

    def build_sync(self: AgentTemplate, **kwargs: Any) -> str:
        seen.append((self, kwargs))
        return "info"

    async def build_async(self: AsyncAgentTemplate, **kwargs: Any) -> str:
        seen.append((self, kwargs))
        return "info"

    monkeypatch.setattr(AgentTemplate, "build", build_sync)
    monkeypatch.setattr(AsyncAgentTemplate, "build", build_async)
    (tmp_path / "agent.py").write_text("x = 1\n")
    the_project = project(
        source=str(tmp_path), exclude=(".venv",), memory_mib=4096, template="imagen-propia"
    )
    info = flav.call(
        flav.service(the_project),
        "build",
        bucket="amzn-s3-demo-bucket",
        force=True,
        region="us-east-1",
    )
    assert info == "info"
    ((template, kwargs),) = seen
    assert isinstance(template, AsyncAgentTemplate if flav.asynchronous else AgentTemplate)
    assert (template.name, template.base, template.memory_mib) == (
        "imagen-propia",
        "rayito-base-caps",
        4096,
    )
    assert (template.app, template.app_exclude) == (str(tmp_path), (".venv",))
    assert template.runtimes == ("deepagents",)
    assert kwargs["bucket"] == "amzn-s3-demo-bucket" and kwargs["force"] is True
    assert kwargs["region"] == "us-east-1"
    assert flav.klass.created == []


def test_build_defaults_the_image_name_to_the_project_name(
    flavor: Callable[..., Flavor], monkeypatch: pytest.MonkeyPatch
) -> None:
    flav = flavor()
    names: list[str] = []
    monkeypatch.setattr(AgentTemplate, "build", lambda self, **kw: names.append(self.name))
    monkeypatch.setattr(AsyncAgentTemplate, "build", _async_recorder(names))
    flav.call(flav.service(project()), "build", bucket="b")
    assert names == ["support-bot"]


def _async_recorder(names: list[str]) -> Callable[..., Any]:
    async def build(self: AsyncAgentTemplate, **kwargs: Any) -> None:
        names.append(self.name)

    return build


def test_build_rejects_a_runtime_that_cannot_be_installed(flavor: Callable[..., Flavor]) -> None:
    flav = flavor()
    with pytest.raises(InvalidArgumentException, match="InstallableRuntime"):
        flav.call(flav.service(project(runtime=FakeAgentRuntime())), "build", bucket="b")
    assert flav.klass.created == []


def test_run_creates_runs_and_always_kills(flavor: Callable[..., Flavor]) -> None:
    flav = flavor(result="listo")
    agent = flav.agent
    the_project = project(egress=["arn:aws:lambda:us-east-1:123456789012:connector/c"])
    result = flav.call(
        flav.service(the_project),
        "run",
        "hola",
        session_id="s",
        model="m",
        reasoning=True,
        region="us-west-2",
    )
    assert result == "listo"
    ((template, kwargs),) = flav.klass.created
    assert template == "support-bot"
    assert kwargs["timeout"] == 660
    assert kwargs["allow_internet_access"] is False
    assert kwargs["egress"] == ["arn:aws:lambda:us-east-1:123456789012:connector/c"]
    assert set(kwargs["gateways"]) == {"bedrock"}
    assert kwargs["region"] == "us-west-2"
    assert "idle" not in kwargs and "metadata" not in kwargs
    ((prompt, run_kwargs),) = agent.calls
    assert prompt == "hola"
    assert run_kwargs["spec"] is the_project.spec
    assert run_kwargs["runtime"] == "deepagents"
    assert run_kwargs["limits"] is the_project.limits
    assert (run_kwargs["session_id"], run_kwargs["model"], run_kwargs["reasoning"]) == (
        "s",
        "m",
        True,
    )
    assert flav.sandbox.kills == 1


def test_run_forwards_every_event_to_on_event_and_closes_the_stream(
    flavor: Callable[..., Flavor],
) -> None:
    flav = flavor(result="listo", events=("uno", "dos"))
    seen: list[Any] = []
    result = flav.call(flav.service(project()), "run", "hola", on_event=seen.append)
    assert (result, seen) == ("listo", ["uno", "dos"])
    assert [stream.closed for stream in flav.agent.streams] == [True]
    assert flav.sandbox.kills == 1


def test_run_without_on_event_still_returns_the_result(flavor: Callable[..., Flavor]) -> None:
    flav = flavor(result="listo", events=("uno",))
    assert flav.call(flav.service(project()), "run", "hola") == "listo"


def test_run_kills_its_sandbox_when_the_agent_fails(flavor: Callable[..., Flavor]) -> None:
    error = AgentException("falló", reason="model_error")
    flav = flavor(error=error)
    with pytest.raises(AgentException) as raised:
        flav.call(flav.service(project()), "run", "hola")
    assert raised.value is error
    assert flav.sandbox.kills == 1


def test_a_failing_kill_never_hides_the_agent_error(flavor: Callable[..., Flavor]) -> None:
    error = AgentException("falló", reason="timeout")
    flav = flavor(error=error, kill_error=SandboxException("no se pudo"))
    with pytest.raises(AgentException) as raised:
        flav.call(flav.service(project()), "run", "hola")
    assert raised.value is error


def test_deploy_writes_the_host_and_starts_it(flavor: Callable[..., Flavor]) -> None:
    flav = flavor()
    the_project = project(
        serve=ServeOptions(
            timeout_seconds=1800.4,
            idle=IdlePolicy(max_idle_seconds=120),
            metadata={"team": "soporte"},
            max_concurrency=2,
            queue_max=3,
            queue_timeout_seconds=45,
        ),
        allow_internet_access=True,
    )
    deployment = flav.call(flav.service(the_project), "deploy")
    ((template, kwargs),) = flav.klass.created
    assert template == "support-bot"
    assert kwargs["timeout"] == 1801
    assert kwargs["idle"] == IdlePolicy(max_idle_seconds=120)
    assert kwargs["allow_internet_access"] is True
    assert kwargs["metadata"] == {
        "team": "soporte",
        "rayito.agent.deployment": "support-bot",
        "rayito.agent.port": str(AGENT_SERVICE_PORT),
    }
    written = {entry.path: entry for entry in flav.sandbox.files.written}
    host_json = json.loads(written[AGENT_HOST_CONFIG_PATH].data)
    assert host_json["driver"] == "deepagents"
    assert host_json["max_concurrency"] == 2
    assert (host_json["queue_max"], host_json["queue_timeout_seconds"]) == (3, 45)
    assert host_json["ready_marker"] == AGENT_SERVICE_READY_MARKER_PATH
    assert host_json["limits"]["max_steps"] == the_project.limits.max_steps
    assert host_json["options"]["config_path"] in written
    clear, (command, options) = flav.sandbox.commands.calls
    assert clear[0].startswith("rm -f ") and clear[0].count("/ready") == 2
    assert command.startswith("exec ")
    assert (
        f"'{AGENT_HOST_DIR}' serve '{AGENT_HOST_CONFIG_PATH}' --port {AGENT_SERVICE_PORT}"
        in command
    )
    assert options["background"] is True and options["timeout"] is None
    assert options["user"] == "user" and options["tag"] == AGENT_SERVE_TAG
    assert options["kill_tree"] is True
    assert options["envs"]["HOME"] == "/home/user"
    assert flav.sandbox.commands.handle.disconnected
    assert isinstance(deployment, AsyncAgentDeployment if flav.asynchronous else AgentDeployment)
    assert deployment.sandbox_id == SANDBOX_ID
    assert deployment.port == AGENT_SERVICE_PORT
    assert flav.sandbox.kills == 0


def test_run_and_deploy_hand_the_project_env_and_network_to_the_sandbox(
    flavor: Callable[..., Flavor],
) -> None:
    flav = flavor()
    policy = NetworkPolicy(allow_out=("api.example.com",), deny_out=(ALL_TRAFFIC,))
    the_project = project(env={"LOG_LEVEL": "debug"}, network=policy)
    flav.call(flav.service(the_project), "run", "hola")
    flav.call(flav.service(the_project), "deploy")
    (_, ran), (_, deployed) = flav.klass.created
    for kwargs in (ran, deployed):
        assert kwargs["envs"] == {"LOG_LEVEL": "debug"}
        assert kwargs["network"] == policy


def test_a_project_without_env_or_network_sends_neither(flavor: Callable[..., Flavor]) -> None:
    flav = flavor()
    flav.call(flav.service(project()), "deploy")
    ((_, kwargs),) = flav.klass.created
    assert "envs" not in kwargs and "network" not in kwargs


def test_deploy_hands_the_access_token_to_the_sandbox_only_when_given(
    flavor: Callable[..., Flavor],
) -> None:
    flav = flavor()
    flav.call(flav.service(project()), "deploy")
    flav.call(flav.service(project()), "deploy", access_token="dG9rZW4")
    (_, without), (_, given) = flav.klass.created
    assert "access_token" not in without
    assert given["access_token"] == "dG9rZW4"


def test_deploy_never_writes_a_secret_or_a_credential(flavor: Callable[..., Flavor]) -> None:
    flav = flavor()
    flav.call(flav.service(project()), "deploy")
    for entry in flav.sandbox.files.written:
        data = entry.data if isinstance(entry.data, bytes) else str(entry.data).encode()
        assert b"bedrock-key" not in data
        assert b"AKIA" not in data


def test_deploy_waits_for_the_ready_marker(flavor: Callable[..., Flavor]) -> None:
    flav = flavor(files={"ready_after": 3})
    flav.call(flav.service(project()), "deploy")
    assert flav.sandbox.files.ready_polls == 4
    assert len(flav.sleeps) == 3
    assert flav.sandbox.kills == 0


def test_deploy_kills_the_sandbox_when_the_host_never_gets_ready(
    flavor: Callable[..., Flavor],
) -> None:
    flav = flavor(files={"ready_after": None})
    with pytest.raises(TimeoutException):
        flav.call(flav.service(project()), "deploy")
    assert flav.sandbox.kills == 1
    assert flav.ticks >= AGENT_SERVICE_READY_TIMEOUT_SECONDS


def test_deploy_fails_fast_when_the_host_left_a_failed_marker(
    flavor: Callable[..., Flavor],
) -> None:
    flav = flavor(files={"ready_after": None, "failed_code": "ModuleNotFoundError"})
    with pytest.raises(SandboxException, match="ModuleNotFoundError"):
        flav.call(flav.service(project()), "deploy")
    assert flav.sandbox.kills == 1
    assert flav.ticks == 0


def test_a_failed_startup_shows_the_end_of_the_apps_stderr(
    flavor: Callable[..., Flavor],
) -> None:
    marker = "app_exited\nTraceback (most recent call last):\nModuleNotFoundError: agent\n"
    flav = flavor(files={"ready_after": None, "failed_marker": marker})
    with pytest.raises(SandboxException) as raised:
        flav.call(flav.service(project()), "deploy")
    message = str(raised.value)
    assert message.startswith("el host del agente no pudo arrancar (app_exited)")
    assert "ModuleNotFoundError: agent" in message
    assert flav.sandbox.kills == 1


def test_a_failed_marker_is_bounded_again_by_the_sdk(flavor: Callable[..., Flavor]) -> None:
    marker = "app_exited\n" + "inicio " * 3000 + "fin-visible"
    flav = flavor(files={"ready_after": None, "failed_marker": marker})
    with pytest.raises(SandboxException) as raised:
        flav.call(flav.service(project()), "deploy")
    message = str(raised.value)
    assert message.endswith("fin-visible")
    assert len(message.encode("utf-8")) < AGENT_STARTUP_DIAGNOSTICS_MAX_BYTES + 200


def test_keep_on_failure_leaves_the_sandbox_alive_and_names_it(
    flavor: Callable[..., Flavor],
) -> None:
    flav = flavor(files={"ready_after": None, "failed_marker": "app_exited\nboom\n"})
    with pytest.raises(SandboxException) as raised:
        flav.call(flav.service(project()), "deploy", access_token="dG9rZW4", keep_on_failure=True)
    assert flav.sandbox.kills == 0
    notes = " ".join(getattr(raised.value, "__notes__", []))
    assert SANDBOX_ID in notes
    assert "dG9rZW4" not in notes
    assert "Sandbox.kill" in notes


def test_keep_on_failure_needs_a_token_the_caller_knows(
    flavor: Callable[..., Flavor], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("RAYITO_ACCESS_TOKEN", raising=False)
    flav = flavor()
    with pytest.raises(InvalidArgumentException, match="access_token"):
        flav.call(flav.service(project()), "deploy", keep_on_failure=True)
    assert flav.klass.created == []
    monkeypatch.setenv("RAYITO_ACCESS_TOKEN", "dG9rZW4")
    flav.call(flav.service(project()), "deploy", keep_on_failure=True)
    assert len(flav.klass.created) == 1


def test_keep_on_failure_is_off_by_default_and_a_success_never_kills(
    flavor: Callable[..., Flavor],
) -> None:
    flav = flavor(files={"ready_after": None, "failed_code": "AgentFailure"})
    with pytest.raises(SandboxException):
        flav.call(flav.service(project()), "deploy")
    assert flav.sandbox.kills == 1
    ok = flavor()
    ok.call(ok.service(project()), "deploy", access_token="dG9rZW4", keep_on_failure=True)
    assert ok.sandbox.kills == 0


def test_keep_on_failure_still_kills_on_a_base_exception(flavor: Callable[..., Flavor]) -> None:
    flav = flavor()

    def interrupted(*_: Any, **__: Any) -> None:
        raise KeyboardInterrupt

    async def ainterrupted(*_: Any, **__: Any) -> None:
        interrupted()

    flav.sandbox.files.write_files = ainterrupted if flav.asynchronous else interrupted  # type: ignore[method-assign, assignment]
    with pytest.raises(KeyboardInterrupt):
        flav.call(flav.service(project()), "deploy", access_token="dG9rZW4", keep_on_failure=True)
    assert flav.sandbox.kills == 1


def test_deploy_kills_the_sandbox_on_any_step_failure(flavor: Callable[..., Flavor]) -> None:
    flav = flavor()

    def boom(*_: Any, **__: Any) -> None:
        raise SandboxException("falló la escritura")

    async def aboom(*_: Any, **__: Any) -> None:
        boom()

    flav.sandbox.files.write_files = aboom if flav.asynchronous else boom  # type: ignore[method-assign, assignment]
    with pytest.raises(SandboxException):
        flav.call(flav.service(project()), "deploy")
    assert flav.sandbox.kills == 1


def test_deploy_with_a_runtime_that_cannot_be_served_fails_before_any_call(
    flavor: Callable[..., Flavor],
) -> None:
    flav = flavor()
    with pytest.raises(InvalidArgumentException, match="ServableRuntime"):
        flav.call(flav.service(project(runtime=FakeAgentRuntime())), "deploy")
    assert flav.klass.created == []


def test_a_failing_create_leaves_nothing_to_kill(flavor: Callable[..., Flavor]) -> None:
    flav = flavor()
    flav.klass.error = SandboxException("sin capacidad")
    with pytest.raises(SandboxException):
        flav.call(flav.service(project()), "deploy")
    assert flav.sandbox.kills == 0


class RawApp(DeepAgents):
    """Un runtime servible que se expone sin traducir."""

    name = "raw-app"

    def host_config(self, spec: Any, *, wiring: Any, workdir: str) -> HostConfig:
        return HostConfig(
            driver="http-app",
            python="python3",
            files=RuntimeFiles(files=(), config_sha256="x"),
            options={"protocol": "raw", "port": 9000, "command": ["serve"]},
        )


def test_a_raw_app_is_labelled_and_cannot_be_invoked(flavor: Callable[..., Flavor]) -> None:
    flav = flavor()
    deployment = flav.call(flav.service(project(runtime=RawApp())), "deploy")
    ((_, kwargs),) = flav.klass.created
    assert kwargs["metadata"]["rayito.agent.protocol"] == "raw"
    assert deployment._raw is True


def test_zero_calls_until_an_operation_runs(flavor: Callable[..., Flavor]) -> None:
    flav = flavor()
    flav.service(project())
    assert flav.klass.created == [] and flav.sandbox.kills == 0
