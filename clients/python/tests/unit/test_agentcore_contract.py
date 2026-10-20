"""`testdata/agentcore-deploy/contract.json` frente al código de Python: los
códigos de error y sus mensajes, los textos de la salida, los nombres de los
parámetros y salidas de la pila y la tabla de opciones que no aplican a
AgentCore. Si discrepan, gana el contrato."""

from __future__ import annotations

from typing import Any

import pytest

from rayito._agentcore import _domain as domain
from rayito._agentcore import _iam_resources as iam
from rayito._agentcore import _messages as messages
from rayito._agentcore._errors import ERROR_EXIT_CODES, ERROR_MESSAGES
from rayito._agentcore._stack import AGENTCORE_STACK_COMPONENT
from rayito._limits import AGENTCORE_IMAGE_RETENTION_COUNT
from tests.unit.fake_agentcore import CONTRACT

CONSTANTS: dict[str, Any] = CONTRACT["constants"]
MESSAGES: dict[str, Any] = CONTRACT["messages"]


def test_error_codes_exit_codes_and_messages_are_the_contracts() -> None:
    assert dict(ERROR_EXIT_CODES) == CONTRACT["errorExitCodes"]
    assert dict(ERROR_MESSAGES) == MESSAGES["errors"]


@pytest.mark.parametrize(
    ("name", "key"),
    [
        ("HEADER_TEMPLATE", "header"),
        ("STEP_TEMPLATE", "step"),
        ("STACK_CONTENTS", "stackContents"),
        ("SECURITY_HEADER", "securityHeader"),
        ("WARNINGS_HEADER", "warningsHeader"),
        ("WARNING_LINE_TEMPLATE", "warningLine"),
        ("SECURITY_LINE_TEMPLATE", "securityLine"),
        ("COST_HEADER", "costHeader"),
        ("COST_LINE_TEMPLATE", "costLine"),
        ("DRY_RUN_FOOTER", "dryRunFooter"),
        ("CONFIRM_QUESTION", "confirm"),
        ("LIST_LINE_TEMPLATE", "listLine"),
        ("LIST_EMPTY_TEMPLATE", "listEmpty"),
        ("DELETE_HEADER_TEMPLATE", "deleteHeader"),
        ("DELETE_RUNTIME_TEMPLATE", "deleteRuntime"),
        ("DELETE_STACK_TEMPLATE", "deleteStack"),
        ("DELETE_LOG_GROUP_TEMPLATE", "deleteLogGroups"),
        ("DELETE_CONFIRM_QUESTION", "deleteConfirm"),
        ("DELETE_NOTHING_TEMPLATE", "deleteNothing"),
        ("DELETED_TEMPLATE", "deleted"),
        ("STOPPED_TEMPLATE", "stopped"),
        ("ERROR_PREFIX", "errorPrefix"),
    ],
)
def test_single_texts_are_the_contracts(name: str, key: str) -> None:
    assert getattr(messages, name) == MESSAGES[key]


@pytest.mark.parametrize(
    ("name", "key"),
    [
        ("RESOURCE_LABELS", "labels"),
        ("ACTION_TEXTS", "actions"),
        ("SECURITY_TEXTS", "security"),
        ("WARNING_TEXTS", "warnings"),
        ("PROGRESS_MESSAGES", "progress"),
        ("DELETE_PROGRESS_MESSAGES", "deleteProgress"),
    ],
)
def test_text_tables_are_the_contracts(name: str, key: str) -> None:
    assert dict(getattr(messages, name)) == MESSAGES[key]


def test_deployed_and_status_lines_are_the_contracts() -> None:
    assert list(messages.DEPLOYED_LINES) == MESSAGES["deployed"]
    assert list(messages.STATUS_LINES) == MESSAGES["status"]


def test_the_cost_lines_render_the_contracts_texts_from_limits() -> None:
    expected = [
        MESSAGES["cost"][key].format(retained=AGENTCORE_IMAGE_RETENTION_COUNT)
        for key in domain.COST_KEYS
    ]
    assert list(domain.cost_lines()) == expected


