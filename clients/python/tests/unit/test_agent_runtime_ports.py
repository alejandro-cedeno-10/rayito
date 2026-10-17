"""Los puertos opcionales `InstallableRuntime` y `ServableRuntime`
(ADR-028): quién los implementa, qué entrega cada adaptador y que el
contrato de `AgentRuntime` no se mueve. Los vectores de configuración y
comandos de OpenCode y deepagents siguen siendo los de siempre
(`test_agent_opencode.py`, `test_agent_deepagents.py`)."""

from __future__ import annotations

from typing import Any

import pytest

from rayito import (
    AgentModel,
    AgentSpec,
    DeepAgents,
    DirectWiring,
    GatewayWiring,
    HostConfig,
    InstallTarget,
    InvalidArgumentException,
    McpRemote,
    UnimplementedError,
    is_installable,
    is_servable,
)
from rayito._agent._deepagents import DEEPAGENTS_CONFIG_PATH, DEEPAGENTS_PYTHON
from rayito._agent._hermes import HermesRuntime
from rayito._agent._opencode import OPENCODE_CONFIG_PATH, OpenCodeRuntime
from rayito._agent._runtime import (
    HOST_DRIVERS,
    RunRequest,
    RuntimeFiles,
    TemplateStep,
    require_installable,
    require_servable,
)
from rayito._agent._runtimes import AGENT_RUNTIMES, resolve_runtime, runtime_to_options
from rayito._agent._strands import StrandsRuntime
from rayito._limits import MODEL_CREDENTIAL_PLACEHOLDER

URLS = {"bedrock": "http://127.0.0.1:18001", "anthropic": "http://127.0.0.1:18002"}


def _bedrock() -> AgentSpec:
    return AgentSpec(
        model=AgentModel(
            provider="bedrock",
            id="us.anthropic.claude-haiku-4-5-20251001-v1:0",
            gateway="bedrock",
            region="us-east-1",
        )
    )


def _anthropic() -> AgentSpec:
    return AgentSpec(
        model=AgentModel(provider="anthropic", id="claude-haiku-4-5", gateway="anthropic")
    )


class TaskOnly:
    """Un runtime de 0.13: sólo el puerto `AgentRuntime`."""

    name = "task-only"

    def build_config(self, spec: AgentSpec, *, gateway_urls: Any, workdir: str) -> RuntimeFiles:
        return RuntimeFiles(files=(), config_sha256="0")

    def command(self, request: RunRequest) -> Any:
        return None

    def new_state(self) -> Any:
        return None

    def parse_line(self, line: bytes, state: Any) -> tuple[()]:
        return ()

    def finish(self, state: Any, exit_code: int) -> Any:
        return None

    def template_steps(self) -> tuple[()]:
        return ()

    def warmup_steps(self) -> tuple[()]:
        return ()


@pytest.mark.parametrize("runtime", [OpenCodeRuntime(), DeepAgents(), DeepAgents("pkg.mod:build")])
def test_the_two_existing_runtimes_implement_both_ports(runtime: object) -> None:
    assert is_installable(runtime)
    assert is_servable(runtime)


def test_a_task_only_runtime_still_resolves_but_has_neither_port() -> None:
    runtime = TaskOnly()
    assert resolve_runtime(runtime) is runtime
    assert not is_installable(runtime)
    assert not is_servable(runtime)
    with pytest.raises(InvalidArgumentException, match="InstallableRuntime"):
        require_installable(runtime)
    with pytest.raises(InvalidArgumentException, match="ServableRuntime"):
        require_servable(runtime)


def test_registry_names() -> None:
    assert isinstance(AGENT_RUNTIMES["opencode"], OpenCodeRuntime)
    langgraph = resolve_runtime("langgraph")
    assert isinstance(langgraph, DeepAgents)
    assert langgraph.require_entrypoint and langgraph.entrypoint is None
    assert resolve_runtime("deepagents") is not langgraph


@pytest.mark.parametrize("name", ["crewai"])
def test_unregistered_names_are_unimplemented_not_key_errors(name: str) -> None:
    with pytest.raises(UnimplementedError) as raised:
        resolve_runtime(name)
    assert f"runtime={name!r}" in str(raised.value)


def test_the_framework_runtimes_are_registered() -> None:
    assert isinstance(resolve_runtime("strands"), StrandsRuntime)
    assert getattr(resolve_runtime("strands-harness"), "harness", False) is True
    assert isinstance(resolve_runtime("hermes"), HermesRuntime)


def test_custom_is_not_a_registry_name() -> None:
    with pytest.raises(InvalidArgumentException, match="CustomRuntime"):
        resolve_runtime("custom")


def test_langgraph_needs_an_entrypoint_to_build_anything() -> None:
    runtime = resolve_runtime("langgraph")
    with pytest.raises(InvalidArgumentException, match="entrypoint"):
        runtime.build_config(_anthropic(), gateway_urls=URLS, workdir="/home/user")
    assert isinstance(runtime, DeepAgents)
    with pytest.raises(InvalidArgumentException, match="entrypoint"):
        runtime.host_config(_anthropic(), wiring=GatewayWiring(URLS), workdir="/home/user")


