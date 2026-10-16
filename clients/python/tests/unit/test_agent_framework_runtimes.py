"""Adaptadores de Strands, Hermes y `CustomRuntime` (`agent-runtimes-byo`,
ADR-028): configuración del host, ficheros y script de una ejecución, recetas
de instalación, matriz de proveedores y valores seguros (D9), contra los
vectores que comparten con TypeScript (`testdata/agent/framework-*.json` y
`provider-catalogue.json`)."""

from __future__ import annotations

import hashlib
import inspect
import json
from pathlib import Path
from typing import Any

import pytest

from rayito import AgentModel, AgentPermissions, AgentSpec
from rayito._agent._custom import CustomRuntime, HttpApp
from rayito._agent._hermes import HERMES_DEFAULT_TOOLSETS, HermesRuntime
from rayito._agent._runtime import (
    DirectWiring,
    GatewayWiring,
    InstallTarget,
    RunRequest,
    is_installable,
    is_servable,
)
from rayito._agent._runtimes import (
    AGENT_RUNTIMES,
    RUNTIME_OPTION_KEYS,
    resolve_runtime,
    runtime_from_options,
    runtime_to_options,
)
from rayito._agent._strands import (
    STRANDS_PROVIDERS,
    STRANDS_TS_PROVIDERS,
    StrandsRuntime,
    harness_builtin_tools,
)
from rayito._limits import MODEL_CREDENTIAL_PLACEHOLDER
from rayito.exceptions import InvalidArgumentException
from rayito.sandbox_sync.agent import Agent

from .fake_agent import FakeCommandHandle, FakeCommands, FakeFilesystem, FakeSandbox, gateway_status

TESTDATA = Path(__file__).parents[4] / "testdata" / "agent"
HOST_VECTORS = json.loads((TESTDATA / "framework-host-config.json").read_text("utf-8"))
INSTALL_VECTORS = json.loads((TESTDATA / "framework-install-steps.json").read_text("utf-8"))
CATALOGUE = json.loads((TESTDATA / "provider-catalogue.json").read_text("utf-8"))
SESSION_ID = "rda_0000000000000000000000000000000a"


def _spec(data: dict[str, Any]) -> AgentSpec:
    permissions = data.get("permissions")
    return AgentSpec(
        model=AgentModel(
            provider=data["provider"],
            id=data["model"],
            gateway="gw",
            region=data.get("region"),
            base_path=data.get("base_path", ""),
            prompt_caching=data.get("prompt_caching", True),
        ),
        instructions=data.get("instructions"),
        permissions=AgentPermissions(**permissions) if permissions else AgentPermissions(),
    )


def _file(entry: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"path": entry.path, "mode": entry.mode}
    text = entry.data.decode("utf-8")
    result["json" if entry.path.endswith(".json") else "text"] = (
        json.loads(text) if entry.path.endswith(".json") else text
    )
    return result


def _runtime(case: dict[str, Any]) -> Any:
    return runtime_from_options(case["runtime"]["kind"], case["runtime"]["options"])


CASES = {case["name"]: case for case in HOST_VECTORS["cases"]}


@pytest.mark.parametrize("name", list(CASES))
def test_host_config_matches_the_shared_vectors(name: str) -> None:
    case = CASES[name]
    runtime = _runtime(case)
    spec = _spec(case["spec"])
    wiring = (
        GatewayWiring(urls={"gw": HOST_VECTORS["gateway_url"]})
        if case["wiring"] == "gateway"
        else DirectWiring()
    )
    expected = case["expected"]
    if "error" in expected:
        with pytest.raises(InvalidArgumentException):
            runtime.host_config(spec, wiring=wiring, workdir=case["workdir"])
        return
    host = runtime.host_config(spec, wiring=wiring, workdir=case["workdir"])
    assert host.driver == expected["driver"]
    assert host.python == expected["python"]
    assert dict(host.options) == expected["options"]
    assert dict(host.envs) == expected["envs"]
    assert [_file(f) for f in host.files.files] == expected["files"]
    if case["wiring"] == "gateway":
        task = runtime.build_config(
            spec, gateway_urls={"gw": HOST_VECTORS["gateway_url"]}, workdir=case["workdir"]
        )
        assert [_file(f) for f in task.files] == expected["task_files"]


