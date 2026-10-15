"""`infra/dashboard-read-role.yaml`: el rol de la cuenta destino sólo lee, no
confía en una raíz de cuenta y su `ExternalId` es opcional."""

from __future__ import annotations

import re
import sys
from typing import Any

from cfn_yaml import SDK_SOURCE, allowed_actions, load_template

if str(SDK_SOURCE) not in sys.path:
    sys.path.insert(0, str(SDK_SOURCE))

from rayito._limits import DASHBOARD_ASSUME_ROLE_SESSION_SECONDS
from rayito._stacks.components.dashboard_read_role import COMPONENT

NAME = "dashboard-read-role"
READ_ACTIONS = frozenset(
    {
        "lambda:ListMicrovms",
        "lambda:ListMicrovmImages",
        "lambda:ListMicrovmImageVersions",
        "lambda:GetMicrovmImageVersion",
        "servicequotas:ListServiceQuotas",
    }
)


def role() -> dict[str, Any]:
    resources = load_template(NAME)["Resources"]
    assert set(resources) == {"ReadRole"}
    return dict(resources["ReadRole"])


def test_it_allows_exactly_the_reads_the_panel_makes() -> None:
    assert allowed_actions(role()) == READ_ACTIONS


def test_it_trusts_the_parameter_and_requires_the_external_id_only_when_given() -> None:
    (statement,) = role()["Properties"]["AssumeRolePolicyDocument"]["Statement"]
    assert statement["Principal"] == {"AWS": {"Ref": "TrustedPrincipalArn"}}
    assert statement["Action"] == "sts:AssumeRole"
    condition, with_id, without_id = statement["Condition"]["Fn::If"]
    assert condition == "RequireExternalId"
    assert with_id == {"StringEquals": {"sts:ExternalId": {"Ref": "ExternalId"}}}
    assert without_id == {"Ref": "AWS::NoValue"}


def test_the_trusted_principal_must_be_a_role_or_user_never_a_root() -> None:
    pattern = load_template(NAME)["Parameters"]["TrustedPrincipalArn"]["AllowedPattern"]
    good = "arn:aws:iam::123456789012:role/rayito-dashboard"
    assert re.fullmatch(pattern, good)
    assert not re.fullmatch(pattern, "arn:aws:iam::123456789012:root")
    assert not re.fullmatch(pattern, "123456789012")


def test_the_external_id_is_not_echoed() -> None:
    assert load_template(NAME)["Parameters"]["ExternalId"]["NoEcho"] is True


def test_component_parameters_and_defaults_equal_the_template() -> None:
    parameters = load_template(NAME)["Parameters"]
    assert {p.name for p in COMPONENT.parameters} == set(parameters)
    for parameter in COMPONENT.parameters:
        if parameter.default is not None:
            assert str(parameters[parameter.name]["Default"]) == parameter.default
        assert bool(parameters[parameter.name].get("NoEcho")) == parameter.no_echo


def test_the_max_session_default_is_the_session_the_panel_asks_for() -> None:
    default = load_template(NAME)["Parameters"]["MaxSessionSeconds"]["Default"]
    assert int(default) == DASHBOARD_ASSUME_ROLE_SESSION_SECONDS
