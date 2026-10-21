"""Modelo opcional cuando no es Bedrock (ADR-033 D2): cuándo hace falta
`[model] id` (`testdata/agent-model-optional/requirement.json`), los nombres
de variable de `[model]`, el cableado en MicroVMs (variables, drivers sin
modelo, telemetría) y que un proyecto existente no cambia."""

from __future__ import annotations

import json
import types
from pathlib import Path
from typing import Any, cast

import pytest

from rayito import AgentModel, AgentProject, AgentSpec
from rayito._agent import _telemetry
from rayito._agent._custom import MODEL_ID_ENV, CustomRuntime
from rayito._agent._deepagents import DeepAgents, build_deepagents_config
from rayito._agent._domain import MODEL_ENV_KEYS, required_model_id
from rayito._agent._gateways import openai_compatible_gateway
from rayito._agent._hermes import HermesRuntime
from rayito._agent._host_runtime import model_env_names, standard_model_envs
from rayito._agent._opencode import build_opencode_config
from rayito._agent._project_domain import (
    _GATEWAY_PRESETS,
    MODEL_ID_REQUIRED_PRESETS,
    AgentProjectException,
    model_id_required,
    require_model_id,
)
from rayito._agent._runner.agent_host.drivers import deepagents as deepagents_driver
from rayito._agent._runner.agent_host.drivers import strands as strands_driver
from rayito._agent._strands import StrandsRuntime
from rayito._limits import MODEL_CREDENTIAL_PLACEHOLDER
from rayito.exceptions import InvalidArgumentException

ROOT = Path(__file__).resolve().parents[4] / "testdata"
REQUIREMENT = json.loads((ROOT / "agent-model-optional" / "requirement.json").read_text("utf-8"))
MICROVM_CASES = [case for case in REQUIREMENT["cases"] if case["input"]["target"] == "microvm"]
PROJECT_DIR = ROOT / "agent-project"
GATEWAY_URL = "https://gw.example.com"
LLM_GATEWAY = "llm"


def _external_model(model_id: str | None = None, **names: Any) -> AgentModel:
    return AgentModel(provider="openai-compatible", id=model_id, gateway=LLM_GATEWAY, **names)


def _model_table(project: AgentProject) -> dict[str, object]:
    return cast(dict[str, object], project.to_dict()["model"])


def _spec(model_id: str | None = None, **names: Any) -> AgentSpec:
    return AgentSpec(model=_external_model(model_id, **names))


def test_the_required_presets_are_the_ones_that_filter_by_model() -> None:
    derived = tuple(
        name for name, preset in _GATEWAY_PRESETS.items() if "models" in preset.required
    )
    assert derived == MODEL_ID_REQUIRED_PRESETS
    assert list(MODEL_ID_REQUIRED_PRESETS) == REQUIREMENT["modelIdRequiredPresets"]


@pytest.mark.parametrize("case", MICROVM_CASES, ids=[case["name"] for case in MICROVM_CASES])
def test_the_microvm_requirement_matrix(case: dict[str, Any]) -> None:
    given = case["input"]
    expected = case["expected"]

    def check() -> None:
        require_model_id(
            given["modelId"],
            runtime=given["runtime"],
            entrypoint=given["entrypoint"],
            gateway_preset=given["gatewayPreset"],
            detected=given["source"] == "detection",
        )

    if expected == "ok":
        check()
        return
    with pytest.raises(AgentProjectException) as raised:
        check()
    assert expected["error"]["exception"] == "AgentProjectException"
    assert (raised.value.code, raised.value.path) == (
        expected["error"]["code"],
        expected["error"]["path"],
    )


def test_a_known_id_is_never_required_again() -> None:
    assert model_id_required(runtime="opencode", entrypoint=False, gateway_preset="bedrock")
    require_model_id("gpt-5-mini", runtime="opencode", entrypoint=False, gateway_preset="bedrock")


def test_an_agent_model_takes_no_id_and_validates_its_variable_names() -> None:
    model = _external_model(id_env="LLM_MODEL", base_url_env="LLM_BASE_URL")
    assert model.id is None
    assert model.env_names() == {"id_env": "LLM_MODEL", "base_url_env": "LLM_BASE_URL"}
    assert AgentSpec(model=model).effective_small_model is None
    for key in MODEL_ENV_KEYS:
        with pytest.raises(InvalidArgumentException, match=key):
            _external_model(**{key: "1-NOT-A-NAME"})