def test_stack_parameters_and_outputs_are_the_contracts() -> None:
    parameters = CONSTANTS["stackParameters"]
    assert {parameter.name for parameter in AGENTCORE_STACK_COMPONENT.parameters} == set(
        parameters.values()
    )
    assert list(domain.STACK_OUTPUT_KEYS) == list(CONSTANTS["stackOutputs"].values())
    assert parameters["agentName"] == domain.STACK_PARAMETER_AGENT_NAME
    assert parameters["artifactBucket"] == domain.STACK_PARAMETER_BUCKET
    assert parameters["bedrockModelArns"] == domain.STACK_PARAMETER_MODEL_ARNS
    assert parameters["readableSecretArns"] == domain.STACK_PARAMETER_SECRET_ARNS
    assert AGENTCORE_STACK_COMPONENT.name == CONSTANTS["stackComponentName"]
    assert list(AGENTCORE_STACK_COMPONENT.capabilities) == CONSTANTS["stackCapabilities"]


def test_iam_resource_rules_are_the_contracts() -> None:
    rules = CONSTANTS["iamResourceRules"]
    assert iam.SECRET_NAME_PATTERN.pattern == rules["secretNamePattern"].strip("^$")
    assert rules["secretNameMaxLength"] == iam.SECRET_NAME_MAX_LENGTH
    assert iam.MODEL_ID_PATTERN.pattern == rules["modelIdPattern"].strip("^$")
    assert iam.ACCOUNT_PATTERN.pattern == rules["accountPattern"].strip("^$")


def test_names_and_states_are_the_contracts() -> None:
    assert domain.AGENT_NAME_PATTERN.pattern == CONSTANTS["agentNamePattern"].strip("^$")
    assert CONSTANTS["agentNameMaxLength"] == domain.AGENT_NAME_MAX_LENGTH
    assert list(domain.REGION_ENV_VARIABLES) == CONSTANTS["runtimeRegionEnvironmentVariables"]
    assert CONSTANTS["runtimeReadyState"] == domain.RUNTIME_READY
    assert set(CONSTANTS["runtimeInProgressStates"]) == domain.RUNTIME_IN_PROGRESS
    assert set(CONSTANTS["runtimeBlockedStates"]) == domain.RUNTIME_BLOCKED
    assert set(CONSTANTS["runtimeRepairableStates"]) == domain.RUNTIME_REPAIRABLE
    assert CONSTANTS["buildSucceededState"] == domain.BUILD_SUCCEEDED
    assert domain.SESSION_ID_PATTERN.pattern == CONSTANTS["sessionIdRules"]["pattern"].strip("^$")
    assert CONSTANTS["awsPartition"] == domain.AWS_PARTITION
    assert CONSTANTS["secretArnTemplate"] == domain.SECRET_ARN_TEMPLATE
    assert CONSTANTS["regionalFoundationModelArnTemplate"] == domain.FOUNDATION_MODEL_ARN_TEMPLATE
    assert CONSTANTS["anyRegionFoundationModelArnTemplate"] == domain.ANY_REGION_MODEL_ARN_TEMPLATE
    assert CONSTANTS["globalFoundationModelArnTemplate"] == domain.GLOBAL_MODEL_ARN_TEMPLATE
    assert CONSTANTS["inferenceProfileArnTemplate"] == domain.INFERENCE_PROFILE_ARN_TEMPLATE


def test_every_option_that_errors_in_the_contract_is_checked_with_its_reason() -> None:
    contract_reasons = [
        row["reason"] for row in CONTRACT["optionsTable"]["rows"] if row["call"] == "error"
    ]
    checked = [option.reason for option in domain.CALL_OPTIONS]
    position = 0
    for reason in checked:
        position = contract_reasons.index(reason, position)


def test_the_options_table_keeps_the_order_of_the_contract() -> None:
    names = [option.name for option in domain.CALL_OPTIONS]
    assert names[:4] == [
        "scaling.min_vms",
        "scaling.max_vms",
        "scaling.warm_vms",
        "scaling.scale_in_after_seconds",
    ]
    assert names.index("memory_mib") < names.index("secret_names") < names.index("pool_backend")