def test_runtime_to_options_names_the_registry_kind() -> None:
    assert runtime_to_options("opencode") == {"kind": "opencode"}
    assert runtime_to_options(OpenCodeRuntime()) == {"kind": "opencode"}
    assert runtime_to_options(DeepAgents()) == {"kind": "deepagents"}
    assert runtime_to_options(DeepAgents("pkg.mod:build")) == {
        "kind": "langgraph",
        "entrypoint": "pkg.mod:build",
    }
    with pytest.raises(InvalidArgumentException):
        runtime_to_options(TaskOnly())


def test_install_target_is_validated() -> None:
    assert InstallTarget().platform == "microvm"
    with pytest.raises(InvalidArgumentException):
        InstallTarget(platform="vm")  # type: ignore[arg-type]
    with pytest.raises(InvalidArgumentException):
        InstallTarget(install_dir="opt/agents")


def test_template_step_ops_are_closed() -> None:
    with pytest.raises(InvalidArgumentException):
        TemplateStep("exec")


def test_host_config_driver_is_a_closed_list() -> None:
    files = RuntimeFiles(files=(), config_sha256="0")
    assert "strands" in HOST_DRIVERS
    with pytest.raises(InvalidArgumentException):
        HostConfig(driver="bash", python="python3", files=files)


def test_opencode_host_config_matches_its_run_command() -> None:
    runtime = OpenCodeRuntime()
    spec = _bedrock()
    host = runtime.host_config(spec, wiring=GatewayWiring(URLS), workdir="/home/user")
    config = runtime.build_config(spec, gateway_urls=URLS, workdir="/home/user")
    command = runtime.command(RunRequest(spec=spec, prompt="hola", workdir="/home/user"))
    assert host.driver == "opencode"
    assert host.files == config
    assert host.envs == command.envs
    assert host.files.files[0].path == OPENCODE_CONFIG_PATH
    assert host.options == {"workdir": "/home/user"}


def test_deepagents_host_config_adds_the_standard_provider_variables() -> None:
    runtime = DeepAgents()
    spec = _bedrock()
    host = runtime.host_config(spec, wiring=GatewayWiring(URLS), workdir="/home/user")
    config = runtime.build_config(spec, gateway_urls=URLS, workdir="/home/user")
    command = runtime.command(RunRequest(spec=spec, prompt="hola", workdir="/home/user"))
    assert host.driver == "deepagents"
    assert host.python == DEEPAGENTS_PYTHON
    assert host.files == config
    assert host.envs == {**command.envs, "AWS_ENDPOINT_URL_BEDROCK_RUNTIME": URLS["bedrock"]}
    assert host.options == {"config_path": DEEPAGENTS_CONFIG_PATH}


def test_direct_wiring_drops_the_gateway_and_the_placeholder() -> None:
    """Un contenedor exportado no tiene pasarela: el proveedor usa su
    endpoint y su credencial estándar, así que ni URL ni marcador."""
    host = DeepAgents().host_config(_bedrock(), wiring=DirectWiring(), workdir="/home/user")
    config = host.files.files[0].data.decode()
    assert '"base_url": null' in config
    assert '"credential_placeholder": null' in config
    assert "AWS_BEARER_TOKEN_BEDROCK" not in host.envs
    assert host.envs["AWS_REGION"] == "us-east-1"
    opencode = OpenCodeRuntime().host_config(_bedrock(), wiring=DirectWiring(), workdir="/w")
    assert "endpoint" not in opencode.files.files[0].data.decode()
    assert "AWS_BEARER_TOKEN_BEDROCK" not in opencode.envs
    assert MODEL_CREDENTIAL_PLACEHOLDER not in opencode.files.files[0].data.decode()


def test_direct_wiring_rejects_what_needs_a_gateway() -> None:
    compatible = AgentSpec(
        model=AgentModel(provider="openai-compatible", id="m", gateway="x", base_path="/v1")
    )
    for runtime in (OpenCodeRuntime(), DeepAgents()):
        with pytest.raises(InvalidArgumentException, match="endpoint estándar"):
            runtime.host_config(compatible, wiring=DirectWiring(), workdir="/w")
    remote = AgentSpec(
        model=_anthropic().model, mcp={"docs": McpRemote(gateway="docs", path="/mcp")}
    )
    with pytest.raises(InvalidArgumentException, match="McpRemote"):
        OpenCodeRuntime().host_config(remote, wiring=DirectWiring(), workdir="/w")


def test_wiring_values_are_immutable() -> None:
    wiring = GatewayWiring({"a": "http://127.0.0.1:1"})
    with pytest.raises(TypeError):
        wiring.urls["b"] = "x"  # type: ignore[index]


def test_install_steps_follow_the_install_dir() -> None:
    steps = OpenCodeRuntime().install_steps(InstallTarget(install_dir="/srv/agents"))
    assert "/srv/agents/bin" in str(steps[0].args["cmd"])
    deep = DeepAgents().install_steps(InstallTarget(install_dir="/srv/agents"))
    assert deep[0].args["dst"] == "/srv/agents/requirements-deepagents.txt"
    assert deep[2].args["executable"] is True