def test_a_project_without_a_model_id_loads_and_writes_no_id() -> None:
    project = AgentProject.load(PROJECT_DIR / "valid" / "model-without-id.toml")
    assert project.spec.model.id is None
    model = _model_table(project)
    assert "id" not in model
    assert model["base_url_env"] == "LLM_BASE_URL"


def _document(**model: object) -> dict[str, object]:
    return {
        "schema": "rayito.agent-project/1",
        "name": "ticket-agent",
        "runtime": {"kind": "langgraph", "entrypoint": "agent:build_graph"},
        "model": {"provider": "openai-compatible", "gateway": LLM_GATEWAY, **model},
        "gateways": {
            LLM_GATEWAY: {
                "preset": "openai-compatible",
                "secret": "rayito/llm-key",
                "upstream": "https://llm-gateway.example.com",
            }
        },
    }


def test_a_model_variable_also_in_env_is_a_conflict() -> None:
    document = {**_document(base_url_env="LLM_BASE_URL"), "env": {"LLM_BASE_URL": "x"}}
    with pytest.raises(AgentProjectException) as raised:
        AgentProject.from_dict(document, base_dir=".")
    assert (raised.value.code, raised.value.path) == ("conflict", "model.base_url_env")


def test_a_model_variable_must_be_a_name() -> None:
    with pytest.raises(AgentProjectException) as raised:
        AgentProject.from_dict(_document(id_env="LLM MODEL"), base_dir=".")
    assert (raised.value.code, raised.value.path) == ("invalid_value", "model.id_env")


def test_a_bedrock_gateway_still_needs_the_id_in_a_file() -> None:
    document = _document()
    document["model"] = {"provider": "bedrock", "gateway": "bedrock", "region": "us-east-1"}
    document["gateways"] = {
        "bedrock": {
            "preset": "bedrock",
            "secret": "rayito/bedrock-key",
            "region": "us-east-1",
            "models": ["amazon.nova-lite-v1:0"],
        }
    }
    with pytest.raises(AgentProjectException) as raised:
        AgentProject.from_dict(document, base_dir=".")
    assert (raised.value.code, raised.value.path) == ("missing_key", "model.id")


@pytest.mark.parametrize(
    "build",
    [
        lambda spec: build_opencode_config(spec, gateway_urls={LLM_GATEWAY: GATEWAY_URL}),
        lambda spec: build_deepagents_config(
            spec, gateway_urls={LLM_GATEWAY: GATEWAY_URL}, workdir="/w", entrypoint=None
        ),
        lambda spec: StrandsRuntime().host_setup(spec, {LLM_GATEWAY: GATEWAY_URL}, "/w"),
        lambda spec: HermesRuntime().host_setup(spec, {LLM_GATEWAY: GATEWAY_URL}, "/w"),
    ],
    ids=["opencode", "deepagents", "strands", "hermes"],
)
def test_a_runtime_that_builds_the_model_needs_its_id(build: Any) -> None:
    with pytest.raises(InvalidArgumentException, match="construye el modelo"):
        build(_spec())


def test_with_an_entrypoint_the_model_travels_as_null() -> None:
    config = build_deepagents_config(
        _spec(), gateway_urls={LLM_GATEWAY: GATEWAY_URL}, workdir="/w", entrypoint="agent:graph"
    )
    assert config["model"] is None
    strands = StrandsRuntime(entrypoint="agent:agent")
    setup = strands.host_setup(_spec(), {LLM_GATEWAY: GATEWAY_URL}, "/w")
    assert setup.config is not None and setup.config["model"] is None


def test_an_exported_langgraph_without_id_needs_no_endpoint() -> None:
    config = build_deepagents_config(_spec(), gateway_urls=None, workdir="/w", entrypoint="a:g")
    assert config["base_url"] is None


def test_required_model_id_names_the_runtime() -> None:
    assert required_model_id(_external_model("demo-model"), "opencode") == "demo-model"
    with pytest.raises(InvalidArgumentException, match="'hermes'"):
        required_model_id(_external_model(), "hermes")


