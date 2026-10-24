"""`infra/quota-alerts.yaml`, leída como datos: sólo lectura sobre MicroVMs,
una métrica acotada a su espacio de nombres, un único tema al que publicar y
los números iguales a los de `limits.json`."""

from __future__ import annotations

import json
import sys
from typing import Any

from cfn_yaml import (
    REPO_ROOT,
    SDK_SOURCE,
    allowed_actions,
    as_list,
    load_template,
    policy_statements,
)

if str(SDK_SOURCE) not in sys.path:
    sys.path.insert(0, str(SDK_SOURCE))

from rayito._stacks.components.quota_alerts import COMPONENT

LIMITS = json.loads((REPO_ROOT / "limits.json").read_text(encoding="utf-8"))
NAME = "quota-alerts"
FORBIDDEN_TYPES = frozenset({"AWS::CloudFront::Distribution", "AWS::S3::Bucket"})
WRITE_ACTIONS = frozenset(
    {"lambda:TerminateMicrovm", "lambda:SuspendMicrovm", "lambda:ResumeMicrovm"}
)
PERCENT = 100


def resources() -> dict[str, Any]:
    return dict(load_template(NAME)["Resources"])


def test_it_creates_no_cloudfront_distribution_or_bucket() -> None:
    assert not {r["Type"] for r in resources().values()} & FORBIDDEN_TYPES
    code = resources()["CheckFunction"]["Properties"]["Code"]
    assert "S3Key" in code and "ZipFile" not in code


def test_the_role_reads_microvms_and_cannot_act_on_them() -> None:
    actions = allowed_actions(resources()["FunctionRole"])
    assert not actions & WRITE_ACTIONS
    assert not any(action.endswith("*") for action in actions)
    assert {"lambda:ListMicrovms", "servicequotas:ListServiceQuotas"} <= actions


def test_the_metric_can_only_go_to_its_own_namespace() -> None:
    (statement,) = [
        s
        for s in policy_statements(resources()["FunctionRole"])
        if s["Sid"] == "PublishUsageMetric"
    ]
    assert statement["Action"] == "cloudwatch:PutMetricData"
    assert statement["Condition"] == {
        "StringEquals": {"cloudwatch:namespace": LIMITS["quotaMetricNamespace"]}
    }


def test_the_function_publishes_only_to_its_topic_and_writes_only_its_state_row() -> None:
    by_sid = {s["Sid"]: s for s in policy_statements(resources()["FunctionRole"])}
    assert by_sid["PublishAlert"]["Action"] == "sns:Publish"
    assert by_sid["PublishAlert"]["Resource"] != "*"
    assert set(as_list(by_sid["KeepLastLevel"]["Action"])) == {
        "dynamodb:GetItem",
        "dynamodb:PutItem",
    }
    assert by_sid["KeepLastLevel"]["Resource"] == {"Fn::GetAtt": "StateTable.Arn"}


def test_the_numbers_equal_limits_json() -> None:
    template = load_template(NAME)
    function = template["Resources"]["CheckFunction"]["Properties"]
    assert function["Timeout"] == LIMITS["quotaAlertsLambdaTimeoutSeconds"]
    assert function["MemorySize"] == LIMITS["quotaAlertsLambdaMemoryMib"]
    parameters = template["Parameters"]
    assert parameters["ScheduleExpression"]["Default"] == LIMITS["quotaAlertsScheduleExpression"]
    assert parameters["WarningPercent"]["Default"] == LIMITS["quotaWarningFraction"] * PERCENT
    assert parameters["CriticalPercent"]["Default"] == LIMITS["quotaCriticalFraction"] * PERCENT


def test_component_parameters_and_defaults_equal_the_template() -> None:
    parameters = load_template(NAME)["Parameters"]
    own = {p.name: p for p in COMPONENT.parameters}
    assert set(own) == set(parameters) - {"ArtifactS3Key"}
    for name, parameter in own.items():
        if parameter.default is not None:
            assert str(parameters[name]["Default"]) == parameter.default, name


def test_the_schedule_targets_the_function_and_only_events_may_invoke_it() -> None:
    rule = resources()["CheckSchedule"]["Properties"]
    assert rule["Targets"][0]["Arn"] == {"Fn::GetAtt": "CheckFunction.Arn"}
    permission = resources()["ScheduleInvokePermission"]["Properties"]
    assert permission["Principal"] == "events.amazonaws.com"
    assert permission["SourceArn"] == {"Fn::GetAtt": "CheckSchedule.Arn"}


def test_the_state_table_is_on_demand() -> None:
    assert resources()["StateTable"]["Properties"]["BillingMode"] == "PAY_PER_REQUEST"


def test_the_function_environment_names_what_the_handler_reads() -> None:
    names = set(resources()["CheckFunction"]["Properties"]["Environment"]["Variables"])
    config = (REPO_ROOT / "infra/lambdas/quota_alerts/quota_alert_check/config.py").read_text(
        encoding="utf-8"
    )
    for name in names - {"AWS_DATA_PATH"}:
        assert f'"{name}"' in config, name