INSTALL_CASES = {case["name"]: case for case in INSTALL_VECTORS["cases"]}


@pytest.mark.parametrize("name", list(INSTALL_CASES))
def test_install_recipes_match_the_shared_vectors(name: str) -> None:
    case = INSTALL_CASES[name]
    runtime = _runtime(case)
    for platform in ("microvm", "container"):
        steps = runtime.install_steps(InstallTarget(platform=platform))
        assert [{"op": s.op, "args": dict(s.args)} for s in steps] == case[platform]
    assert {
        key: hashlib.sha256(data).hexdigest()
        for key, data in sorted(runtime.context_files().items())
    } == case["context_files"]
    assert dict(runtime.manifest_entry()) == case["manifest_entry"]
    assert list(runtime.smoke_checks()) == case["smoke_checks"]
    assert list(runtime.prefetch_paths()) == case["prefetch_paths"]
    assert [{"cmd": w.cmd, "background": w.background} for w in runtime.warmup_steps()] == case[
        "warmup_steps"
    ]


def test_every_framework_runtime_implements_both_optional_ports() -> None:
    runtimes = [
        AGENT_RUNTIMES["strands"],
        AGENT_RUNTIMES["strands-harness"],
        AGENT_RUNTIMES["hermes"],
        StrandsRuntime(language="typescript", entrypoint="agent.ts"),
        CustomRuntime(command=["x"]),
    ]
    assert all(is_installable(runtime) and is_servable(runtime) for runtime in runtimes)


def test_the_registry_resolves_the_framework_names() -> None:
    assert resolve_runtime("strands").name == "strands"
    assert resolve_runtime("strands-harness").name == "strands-harness"
    assert resolve_runtime("hermes").name == "hermes"
    with pytest.raises(InvalidArgumentException):
        resolve_runtime("custom")


@pytest.mark.parametrize(
    ("kind", "options"),
    [
        ("strands", {}),
        ("strands", {"entrypoint": "agente:crea", "requirements": "requirements.txt"}),
        ("strands-harness", {"entrypoint": "agente:crea"}),
        ("strands", {"language": "typescript", "entrypoint": "agent.ts"}),
        ("hermes", {}),
        ("hermes", {"toolsets": ["terminal"], "skills_dir": "skills"}),
        ("custom", {"command": ["./run.sh"], "protocol": "text", "install": ["echo hola"]}),
        ("custom", {"entrypoint": "agente:responde", "requirements": "r.txt", "python": "python3"}),
        (
            "custom",
            {"app": {"command": ["serve"], "port": 9000, "protocol": "openai-chat"}},
        ),
    ],
)
def test_runtime_options_round_trip_through_the_project_file_table(
    kind: str, options: dict[str, Any]
) -> None:
    runtime = runtime_from_options(kind, options)
    table = runtime_to_options(runtime)
    assert table["kind"] == kind
    assert {key: value for key, value in table.items() if key != "kind"} == options
    assert set(table) - {"kind"} <= RUNTIME_OPTION_KEYS[kind]


@pytest.mark.parametrize(
    "factory",
    [
        lambda: StrandsRuntime(language="rust"),  # type: ignore[arg-type]
        lambda: StrandsRuntime(language="typescript"),
        lambda: StrandsRuntime(language="typescript", entrypoint="../fuera.ts"),
        lambda: StrandsRuntime(language="typescript", entrypoint="agent.ts", harness=True),
        lambda: StrandsRuntime(entrypoint="sin-dos-puntos"),
        lambda: StrandsRuntime(requirements="../r.txt"),
        lambda: HermesRuntime(toolsets=()),
        lambda: HermesRuntime(toolsets=("terminal", "terminal")),
        lambda: HermesRuntime(toolsets=("Terminal",)),
        lambda: HermesRuntime(skills_dir="../skills"),
        lambda: CustomRuntime(),
        lambda: CustomRuntime(command=["a"], entrypoint="m:f"),
        lambda: CustomRuntime(entrypoint="m:f", protocol="text"),
        lambda: CustomRuntime(entrypoint="sin-forma"),
        lambda: CustomRuntime(command=[]),
        lambda: HttpApp(command=["x"], port=0),
        lambda: HttpApp(command=["x"], port=8000, health_path="ping"),
        lambda: HttpApp(command=["x"], port=8000, protocol="soap"),  # type: ignore[arg-type]
    ],
)
def test_invalid_runtime_arguments_fail_with_invalid_argument(factory: Any) -> None:
    with pytest.raises(InvalidArgumentException):
        factory()