def test_the_microvm_wiring_exports_the_named_variables_with_the_placeholder() -> None:
    spec = AgentSpec(
        model=AgentModel(
            provider="openai-compatible",
            id="demo-model",
            gateway=LLM_GATEWAY,
            base_path="/v1",
            id_env="LLM_MODEL",
            base_url_env="LLM_BASE_URL",
            api_key_env="LLM_API_KEY",
        )
    )
    envs = standard_model_envs(spec, {LLM_GATEWAY: GATEWAY_URL})
    assert envs["LLM_BASE_URL"] == f"{GATEWAY_URL}/v1" == envs["OPENAI_BASE_URL"]
    assert envs["LLM_API_KEY"] == MODEL_CREDENTIAL_PLACEHOLDER
    assert envs["LLM_MODEL"] == "demo-model"
    assert standard_model_envs(spec, None) == {"LLM_MODEL": "demo-model"}


def test_without_an_id_the_id_variable_is_not_exported() -> None:
    spec = _spec(id_env="LLM_MODEL", base_url_env="LLM_BASE_URL")
    assert "LLM_MODEL" not in model_env_names(spec, GATEWAY_URL)
    assert model_env_names(spec, None) == {}


def test_without_names_the_wiring_is_the_same_as_before() -> None:
    spec = _spec("demo-model")
    assert model_env_names(spec, GATEWAY_URL) == {}


def test_the_custom_runtime_omits_the_model_id_variable_without_an_id() -> None:
    envs = CustomRuntime(entrypoint="agent:run").wiring_envs(_spec(), {LLM_GATEWAY: GATEWAY_URL})
    assert MODEL_ID_ENV not in envs
    with_id = CustomRuntime(entrypoint="agent:run").wiring_envs(
        _spec("demo-model"), {LLM_GATEWAY: GATEWAY_URL}
    )
    assert with_id[MODEL_ID_ENV] == "demo-model"


def test_telemetry_omits_the_request_model_without_an_id() -> None:
    attributes = _telemetry.start_attributes(_spec(), runtime_name="langgraph")
    assert _telemetry.REQUEST_MODEL_ATTRIBUTE not in attributes
    known = _telemetry.start_attributes(_spec("demo-model"), runtime_name="langgraph")
    assert known[_telemetry.REQUEST_MODEL_ATTRIBUTE] == "demo-model"


@pytest.mark.parametrize(
    "driver", [deepagents_driver, strands_driver], ids=["deepagents", "strands"]
)
def test_the_drivers_build_a_model_only_with_an_id(
    driver: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[str] = []
    monkeypatch.setattr(driver, "build_model", lambda config, model_id: built.append(model_id))
    assert driver.optional_model({}, None) is None
    driver.optional_model({}, "demo-model")
    assert built == ["demo-model"]


def test_the_deepagents_context_has_no_model_without_an_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = types.SimpleNamespace(
        LocalShellBackend=lambda **kwargs: kwargs, GENERAL_PURPOSE_SUBAGENT={}
    )
    monkeypatch.setattr(
        deepagents_driver,
        "importlib",
        types.SimpleNamespace(import_module=lambda name: backend),
    )
    monkeypatch.setattr(deepagents_driver, "permission_middleware", lambda rules: "permissions")

    def forbidden(config: Any, model_id: str) -> None:
        raise AssertionError("no debe construir un modelo")

    monkeypatch.setattr(deepagents_driver, "build_model", forbidden)
    ctx = deepagents_driver.build_context({"workdir": "/w", "permissions": {}}, None)
    assert ctx.model is None


def test_an_existing_project_keeps_its_canonical_form() -> None:
    for path in sorted((PROJECT_DIR / "valid").glob("*.toml")):
        expected = json.loads(path.with_suffix(".expected.json").read_text("utf-8"))
        model = _model_table(AgentProject.load(path))
        assert model == expected["model"]
        assert list(model)[:1] == ["provider"]


def test_a_gateway_built_by_hand_keeps_working_without_an_id() -> None:
    project = AgentProject(
        name="ticket-agent",
        runtime=DeepAgents("agent:graph", require_entrypoint=True),
        spec=_spec(),
        gateways={
            LLM_GATEWAY: openai_compatible_gateway(
                "rayito/llm-key", upstream="https://llm-gateway.example.com"
            )
        },
    )
    assert "id" not in _model_table(project)
