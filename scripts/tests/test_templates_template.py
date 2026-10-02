"""`infra/templates.yaml` (m15-templates), leída como datos: una sola
política IAM gestionada (coste $0); las acciones de imagen acotadas a las
imágenes de la cuenta y la región, nunca `Resource: "*"`; un `Deny` que
impide crear o actualizar las imágenes base publicadas; ningún
`s3:GetObject` sobre todos los buckets; sin acciones inexistentes
(`s3:HeadObject`) ni sin usar (`*MicrovmImageBuild*`)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = REPO_ROOT / "infra" / "templates.yaml"
IMAGE_ARN = "arn:${AWS::Partition}:lambda:${AWS::Region}:${AWS::AccountId}:microvm-image:"
ALLOWED_ACTIONS = frozenset(
    {
        "lambda:CreateMicrovmImage",
        "lambda:UpdateMicrovmImage",
        "lambda:GetMicrovmImage",
        "lambda:GetMicrovmImageVersion",
        "lambda:ListMicrovmImageVersions",
        "iam:PassRole",
        "s3:GetObject",
        "s3:PutObject",
        "logs:DescribeLogStreams",
        "logs:GetLogEvents",
    }
)


class TemplateLoader(yaml.SafeLoader):
    """`SafeLoader` con las etiquetas cortas de CloudFormation resueltas a
    su forma larga (igual que `test_secrets_template.py`)."""


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


def statements() -> dict[str, dict[str, Any]]:
    policy = template()["Resources"]["RayitoTemplateBuilder"]["Properties"]["PolicyDocument"]
    return {statement["Sid"]: statement for statement in policy["Statement"]}


def test_only_one_managed_policy_is_created() -> None:
    resources = template()["Resources"]
    assert [resource["Type"] for resource in resources.values()] == ["AWS::IAM::ManagedPolicy"]


def test_every_action_is_on_the_allow_list() -> None:
    for statement in statements().values():
        assert set(statement["Action"]) <= ALLOWED_ACTIONS, statement["Sid"]


def test_no_statement_grants_every_resource() -> None:
    for statement in statements().values():
        assert statement["Resource"] != "*", statement["Sid"]


def test_image_actions_are_scoped_to_this_accounts_images() -> None:
    build = statements()["BuildMicrovmImages"]
    assert build["Resource"] == {"Fn::Sub": f"{IMAGE_ARN}*"}


def test_the_published_base_images_can_never_be_overwritten() -> None:
    deny = statements()["NeverOverwriteBaseImages"]
    assert deny["Effect"] == "Deny"
    assert set(deny["Action"]) == {"lambda:CreateMicrovmImage", "lambda:UpdateMicrovmImage"}
    assert deny["Resource"] == {"Fn::Sub": f"{IMAGE_ARN}${{ProtectedImageNamePrefix}}*"}
    assert template()["Parameters"]["ProtectedImageNamePrefix"]["Default"] == "rayito-base"


def test_the_base_artifact_read_falls_back_to_the_artifact_bucket() -> None:
    resource = statements()["ReadBaseImageArtifacts"]["Resource"]
    _condition, when_set, when_empty = resource["Fn::If"]
    assert when_set == {"Fn::Sub": "${BaseImageBucketArn}/*"}
    assert when_empty == {"Fn::Sub": "${ArtifactBucketArn}/*"}
