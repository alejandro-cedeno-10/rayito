"""`RayitoAgent` sintetizado sin AWS (ADR-032 D12.1): la IAM de la pila
anidada es la de `infra/agentcore-runtime.yaml` con los parámetros de la CLI
más una sola política que lee el objeto del asset; las etiquetas fijas llegan
a todo recurso etiquetable; el runtime depende del build y `Mmds` lee su id y
su versión; `local_docker()` quita el build; la región token falla y la cuenta
token vale."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("aws_cdk")
yaml = pytest.importorskip("yaml")

import aws_cdk as cdk  # noqa: E402
from aws_cdk import aws_iam as iam  # noqa: E402

from rayito._agentcore._domain import (  # noqa: E402
    bedrock_resource_arns,
    secret_resource_arns,
)
from rayito._limits import CDK_RUNTIME_ENVIRONMENT_VALUE_MAX_CHARS  # noqa: E402
from rayito.cdk import AgentImageBuild, RayitoAgent, RayitoCdkException  # noqa: E402
from rayito.cdk._agent import check_runtime_environment  # noqa: E402
from rayito.cdk._environment import fixed_tags  # noqa: E402
from rayito.exceptions import AgentCoreException  # noqa: E402
from tests.cdk_contract_harness import APP_CONTEXT, REPO_ROOT, TESTDATA_DIR  # noqa: E402

ACCOUNT = "123456789012"
REGION = "us-east-1"
STRANDS = TESTDATA_DIR / "agent-samples" / "python" / "strands"
MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
RUNTIME_TEMPLATE = REPO_ROOT / "infra" / "agentcore-runtime.yaml"
FIXED_KEYS = tuple(fixed_tags("agentcore-runtime"))
#: Tipos sin `Tags` en CloudFormation.
UNTAGGABLE = ("AWS::IAM::Policy", "Custom::RayitoImageBuild", "Custom::RayitoRuntimeMmds")


class TemplateLoader(yaml.SafeLoader):  # type: ignore[name-defined,misc]
    pass


def _short_tag(loader: Any, suffix: str, node: Any) -> Any:
    name = suffix if suffix in {"Ref", "Condition"} else f"Fn::{suffix}"
    if isinstance(node, yaml.ScalarNode):
        return {name: loader.construct_scalar(node)}
    if isinstance(node, yaml.SequenceNode):
        return {name: loader.construct_sequence(node, deep=True)}
    return {name: loader.construct_mapping(node, deep=True)}


TemplateLoader.add_multi_constructor("!", _short_tag)


def fold(value: Any, parameters: dict[str, Any]) -> Any:
    """Lo que `CfnInclude` hace al plegar los parámetros: `Ref` de un
    parámetro es su valor y `${Parámetro}` de un `Fn::Sub`, su texto."""
    if isinstance(value, dict):
        if set(value) == {"Ref"} and value["Ref"] in parameters:
            return parameters[value["Ref"]]
        if set(value) == {"Fn::Sub"} and isinstance(value["Fn::Sub"], str):
            text = value["Fn::Sub"]
            for name, replacement in parameters.items():
                text = text.replace("${" + name + "}", str(replacement))
            return {"Fn::Sub": text}
        return {key: fold(item, parameters) for key, item in value.items()}
    if isinstance(value, list):
        return [fold(item, parameters) for item in value]
    return value


def new_stack(env: cdk.Environment | None = None) -> tuple[cdk.App, cdk.Stack]:
    app = cdk.App(context=APP_CONTEXT)
    stack = cdk.Stack(app, "AgentStack", env=env or cdk.Environment(account=ACCOUNT, region=REGION))
    return app, stack


def templates(stack: cdk.Stack) -> tuple[dict[str, Any], dict[str, Any]]:
    root = dict(cdk.assertions.Template.from_stack(stack).to_json())
    nested = next(child for child in stack.node.find_all() if isinstance(child, cdk.NestedStack))
    return root, dict(cdk.assertions.Template.from_stack(nested).to_json())


def of_type(template: dict[str, Any], kind: str) -> dict[str, Any]:
    return {key: value for key, value in template["Resources"].items() if value["Type"] == kind}


@pytest.fixture(scope="module")
def strands() -> tuple[RayitoAgent, dict[str, Any], dict[str, Any]]:
    _, stack = new_stack()
    agent = RayitoAgent(
        stack, "Agent", project=STRANDS, bedrock_models=[MODEL], readable_secrets=["demo/key"]
    )
    root, nested = templates(stack)
    return agent, root, nested


def test_the_nested_iam_is_the_clis_plus_the_source_policy(
    strands: tuple[RayitoAgent, dict[str, Any], dict[str, Any]],
) -> None:
    agent, _, nested = strands
    yaml_template = yaml.load(RUNTIME_TEMPLATE.read_text(encoding="utf-8"), Loader=TemplateLoader)
    models = list(bedrock_resource_arns([MODEL], REGION, ACCOUNT))
    secrets = list(secret_resource_arns(["demo/key"], REGION, ACCOUNT))
    parameters: dict[str, Any] = {
        "AgentName": "strands",
        "AgentDisplayName": "strands",
        "RuntimeName": agent.runtime_name,
        "ArtifactBucket": (
            f"cdk-{cdk.DefaultStackSynthesizer.DEFAULT_QUALIFIER}-assets-{ACCOUNT}-{REGION}"
        ),
        "BedrockModelArns": models,
        "ReadableSecretArns": secrets,
    }
    for role in ("RuntimeRole", "BuildRole"):
        expected = yaml_template["Resources"][role]["Properties"]
        found = nested["Resources"][role]["Properties"]
        assert found["AssumeRolePolicyDocument"] == fold(
            expected["AssumeRolePolicyDocument"], parameters
        )
        assert found["Policies"][0] == fold(expected["Policies"][0], parameters)
    runtime_policies = nested["Resources"]["RuntimeRole"]["Properties"]["Policies"]
    assert len(runtime_policies) == 1
    build_policies = nested["Resources"]["BuildRole"]["Properties"]["Policies"]
    assert len(build_policies) == 2
    (extra,) = build_policies[1]["PolicyDocument"]["Statement"]
    assert (
        extra["Action"] == ["s3:GetObject", "s3:GetObjectVersion"]
        and extra["Sid"] == "ReadSourceAsset"
    )


def test_the_role_statements_equal_the_resolved_arn_lists(
    strands: tuple[RayitoAgent, dict[str, Any], dict[str, Any]],
) -> None:
    _, _, nested = strands
    statements = nested["Resources"]["RuntimeRole"]["Properties"]["Policies"][0]["PolicyDocument"][
        "Statement"
    ]
    conditional = {item["Fn::If"][1]["Sid"]: item["Fn::If"][1] for item in statements[-2:]}
    vectors = json.loads(
        (TESTDATA_DIR / "agentcore-deploy" / "model-arns.json").read_text(encoding="utf-8")
    )
    assert vectors
    assert conditional["InvokeBedrockModels"]["Resource"] == list(
        bedrock_resource_arns([MODEL], REGION, ACCOUNT)
    )
    assert conditional["ReadSecrets"]["Resource"] == list(
        secret_resource_arns(["demo/key"], REGION, ACCOUNT)
    )


def test_without_secrets_the_secrets_statement_is_conditioned_away() -> None:
    _, stack = new_stack()
    RayitoAgent(stack, "Agent", project=STRANDS)
    _, nested = templates(stack)
    condition = nested["Conditions"]["HasReadableSecrets"]
    assert condition == {"Fn::Not": [{"Fn::Equals": [{"Fn::Join": ["", []]}, ""]}]}


def test_every_taggable_resource_carries_the_fixed_and_user_tags() -> None:
    _, stack = new_stack()
    RayitoAgent(stack, "Agent", project=STRANDS, tags={"team": "demo"})
    root, nested = templates(stack)
    for template in (root, nested):
        for logical_id, resource in template["Resources"].items():
            if resource["Type"] in UNTAGGABLE:
                continue
            tags = resource["Properties"]["Tags"]
            keys = set(tags) if isinstance(tags, dict) else {item["Key"] for item in tags}
            assert {*FIXED_KEYS, "team"} <= keys, logical_id


def test_the_runtime_waits_for_the_build_and_mmds_follows_its_version(
    strands: tuple[RayitoAgent, dict[str, Any], dict[str, Any]],
) -> None:
    _, root, _ = strands
    ((runtime_id, runtime),) = of_type(root, "AWS::BedrockAgentCore::Runtime").items()
    (build_id,) = of_type(root, "Custom::RayitoImageBuild")
    (mmds,) = of_type(root, "Custom::RayitoRuntimeMmds").values()
    assert runtime["DependsOn"] == [build_id]
    assert mmds["Properties"]["AgentRuntimeId"] == {"Fn::GetAtt": [runtime_id, "AgentRuntimeId"]}
    assert mmds["Properties"]["AgentRuntimeVersion"] == {
        "Fn::GetAtt": [runtime_id, "AgentRuntimeVersion"]
    }
    properties = runtime["Properties"]
    assert properties["Description"] == "Rayito agent strands (rayito.agentcore/1+cdk)"
    assert properties["NetworkConfiguration"] == {"NetworkMode": "PUBLIC"}
    assert properties["ProtocolConfiguration"] == "HTTP"


def test_the_handler_can_only_touch_its_project_repository_and_runtime(
    strands: tuple[RayitoAgent, dict[str, Any], dict[str, Any]],
) -> None:
    _, root, _ = strands
    (policy,) = (
        value
        for key, value in of_type(root, "AWS::IAM::Policy").items()
        if key.startswith("AgentHandlerRole")
    )
    actions = {
        action
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]
        for action in (
            statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]]
        )
    }
    assert actions == {
        "logs:CreateLogStream",
        "logs:PutLogEvents",
        "codebuild:StartBuild",
        "codebuild:BatchGetBuilds",
        "ecr:DescribeImages",
        "bedrock-agentcore:GetAgentRuntime",
        "bedrock-agentcore:UpdateAgentRuntime",
        "iam:PassRole",
    }
    for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
        assert statement["Resource"] != "*"


def test_local_docker_drops_the_build_resources(tmp_path: Path) -> None:
    app = cdk.App(context=APP_CONTEXT, outdir=str(tmp_path))
    stack = cdk.Stack(app, "AgentStack", env=cdk.Environment(account=ACCOUNT, region=REGION))
    RayitoAgent(stack, "Agent", project=STRANDS, build=AgentImageBuild.local_docker())
    root, nested = templates(stack)
    assert not of_type(root, "Custom::RayitoImageBuild")
    assert set(nested["Resources"]) == {"RuntimeRole"}
    pull = nested["Resources"]["RuntimeRole"]["Properties"]["Policies"][0]["PolicyDocument"][
        "Statement"
    ][0]
    assert pull["Sid"] == "PullImage"
    assert "container-assets" in json.dumps(pull["Resource"])
    manifest = json.loads((tmp_path / "AgentStack.assets.json").read_text(encoding="utf-8"))
    (image,) = manifest["dockerImages"].values()
    assert image["source"]["platform"] == "linux/arm64"


def test_an_environment_agnostic_stack_is_refused_before_any_child() -> None:
    app = cdk.App()
    stack = cdk.Stack(app, "NoEnv")
    with pytest.raises(RayitoCdkException) as raised:
        RayitoAgent(stack, "Agent", project=STRANDS)
    assert raised.value.code == "region_unresolved"
    assert stack.node.try_find_child("Agent") is None


def test_an_account_token_builds_the_arns_with_pseudo_parameters() -> None:
    _, stack = new_stack(cdk.Environment(region=REGION))
    RayitoAgent(stack, "Agent", project=STRANDS, readable_secrets=["demo/key"])
    _, nested = templates(stack)
    statements = nested["Resources"]["RuntimeRole"]["Properties"]["Policies"][0]["PolicyDocument"][
        "Statement"
    ]
    secrets = statements[-1]["Fn::If"][1]["Resource"]
    assert "AWS::AccountId" in json.dumps(secrets)


def test_an_invalid_account_is_refused() -> None:
    _, stack = new_stack(cdk.Environment(account="12345", region=REGION))
    with pytest.raises(RayitoCdkException) as raised:
        RayitoAgent(stack, "Agent", project=STRANDS)
    assert raised.value.code == "account_invalid"


def test_plan_errors_pass_through_as_agentcore_exceptions() -> None:
    _, stack = new_stack()
    with pytest.raises(AgentCoreException) as raised:
        RayitoAgent(stack, "Agent", project=STRANDS, idle_session_timeout_seconds=10)
    assert raised.value.code == "lifecycle_invalid"


def test_an_environment_value_over_the_schema_cap_is_env_invalid() -> None:
    check_runtime_environment({"A": "x" * CDK_RUNTIME_ENVIRONMENT_VALUE_MAX_CHARS})
    with pytest.raises(AgentCoreException) as raised:
        check_runtime_environment({"A": "x" * (CDK_RUNTIME_ENVIRONMENT_VALUE_MAX_CHARS + 1)})
    assert raised.value.code == "env_invalid"
    assert raised.value.fields["maxChars"] == str(CDK_RUNTIME_ENVIRONMENT_VALUE_MAX_CHARS)


@pytest.mark.parametrize("minutes", [0, 4, 111, True])
def test_an_out_of_range_build_timeout_is_option_invalid(minutes: int) -> None:
    with pytest.raises(RayitoCdkException) as raised:
        AgentImageBuild.code_build(timeout_minutes=minutes)
    assert raised.value.code == "option_invalid"


def test_the_build_timeout_reaches_the_project_and_the_provider() -> None:
    _, stack = new_stack()
    RayitoAgent(
        stack, "Agent", project=STRANDS, build=AgentImageBuild.code_build(timeout_minutes=20)
    )
    _, nested = templates(stack)
    assert nested["Resources"]["BuildProject"]["Properties"]["TimeoutInMinutes"] == 20


def test_outputs_only_when_asked_and_grant_invoke() -> None:
    _, stack = new_stack()
    agent = RayitoAgent(stack, "Agent", project=STRANDS, name="my_agent", export_outputs=True)
    role = iam.Role(stack, "Caller", assumed_by=iam.AccountRootPrincipal())
    agent.grant_invoke(role)
    root, _ = templates(stack)
    assert len(root["Outputs"]) == 2
    assert agent.agent_name == "my_agent"
    assert agent.runtime_name == "rayito_my_agent"
    caller = next(
        value
        for key, value in of_type(root, "AWS::IAM::Policy").items()
        if key.startswith("CallerDefaultPolicy")
    )
    (statement,) = caller["Properties"]["PolicyDocument"]["Statement"]
    assert statement["Action"] == "bedrock-agentcore:InvokeAgentRuntime"
    assert agent.runtime is not None


def test_no_outputs_by_default_and_two_agents_never_collide() -> None:
    _, stack = new_stack()
    RayitoAgent(stack, "One", project=STRANDS, name="one")
    RayitoAgent(stack, "Two", project=STRANDS, name="two")
    root = dict(cdk.assertions.Template.from_stack(stack).to_json())
    assert "Outputs" not in root
    assert len(of_type(root, "AWS::BedrockAgentCore::Runtime")) == 2
    assert len(of_type(root, "AWS::CloudFormation::Stack")) == 2
