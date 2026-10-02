"""`infra/sizes-guard.yaml` (M15 sizes-catalog), leída como datos: la
`RayitoRunAllowedSizes` que cuenta como guardarraíles de coste necesita, no
sólo un Allow sobre `ImageArns`, sino un Deny de `lambda:RunMicrovm` con
`NotResource: ImageArns` — sin él, adjuntarla a una identidad que ya tenga
el `microvm-image:*` de `infra/iam.yaml`'s `CallerPolicy` (el caso que la
guía documenta) no restringe nada: un Allow explícito en otra política ya
cubre todo, e IAM nunca deniega lo que nadie pidió denegar. `cfn-lint`
valida la forma, no la política: sin este test, perder el Deny no pone
nada en rojo."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = REPO_ROOT / "infra" / "sizes-guard.yaml"
POLICY_NAME = "RayitoRunAllowedSizes"
GUARDED_ACTION = "lambda:RunMicrovm"


class TemplateLoader(yaml.SafeLoader):
    """`SafeLoader` con las etiquetas cortas de CloudFormation (`!Ref`,
    `!Sub`, `!If`…) resueltas a su forma larga."""


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


def statements() -> list[dict[str, Any]]:
    resource = template()["Resources"][POLICY_NAME]
    assert resource["Type"] == "AWS::IAM::ManagedPolicy"
    return list(resource["Properties"]["PolicyDocument"]["Statement"])


def actions_of(statement: dict[str, Any]) -> set[str]:
    action = statement["Action"]
    return {action} if isinstance(action, str) else set(action)


def test_only_one_resource_and_it_is_the_managed_policy() -> None:
    assert set(template()["Resources"]) == {POLICY_NAME}


def test_an_allow_statement_grants_run_microvm_on_the_listed_arns() -> None:
    allows = [s for s in statements() if s["Effect"] == "Allow"]
    assert len(allows) == 1
    (allow,) = allows
    assert actions_of(allow) == {GUARDED_ACTION}
    assert allow["Resource"] == {"Ref": "ImageArns"}


def test_a_deny_statement_blocks_run_microvm_outside_the_listed_arns() -> None:
    """The Allow alone cannot restrict anything once a broader grant (the
    standard CallerPolicy's `microvm-image:*`) is already attached to the
    same identity: IAM only denies what an explicit Deny names. A Deny with
    `NotResource` is what makes this policy effective regardless of what
    else the identity can already do."""
    denies = [s for s in statements() if s["Effect"] == "Deny"]
    assert len(denies) == 1
    (deny,) = denies
    assert actions_of(deny) == {GUARDED_ACTION}
    assert "Resource" not in deny
    assert deny["NotResource"] == {"Ref": "ImageArns"}


def test_both_statements_act_on_exactly_the_same_action() -> None:
    """A Deny that doesn't cover every action the Allow grants would leave
    an ungated one; an Allow that grants more than the Deny denies would
    reintroduce the same gap this guard exists to close."""
    actions = {action for statement in statements() for action in actions_of(statement)}
    assert actions == {GUARDED_ACTION}


def test_image_arns_is_the_only_parameter_and_has_no_default() -> None:
    (name, parameter) = next(iter(template()["Parameters"].items()))
    assert len(template()["Parameters"]) == 1
    assert name == "ImageArns"
    assert parameter["Type"] == "CommaDelimitedList"
    assert "Default" not in parameter