def test_matrix_in_code_matches_the_provider_catalogue() -> None:
    matrix = CATALOGUE["runtimes"]
    for runtime, providers in (
        ("strands", STRANDS_PROVIDERS),
        ("strands-ts", STRANDS_TS_PROVIDERS),
    ):
        supported = {
            name: (entry["client"], entry["base_path"])
            for name, entry in matrix[runtime].items()
            if entry["supported"]
        }
        assert {name: (p.client, p.base_path) for name, p in providers.items()} == supported
    from rayito._agent._hermes import HERMES_PROVIDERS

    hermes = {
        name: (entry["client"], entry["base_path"])
        for name, entry in matrix["hermes"].items()
        if entry["supported"]
    }
    assert {name: (p.client, p.base_path) for name, p in HERMES_PROVIDERS.items()} == hermes


def test_every_runtime_provider_is_a_known_model_provider() -> None:
    known = set(CATALOGUE["model_providers"]) | {"openai-compatible"}
    for runtime, providers in CATALOGUE["runtimes"].items():
        assert set(providers) <= known, runtime


def test_the_strands_model_clients_come_from_the_recorded_spike() -> None:
    spike = json.loads((TESTDATA / "strands-spike.json").read_text("utf-8"))
    for language, providers in (
        ("python", STRANDS_PROVIDERS),
        ("typescript", STRANDS_TS_PROVIDERS),
    ):
        for provider, entry in providers.items():
            measured = spike[language].get(
                "openai" if provider == "openai-compatible" else provider
            )
            assert measured is not None
            assert measured["client"] == entry.client


# ---- D9: valores seguros, cada uno con su test ----


def _bedrock(**kwargs: Any) -> AgentSpec:
    return AgentSpec(
        model=AgentModel(provider="bedrock", id="m", gateway="gw", region="us-east-1", **kwargs),
    )


