"""`infra/agentcore-runtime.yaml` (ADR-031, `--target agentcore`), leída como
datos. Fija lo que `cfn-lint` no comprueba: los parámetros y las salidas que
usan los SDKs (`testdata/agentcore-deploy/contract.json`), los valores por
defecto de `limits.json`, el entorno de CodeBuild, el repositorio inmutable que
se vacía al borrar la pila y el mínimo privilegio del rol del runtime: Bedrock
y Secrets Manager sólo cuando se listan, sin `GetWorkloadAccessTokenForUserId`
y con `aws:SourceAccount` y `aws:SourceArn` en la confianza."""

from __future__ import annotations

import json
from typing import Any

from cfn_yaml import REPO_ROOT, as_list, load_template

CONTRACT = json.loads(
    (REPO_ROOT / "testdata" / "agentcore-deploy" / "contract.json").read_text(
        encoding="utf-8"
    )
)
CONSTANTS: dict[str, Any] = CONTRACT["constants"]
LIMITS = json.loads((REPO_ROOT / "limits.json").read_text(encoding="utf-8"))
RUNTIME_ROLE = "RuntimeRole"
BUILD_ROLE = "BuildRole"
CONDITIONAL_FUNCTION = "Fn::If"
TRUE_BRANCH = 1
USER_ID_TOKEN_ACTION = "bedrock-agentcore:GetWorkloadAccessTokenForUserId"


def template() -> dict[str, Any]:
    return load_template("agentcore-runtime")


def resource(name: str) -> dict[str, Any]:
    return dict(template()["Resources"][name])


def statements(role: str) -> list[dict[str, Any]]:
    """Las sentencias del rol, con la rama verdadera de cada `!If`."""
    flat: list[dict[str, Any]] = []
    for policy in resource(role)["Properties"]["Policies"]:
        for item in policy["PolicyDocument"]["Statement"]:
            flat.append(
                item[CONDITIONAL_FUNCTION][TRUE_BRANCH]
                if CONDITIONAL_FUNCTION in item
                else item
            )
    return flat


def test_parameters_and_outputs_are_the_contract_ones() -> None:
    assert set(template()["Parameters"]) == set(CONSTANTS["stackParameters"].values())
    assert set(template()["Outputs"]) == set(CONSTANTS["stackOutputs"].values())


def test_defaults_come_from_limits() -> None:
    parameters = template()["Parameters"]
    assert (
        parameters["ImageRetentionCount"]["Default"]
        == LIMITS["agentcoreImageRetentionCount"]
    )
    assert (
        parameters["BuildLogRetentionDays"]["Default"]
        == LIMITS["agentcoreBuildLogRetentionDays"]
    )
    assert (
        parameters["BuildTimeoutMinutes"]["Default"]
        == LIMITS["agentcoreBuildTimeoutMinutes"]
    )
    assert parameters["AgentName"]["MaxLength"] == CONSTANTS["agentNameMaxLength"]
    assert parameters["AgentName"]["AllowedPattern"] == CONSTANTS["agentNamePattern"]
    assert (
        parameters["RuntimeName"]["MaxLength"]
        == LIMITS["agentcoreRuntimeNameMaxLength"]
    )


def test_the_build_runs_on_arm_with_docker() -> None:
    environment = resource("BuildProject")["Properties"]["Environment"]
    assert environment["Type"] == LIMITS["agentcoreBuildEnvironmentType"]
    assert environment["Image"] == LIMITS["agentcoreBuildImage"]
    assert environment["ComputeType"] == LIMITS["agentcoreBuildComputeType"]
    assert environment["PrivilegedMode"] is True
    buildspec = resource("BuildProject")["Properties"]["Source"]["BuildSpec"]
    for variable in CONSTANTS["buildEnvironmentVariables"].values():
        assert f"${variable}" in buildspec
    assert "--platform linux/arm64" in buildspec


def test_the_repository_is_immutable_and_emptied_with_the_stack() -> None:
    repository = resource("Repository")
    properties = repository["Properties"]
    assert properties["ImageTagMutability"] == "IMMUTABLE"
    assert properties["EmptyOnDelete"] is True
    assert properties["ImageScanningConfiguration"] == {"ScanOnPush": True}
    assert repository["DeletionPolicy"] == "Delete"


def test_the_runtime_role_is_assumed_only_by_agentcore_of_this_account() -> None:
    trust = resource(RUNTIME_ROLE)["Properties"]["AssumeRolePolicyDocument"][
        "Statement"
    ]
    assert len(trust) == 1
    assert trust[0]["Principal"] == {"Service": "bedrock-agentcore.amazonaws.com"}
    assert set(trust[0]["Condition"]) == {"StringEquals", "ArnLike"}
    assert "aws:SourceAccount" in trust[0]["Condition"]["StringEquals"]
    assert "aws:SourceArn" in trust[0]["Condition"]["ArnLike"]


def test_bedrock_and_secrets_only_when_listed() -> None:
    by_sid = {item.get("Sid"): item for item in statements(RUNTIME_ROLE)}
    assert by_sid["InvokeBedrockModels"]["Resource"] == {"Ref": "BedrockModelArns"}
    assert by_sid["ReadSecrets"]["Resource"] == {"Ref": "ReadableSecretArns"}
    raw = resource(RUNTIME_ROLE)["Properties"]["Policies"][0]["PolicyDocument"][
        "Statement"
    ]
    conditional = [
        item[CONDITIONAL_FUNCTION][0] for item in raw if CONDITIONAL_FUNCTION in item
    ]
    assert conditional == ["HasBedrockModels", "HasReadableSecrets"]


def test_the_runtime_role_has_no_wildcard_bedrock_or_user_id_tokens() -> None:
    actions = {
        action
        for item in statements(RUNTIME_ROLE)
        if item["Effect"] == "Allow"
        for action in as_list(item["Action"])
    }
    assert USER_ID_TOKEN_ACTION not in actions
    assert not any(action.endswith(":*") for action in actions)
    for item in statements(RUNTIME_ROLE):
        if any(action.startswith("bedrock:") for action in as_list(item["Action"])):
            assert item["Resource"] != "*"


def test_the_build_role_reads_only_the_agent_source_prefix() -> None:
    by_sid = {item.get("Sid"): item for item in statements(BUILD_ROLE)}
    source = json.dumps(by_sid["ReadSource"]["Resource"])
    assert LIMITS["agentcoreSourcePrefix"] in source
    assert "${AgentName}/*" in source
