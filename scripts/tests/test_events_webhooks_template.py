"""`infra/events-webhooks.yaml` (M15, m15-events-webhooks), leída como
datos: el secreto HMAC, la tabla DynamoDB (streams, GSI1), las tres Lambdas
(forwarder/deliverer/reconciler) y sus roles de mínimo privilegio, la
suscripción de CloudWatch Logs y la regla de EventBridge Scheduler.
`cfn-lint` valida la forma, no la política (ver `make infra-lint`)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = REPO_ROOT / "infra" / "events-webhooks.yaml"
LAMBDA_ROOT = REPO_ROOT / "infra" / "lambdas" / "events_webhooks"


class TemplateLoader(yaml.SafeLoader):
    pass


def construct_short_tag(loader: yaml.SafeLoader, suffix: str, node: yaml.Node) -> Any:
    name = suffix if suffix in {"Ref", "Condition"} else f"Fn::{suffix}"
    if isinstance(node, yaml.ScalarNode):
        return {name: loader.construct_scalar(node)}
    if isinstance(node, yaml.SequenceNode):
        return {name: loader.construct_sequence(node, deep=True)}
    return {name: loader.construct_mapping(node, deep=True)}


TemplateLoader.add_multi_constructor("!", construct_short_tag)


def template() -> dict[str, Any]:
    document = yaml.load(TEMPLATE.read_text(encoding="utf-8"), Loader=TemplateLoader)
    assert isinstance(document, dict)
    return document


def resources() -> dict[str, Any]:
    return dict(template()["Resources"])


def test_the_three_lambdas_exist_with_their_own_handler() -> None:
    expected_handlers = {
        "ForwarderFunction": "handlers.forwarder.handler",
        "DelivererFunction": "handlers.deliverer.handler",
        "ReconcilerFunction": "handlers.reconciler.handler",
    }
    found = resources()
    for name, handler in expected_handlers.items():
        assert found[name]["Type"] == "AWS::Lambda::Function"
        assert found[name]["Properties"]["Handler"] == handler
        assert found[name]["Properties"]["Runtime"] == "python3.12"


def test_the_table_is_on_demand_with_streams_and_a_sparse_gsi() -> None:
    table = resources()["EventsTable"]["Properties"]
    assert table["BillingMode"] == "PAY_PER_REQUEST"
    assert table["StreamSpecification"] == {"StreamViewType": "NEW_IMAGE"}
    gsi_names = [gsi["IndexName"] for gsi in table["GlobalSecondaryIndexes"]]
    assert gsi_names == ["gsi1"]
    assert table["TimeToLiveSpecification"] == {
        "AttributeName": "expires_at",
        "Enabled": True,
    }


def test_the_secret_has_no_hard_coded_value() -> None:
    secret = resources()["StackKeySecret"]["Properties"]
    assert "GenerateSecretString" in secret
    assert "SecretString" not in secret


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def role_actions(role: str) -> set[str]:
    policies = resources()[role]["Properties"]["Policies"]
    actions: set[str] = set()
    for policy in policies:
        for statement in policy["PolicyDocument"]["Statement"]:
            actions.update(as_list(statement["Action"]))
    return actions


def test_each_lambda_role_is_scoped_to_what_it_needs() -> None:
    forwarder = role_actions("ForwarderRole")
    assert forwarder == {
        "dynamodb:PutItem",
        "secretsmanager:GetSecretValue",
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents",
    }
    deliverer = role_actions("DelivererRole")
    assert "lambda:ListMicrovms" not in deliverer
    assert "dynamodb:Scan" not in deliverer
    reconciler = role_actions("ReconcilerRole")
    assert "lambda:ListMicrovms" in reconciler
    assert "secretsmanager:GetSecretValue" not in reconciler


def test_no_statement_grants_a_wildcard_dynamodb_or_secret_resource() -> None:
    # `lambda:ListMicrovms` is the one legitimate exception: AWS defines no
    # resource-level permissions for it (it lists every MicroVM in the
    # account), so `Resource: "*"` there is correct, not a finding.
    for role in ("ForwarderRole", "DelivererRole", "ReconcilerRole"):
        policies = resources()[role]["Properties"]["Policies"]
        for policy in policies:
            for statement in policy["PolicyDocument"]["Statement"]:
                if as_list(statement["Action"]) == ["lambda:ListMicrovms"]:
                    continue
                for resource in as_list(statement.get("Resource", [])):
                    if isinstance(resource, str):
                        assert resource != "*", (role, statement)


def test_the_reconciler_schedule_targets_the_reconciler_function() -> None:
    schedule = resources()["ReconcilerSchedule"]["Properties"]
    assert schedule["Target"]["Arn"] == {"Fn::GetAtt": "ReconcilerFunction.Arn"}
    assert schedule["ScheduleExpression"] == {
        "Fn::Sub": "rate(${ReconcilerIntervalMinutes} minutes)"
    }


def test_the_interval_parameter_matches_the_sdk_constants() -> None:
    # `rate(N minutes)` is rejected by Scheduler for N = 1 (it wants the
    # singular `minute`), hence the SDKs' minimum of 2; the default is the
    # SDKs' own constant, never a second literal.
    from rayito._lifecycle_events._domain import (
        DEFAULT_RECONCILER_INTERVAL_MINUTES,
        MIN_RECONCILER_INTERVAL_MINUTES,
    )

    parameter = template()["Parameters"]["ReconcilerIntervalMinutes"]
    assert parameter["Default"] == DEFAULT_RECONCILER_INTERVAL_MINUTES
    assert parameter["MinValue"] == MIN_RECONCILER_INTERVAL_MINUTES


def test_the_reconciler_gets_its_interval_and_model_path_as_env_vars() -> None:
    # The dedupe window (`domain/dedupe.py`) reads the same interval the
    # schedule runs on; `AWS_DATA_PATH` points botocore at the bundled
    # `lambda-microvms` model before any client exists (decision 8).
    variables = resources()["ReconcilerFunction"]["Properties"]["Environment"]["Variables"]
    assert variables["RECONCILER_INTERVAL_MINUTES"] == {"Ref": "ReconcilerIntervalMinutes"}
    assert variables["AWS_DATA_PATH"] == "/var/task/models"


def test_the_deliverer_reports_partial_batch_failures_and_keeps_failed_records() -> None:
    mapping = resources()["DelivererEventSourceMapping"]["Properties"]
    assert mapping["FunctionResponseTypes"] == ["ReportBatchItemFailures"]
    assert mapping["BisectBatchOnFunctionError"] is True
    assert mapping["DestinationConfig"]["OnFailure"]["Destination"] == {
        "Fn::GetAtt": "DelivererFailuresQueue.Arn"
    }


def test_the_operator_policy_covers_every_sdk_call() -> None:
    statements = resources()["EventsOperatorPolicy"]["Properties"]["PolicyDocument"]["Statement"]
    actions = {action for statement in statements for action in as_list(statement["Action"])}
    assert {
        "secretsmanager:GetSecretValue",
        "dynamodb:PutItem",
        "dynamodb:Query",
        "dynamodb:DeleteItem",
        "cloudformation:DescribeStacks",
    } <= actions
    table_statement = next(
        statement for statement in statements if "dynamodb:Query" in as_list(statement["Action"])
    )
    assert {"Fn::Sub": "${EventsTable.Arn}/index/gsi1"} in table_statement["Resource"]


def test_no_account_id_or_bucket_name_is_hard_coded() -> None:
    text = TEMPLATE.read_text(encoding="utf-8")
    assert "123456789012" not in text
    for digits in ("111111111111", "999999999999"):
        assert digits not in text