def test_strands_has_no_otel_exporter_and_never_carries_aws_credentials() -> None:
    runtime = StrandsRuntime()
    host = runtime.host_config(
        _bedrock(), wiring=GatewayWiring(urls={"gw": "http://127.0.0.1:18001"}), workdir="/w"
    )
    assert host.envs["OTEL_SDK_DISABLED"] == "true"
    assert host.envs["OTEL_TRACES_EXPORTER"] == "none"
    assert not [key for key in host.envs if "ENDPOINT" in key and key.startswith("OTEL")]
    assert not {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"} & set(host.envs)
    assert host.envs["AWS_BEARER_TOKEN_BEDROCK"] == MODEL_CREDENTIAL_PLACEHOLDER
    config = json.loads(host.files.files[0].data)
    assert config["credential_placeholder"] == MODEL_CREDENTIAL_PLACEHOLDER


def test_the_harness_turns_off_code_execution_and_web_search_whatever_the_permissions() -> None:
    allow_all = AgentSpec(
        model=AgentModel(provider="bedrock", id="m", gateway="gw", region="us-east-1"),
        permissions=AgentPermissions(
            tools={"webfetch": "allow", "websearch": "allow", "bash": "allow"}
        ),
    )
    tools = harness_builtin_tools(allow_all)
    assert tools["programmatic_tool_caller"] is False
    assert tools["web_search"] is False
    assert tools["*"] is False


def test_the_harness_builtin_tools_follow_the_agent_permissions() -> None:
    spike = json.loads((TESTDATA / "strands-spike.json").read_text("utf-8"))
    names = set(spike["harness"]["builtin_tool_names"])
    defaults = harness_builtin_tools(
        AgentSpec(model=AgentModel(provider="bedrock", id="m", gateway="gw", region="us-east-1"))
    )
    assert set(defaults) - {"*"} <= names
    assert {name for name, enabled in defaults.items() if enabled and name != "*"} == {
        "shell",
        "read",
        "write",
        "edit",
        "subagent",
    }
    restricted = harness_builtin_tools(
        AgentSpec(
            model=AgentModel(provider="bedrock", id="m", gateway="gw", region="us-east-1"),
            permissions=AgentPermissions(tools={"bash": "deny", "edit": "deny", "task": "deny"}),
        )
    )
    assert {name for name, enabled in restricted.items() if enabled and name != "*"} == {"read"}


def test_the_harness_keeps_its_state_under_the_agent_state_dir() -> None:
    host = StrandsRuntime(harness=True).host_config(
        _bedrock(), wiring=GatewayWiring(urls={"gw": "http://127.0.0.1:18001"}), workdir="/w"
    )
    config = json.loads(host.files.files[0].data)
    assert config["sessions_dir"].startswith("/home/user/.rayito/agent/strands/")
    assert config["memory_dir"].startswith("/home/user/.rayito/agent/strands/")
    assert "interventions" not in config


def test_hermes_listens_on_loopback_and_never_carries_its_api_key() -> None:
    runtime = HermesRuntime()
    spec = _bedrock()
    wiring = GatewayWiring(urls={"gw": "http://127.0.0.1:18001"})
    host = runtime.host_config(spec, wiring=wiring, workdir="/w")
    task = runtime.build_config(spec, gateway_urls={"gw": "http://127.0.0.1:18001"}, workdir="/w")
    command = runtime.command(RunRequest(spec=spec, prompt="secreto", workdir="/w"))
    assert "API_SERVER_HOST" not in host.envs
    assert host.envs["HERMES_HOME"].startswith("/home/user/.rayito/agent/hermes/")
    everything = json.dumps(
        [dict(host.envs), [f.data.decode() for f in task.files], dict(command.envs), command.script]
    )
    assert "API_SERVER_KEY" not in everything
    assert "secreto" not in everything and "secreto" not in command.script
    assert b"secreto" in command.stdin


def test_hermes_drops_the_toolsets_the_permissions_deny_and_defaults_to_terminal_and_files() -> (
    None
):
    assert HermesRuntime().toolsets == HERMES_DEFAULT_TOOLSETS == ("terminal", "file")
    spec = AgentSpec(
        model=AgentModel(provider="anthropic", id="m", gateway="gw"),
        permissions=AgentPermissions(tools={"bash": "deny"}),
    )
    host = HermesRuntime(toolsets=("terminal", "file", "web")).host_config(
        spec, wiring=GatewayWiring(urls={"gw": "http://127.0.0.1:18001"}), workdir="/w"
    )
    assert json.loads(host.files.files[0].data)["toolsets"] == ["file"]


def test_hermes_rejects_command_patterns_it_cannot_enforce() -> None:
    spec = AgentSpec(
        model=AgentModel(provider="anthropic", id="m", gateway="gw"),
        permissions=AgentPermissions(tools={"bash": {"git *": "allow", "*": "deny"}}),
    )
    with pytest.raises(InvalidArgumentException, match="patrones"):
        HermesRuntime().host_config(
            spec, wiring=GatewayWiring(urls={"gw": "http://127.0.0.1:18001"}), workdir="/w"
        )


def test_the_prompt_never_goes_in_argv_or_the_script_for_any_runtime() -> None:
    spec = _bedrock()
    for runtime in (
        StrandsRuntime(),
        HermesRuntime(),
        CustomRuntime(command=["x"]),
        CustomRuntime(entrypoint="m:f"),
    ):
        command = runtime.command(RunRequest(spec=spec, prompt="el prompt", workdir="/w"))
        assert "el prompt" not in command.script
        assert json.loads(command.stdin)["prompt"] == "el prompt"


def test_without_a_gateway_there_is_no_placeholder_key_or_endpoint_override() -> None:
    host = StrandsRuntime().host_config(_bedrock(), wiring=DirectWiring(), workdir="/w")
    assert "AWS_BEARER_TOKEN_BEDROCK" not in host.envs
    assert "AWS_ENDPOINT_URL_BEDROCK_RUNTIME" not in host.envs
    config = json.loads(host.files.files[0].data)
    assert config["base_url"] is None and config["credential_placeholder"] is None


def test_the_custom_runtime_sees_the_model_only_through_the_gateway() -> None:
    envs = (
        CustomRuntime(command=["x"])
        .host_config(
            _bedrock(), wiring=GatewayWiring(urls={"gw": "http://127.0.0.1:18001"}), workdir="/w"
        )
        .envs
    )
    assert envs["RAYITO_MODEL_API_KEY"] == MODEL_CREDENTIAL_PLACEHOLDER
    assert envs["RAYITO_MODEL_BASE_URL"] == "http://127.0.0.1:18001"
    assert not {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"} & set(envs)


# ---- el contrato de tareas de AgentRuntime, por el host ----


def _lines(events: list[dict[str, Any]]) -> list[bytes]:
    return [json.dumps(event, separators=(",", ":")).encode() for event in events]


def test_a_strands_run_decodes_the_recorded_events_into_the_sdk_result() -> None:
    expected = json.loads((TESTDATA / "strands-expected-events.json").read_text("utf-8"))["plain"]
    handle = FakeCommandHandle(
        lines=_lines(
            [
                {"v": 1, "type": "session", "session_id": SESSION_ID},
                *expected,
                {"v": 1, "type": "done", "session_id": SESSION_ID},
            ]
        )
    )
    sandbox = FakeSandbox(
        commands=FakeCommands(handles=[handle]),
        files=FakeFilesystem(),
        gateways={"gw": gateway_status()},
    )
    result = Agent(sandbox).run("suma 2 y 3", spec=_bedrock(), runtime=StrandsRuntime())
    assert result.session_id == SESSION_ID
    assert result.text.endswith("El resultado es 5.")
    assert result.usage.input == 300
    assert [call.name for call in result.tool_calls] == ["add"]
    written = {entry.path for entry in sandbox.files.write_files_calls[0]}
    assert "/home/user/.rayito/agent/strands/wiring.env" in written
    assert sandbox.commands.calls[0].envs is not None
    assert json.loads(handle.stdin)["prompt"] == "suma 2 y 3"


@pytest.mark.parametrize(
    "runtime",
    [StrandsRuntime(), HermesRuntime(), StrandsRuntime(language="typescript", entrypoint="a.ts")],
)
def test_an_unsupported_provider_fails_before_any_rpc(runtime: Any) -> None:
    spec = AgentSpec(
        model=AgentModel(provider="azure", id="gpt-4.1", gateway="gw"),
    )
    sandbox = FakeSandbox(
        commands=FakeCommands(handles=[]), files=FakeFilesystem(), gateways={"gw": gateway_status()}
    )
    with pytest.raises(InvalidArgumentException, match="azure"):
        Agent(sandbox).run("hola", spec=spec, runtime=runtime)
    assert sandbox.commands.calls == []
    assert sandbox.files.write_files_calls == []


def test_the_missing_runtime_line_decodes_as_a_failure() -> None:
    runtime = StrandsRuntime()
    state = runtime.new_state()
    events = runtime.parse_line(b'{"v":1,"type":"rayito.runtime_missing"}', state)
    assert events and runtime.finish(state, 0).reason == "runtime_missing"  # type: ignore[union-attr]


def test_session_ids_must_have_the_sdk_form() -> None:
    with pytest.raises(InvalidArgumentException):
        StrandsRuntime().command(
            RunRequest(spec=_bedrock(), prompt="x", workdir="/w", session_id="otra-forma")
        )


def test_the_framework_adapters_are_pure() -> None:
    from rayito._agent import _custom, _hermes, _host_runtime, _strands

    for module in (_custom, _hermes, _host_runtime, _strands):
        source = inspect.getsource(module)
        assert "import grpc" not in source and "import boto3" not in source
