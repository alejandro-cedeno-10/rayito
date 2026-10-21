"""Modelo fuera de Bedrock en AgentCore (ADR-033 D2): el opt-in
`external_model` (`testdata/agent-model-optional/plan-vectors.json`), las
filas de AgentCore de `requirement.json`, `model_required`, la bandera
`--external-model` y la prop de CDK. Nada toca AWS."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import boto3
import pytest
from typer.testing import CliRunner

from rayito import AgentCoreOptions, AgentDeployOptions, AgentProject, plan_deploy
from rayito._agentcore._messages import SECURITY_TEXTS
from rayito.cli._agentcore_output import plan_document
from rayito.cli.app import app
from rayito.exceptions import AgentCoreException

ROOT = Path(__file__).resolve().parents[4] / "testdata"
DATA = ROOT / "agent-model-optional"
PLAN_CASES: list[dict[str, Any]] = json.loads((DATA / "plan-vectors.json").read_text("utf-8"))[
    "cases"
]
REQUIREMENT = json.loads((DATA / "requirement.json").read_text("utf-8"))
AGENTCORE_CASES = [case for case in REQUIREMENT["cases"] if case["input"]["target"] == "agentcore"]
CONTRACT = json.loads((ROOT / "agentcore-deploy" / "contract.json").read_text("utf-8"))
REGION = "us-east-1"
BUCKET = "amzn-s3-demo-bucket"
UNCOMPARED_KEYS = ("imageTag", "cost")


@pytest.fixture(autouse=True)
def no_aws_client(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(self: Any, service: str, *args: Any, **kwargs: Any) -> None:
        raise AssertionError(f"planificar no debe crear el cliente {service}")

    monkeypatch.setattr(boto3.session.Session, "client", forbidden)


def agentcore_of(raw: dict[str, Any]) -> AgentCoreOptions:
    return AgentCoreOptions(
        bedrock_models=tuple(raw.get("bedrockModels", ())),
        readable_secrets=tuple(raw.get("readableSecrets", ())),
        external_model=raw.get("externalModel", False),
    )


def plan_of(project: AgentProject, agentcore: AgentCoreOptions) -> Any:
    return plan_deploy(
        project=project,
        options=AgentDeployOptions(),
        region=REGION,
        target="agentcore",
        agentcore=agentcore,
    )


@pytest.mark.parametrize("case", PLAN_CASES, ids=[case["name"] for case in PLAN_CASES])
def test_the_external_model_plan_vectors(
    case: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "environ", dict(case["environ"]))
    project = AgentProject.from_dict(case["project"], base_dir=str(DATA))
    expected = case["expected"]
    options = agentcore_of(case["call"]["agentcore"])
    if "error" in expected:
        with pytest.raises(AgentCoreException) as raised:
            plan_of(project, options)
        assert {"code": raised.value.code, **raised.value.fields} == expected["error"]
        return
    document = plan_document(plan_of(project, options))
    for key in UNCOMPARED_KEYS:
        document.pop(key)
    assert document == expected
    assert set(document["security"]) <= set(SECURITY_TEXTS)


def _requirement_project(given: dict[str, Any]) -> AgentProject:
    model: dict[str, Any] = {"provider": given["provider"], "gateway": "llm"}
    if given["provider"] == "bedrock":
        model["region"] = REGION
    if given["modelId"] is not None:
        model["id"] = given["modelId"]
    document = {
        "schema": "rayito.agent-project/1",
        "name": "ticket-agent",
        "runtime": {"kind": given["runtime"], "entrypoint": "agent:build_graph"},
        "model": model,
        "gateways": {
            "llm": {
                "preset": "custom",
                "upstream": "https://llm-gateway.example.com",
                "headers": {"authorization": "rayito/llm-key"},
                "allow": [["POST", "/v1/chat/completions"]],
            }
        },
    }
    return AgentProject.from_dict(document, base_dir=str(DATA))


@pytest.mark.parametrize("case", AGENTCORE_CASES, ids=[case["name"] for case in AGENTCORE_CASES])
def test_the_agentcore_requirement_matrix(
    case: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "environ", {"RAYITO_BUCKET": BUCKET})
    given = case["input"]
    options = agentcore_of(given)
    project = _requirement_project(given)
    if case["expected"] == "ok":
        plan = plan_of(project, options)
        assert plan.external_model is given["externalModel"]
        return
    with pytest.raises(AgentCoreException) as raised:
        plan_of(project, options)
    assert raised.value.code == case["expected"]["error"]["code"]


def test_model_required_keeps_its_exit_code_and_the_contracts_text() -> None:
    error = AgentCoreException("model_required")
    assert error.exit_code == CONTRACT["errorExitCodes"]["model_required"]
    assert str(error) == CONTRACT["messages"]["errors"]["model_required"]


def test_the_external_model_line_is_the_contracts() -> None:
    assert SECURITY_TEXTS["externalModel"] == CONTRACT["messages"]["security"]["externalModel"]
    assert list(SECURITY_TEXTS) == list(CONTRACT["messages"]["security"])


def test_the_key_never_reaches_the_runtime_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "environ", {"RAYITO_BUCKET": BUCKET})
    case = PLAN_CASES[0]
    project = AgentProject.from_dict(case["project"], base_dir=str(DATA))
    plan = plan_of(project, AgentCoreOptions(external_model=True))
    key_name = project.spec.model.api_key_env
    assert key_name is not None and key_name not in plan.environment
    assert "rayito/agents/ticket-agent/llm" not in json.dumps(dict(plan.environment))


def _project_dir(tmp_path: Path) -> Path:
    case = PLAN_CASES[0]
    (tmp_path / "rayito-agent.json").write_text(json.dumps(case["project"]), encoding="utf-8")
    (tmp_path / "agent.py").write_text("def build_graph(ctx):\n    return None\n")
    return tmp_path


def _cli(tmp_path: Path, *args: str) -> Any:
    return CliRunner().invoke(
        app, ["--json", "--region", REGION, "agent", "deploy", str(tmp_path), "--dry-run", *args]
    )


def test_the_cli_plans_an_external_model_with_the_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RAYITO_BUCKET", BUCKET)
    result = _cli(_project_dir(tmp_path), "--target", "agentcore", "--external-model")
    assert result.exit_code == 0, result.output
    plan = json.loads(result.stdout)["plan"]
    assert plan["externalModel"] is True
    assert plan["bedrockModels"] == []
    assert "externalModel" in plan["security"]


def test_the_cli_refuses_an_external_model_without_the_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RAYITO_BUCKET", BUCKET)
    result = _cli(_project_dir(tmp_path), "--target", "agentcore")
    assert json.loads(result.stdout)["error"]["code"] == "provider_unsupported"
    assert result.exit_code == CONTRACT["errorExitCodes"]["provider_unsupported"]


def test_external_model_without_agentcore_is_option_requires_agentcore(tmp_path: Path) -> None:
    result = _cli(_project_dir(tmp_path), "--external-model")
    document = json.loads(result.stdout)
    assert document["error"]["code"] == "option_requires_agentcore"
    assert "--external-model" in document["error"]["message"]
    assert result.exit_code == CONTRACT["errorExitCodes"]["option_requires_agentcore"]


def test_the_cdk_construct_grants_no_bedrock_to_an_external_model(tmp_path: Path) -> None:
    pytest.importorskip("aws_cdk")
    import aws_cdk as cdk

    from rayito.cdk import RayitoAgent
    from tests.cdk_contract_harness import APP_CONTEXT

    app_ = cdk.App(context=APP_CONTEXT)
    stack = cdk.Stack(app_, "Stack", env=cdk.Environment(account="123456789012", region=REGION))
    RayitoAgent(stack, "Agent", project=_project_dir(tmp_path), external_model=True)
    nested = next(child for child in stack.node.find_all() if isinstance(child, cdk.NestedStack))
    template = cdk.assertions.Template.from_stack(nested).to_json()
    assert template["Conditions"]["HasBedrockModels"] == {
        "Fn::Not": [{"Fn::Equals": [{"Fn::Join": ["", []]}, ""]}]
    }
    root = cdk.assertions.Template.from_stack(stack).to_json()
    (runtime,) = (
        resource
        for resource in root["Resources"].values()
        if resource["Type"] == "AWS::BedrockAgentCore::Runtime"
    )
    environment = runtime["Properties"]["EnvironmentVariables"]
    assert environment["LLM_BASE_URL"] == "https://llm-gateway.example.com/v1"
    assert "LLM_API_KEY" not in environment
