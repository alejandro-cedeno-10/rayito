"""`infra/dashboard-cloudwatch.yaml` dibuja exactamente el dashboard que
imprime `rayito dashboard cloudwatch`: su `DashboardBody` está generado por el
SDK (sin deriva) y, evaluado con cada combinación de opciones, es igual al
JSON del generador. Sólo crea el dashboard."""

from __future__ import annotations

import json
import re
import sys
from itertools import product
from typing import Any

import pytest
from cfn_yaml import REPO_ROOT, SDK_SOURCE, load_template

for entry in (str(SDK_SOURCE), str(REPO_ROOT / "scripts")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

import gen_cloudwatch_template
from rayito._dashboard._cloudwatch_board import (
    DEFAULT_LOG_GROUP_PREFIX,
    build_board,
)
from rayito._stacks.components.dashboard_cloudwatch import COMPONENT
from rayito._stacks.components.dashboard_cloudwatch import (
    DEFAULT_LOG_GROUP_PREFIX as COMPONENT_LOG_GROUP_PREFIX,
)

REGION = "eu-west-1"
PRESET_PREFIX = "/rayito/"


def resolve(value: Any, enabled: dict[str, bool]) -> str:
    """Resuelve `Fn::If`, `Fn::Join` y `Ref AWS::Region` como CloudFormation. El
    `Fn::Sub` externo no vuelve a sustituir dentro de los valores que inserta:
    un `${AWS::Region}` literal en una variable llegaría tal cual al dashboard."""
    if isinstance(value, str):
        return value
    if "Fn::If" in value:
        condition, when_true, when_false = value["Fn::If"]
        return resolve(when_true if enabled[condition] else when_false, enabled)
    if "Fn::Join" in value:
        separator, parts = value["Fn::Join"]
        return separator.join(resolve(part, enabled) for part in parts)
    assert value == {"Ref": "AWS::Region"}, value
    return REGION


def evaluate_body(include_otlp: bool, include_quota: bool) -> dict[str, Any]:
    """Resuelve el `Fn::Sub` del `DashboardBody` como lo haría CloudFormation."""
    template = load_template("dashboard-cloudwatch")
    text, variables = template["Resources"]["Dashboard"]["Properties"]["DashboardBody"]["Fn::Sub"]
    enabled = {"OtlpWidgets": include_otlp, "QuotaWidgets": include_quota}
    for name, expression in variables.items():
        text = text.replace("${" + name + "}", resolve(expression, enabled))
    text = text.replace("${AWS::Region}", REGION).replace("${LogGroupPrefix}", PRESET_PREFIX)
    body = json.loads(text)
    assert isinstance(body, dict)
    return body


def test_no_widget_reaches_the_dashboard_with_an_unreplaced_region() -> None:
    body = evaluate_body(include_otlp=True, include_quota=True)

    assert "${" not in json.dumps(body)


def test_the_template_body_is_generated_from_the_sdk() -> None:
    current = gen_cloudwatch_template.TEMPLATE.read_text(encoding="utf-8")
    assert gen_cloudwatch_template.render(current) == current


@pytest.mark.parametrize(("include_otlp", "include_quota"), list(product([False, True], repeat=2)))
def test_the_deployed_dashboard_equals_the_printed_one(
    include_otlp: bool, include_quota: bool
) -> None:
    expected = build_board(region=REGION, include_otlp=include_otlp, include_quota=include_quota)
    assert evaluate_body(include_otlp, include_quota) == expected


def test_it_creates_only_the_dashboard() -> None:
    resources = load_template("dashboard-cloudwatch")["Resources"]
    assert {resource["Type"] for resource in resources.values()} == {"AWS::CloudWatch::Dashboard"}


def test_component_parameters_and_defaults_equal_the_template() -> None:
    parameters = load_template("dashboard-cloudwatch")["Parameters"]
    assert {p.name for p in COMPONENT.parameters} == set(parameters)
    for parameter in COMPONENT.parameters:
        assert parameters[parameter.name]["Default"] == parameter.default, parameter.name
    assert parameters["LogGroupPrefix"]["Default"] == DEFAULT_LOG_GROUP_PREFIX
    assert COMPONENT_LOG_GROUP_PREFIX == DEFAULT_LOG_GROUP_PREFIX


def test_the_prefix_parameter_cannot_break_out_of_the_query_quotes() -> None:
    pattern = load_template("dashboard-cloudwatch")["Parameters"]["LogGroupPrefix"][
        "AllowedPattern"
    ]
    assert "'" not in pattern and '"' not in pattern


def test_the_default_dashboard_name_is_the_stack_name_and_a_custom_one_is_kept() -> (
    None
):
    template = load_template("dashboard-cloudwatch")
    name = template["Resources"]["Dashboard"]["Properties"]["DashboardName"]
    condition, custom, fallback = name["Fn::If"]
    assert template["Parameters"]["DashboardName"]["Default"] == ""
    assert template["Conditions"][condition] == {
        "Fn::Not": [{"Fn::Equals": [{"Ref": "DashboardName"}, ""]}]
    }
    assert custom == {"Ref": "DashboardName"}
    assert fallback == {"Ref": "AWS::StackName"}


def test_the_dashboard_name_parameter_accepts_empty_or_a_valid_name() -> None:
    pattern = load_template("dashboard-cloudwatch")["Parameters"]["DashboardName"][
        "AllowedPattern"
    ]
    assert re.fullmatch(pattern, "")
    assert re.fullmatch(pattern, "rayito")
    assert re.fullmatch(pattern, "team_a-board")
    assert not re.fullmatch(pattern, "with space")
