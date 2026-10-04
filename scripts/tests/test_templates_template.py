"""`infra/templates.yaml` (m15-templates), leída como datos: una sola
política IAM gestionada (coste $0); las acciones de imagen acotadas a las
imágenes de la cuenta y la región, y `Resource: "*"` sólo para
`CreateMicrovmImage`, que AWS autoriza sobre `*` (Q114); un `Deny` que
impide actualizar las imágenes base publicadas; ningún
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
        "lambda:PassNetworkConnector",
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


def test_only_create_microvm_image_grants_every_resource() -> None:
    """AWS autoriza `CreateMicrovmImage` sobre `*`, no sobre el ARN de la
    imagen nueva (Q114): es la única acción que puede ir sin acotar."""
    for sid, statement in statements().items():
        if statement["Resource"] == "*":
            assert statement["Action"] == ["lambda:CreateMicrovmImage"], sid


def test_image_actions_are_scoped_to_this_accounts_images() -> None:
    build = statements()["BuildMicrovmImages"]
    assert build["Resource"] == {"Fn::Sub": f"{IMAGE_ARN}*"}


def test_the_published_base_images_can_never_be_overwritten() -> None:
    deny = statements()["NeverOverwriteBaseImages"]
    assert deny["Effect"] == "Deny"
    # Sólo update: create va sobre `*` (Q114) y no se puede acotar por nombre;
    # create sobre un nombre existente falla, así que no sobrescribe nada.
    assert deny["Action"] == ["lambda:UpdateMicrovmImage"]
    assert deny["Resource"] == {"Fn::Sub": f"{IMAGE_ARN}${{ProtectedImageNamePrefix}}*"}
    assert template()["Parameters"]["ProtectedImageNamePrefix"]["Default"] == "rayito-base"


def test_only_the_aws_managed_network_connectors_can_be_passed() -> None:
    passes = statements()["PassManagedNetworkConnectors"]
    assert passes["Action"] == ["lambda:PassNetworkConnector"]
    assert passes["Resource"] == {
        "Fn::Sub": "arn:${AWS::Partition}:lambda:${AWS::Region}:aws:network-connector:"
        "aws-network-connector:*"
    }


def test_the_base_artifact_read_falls_back_to_the_artifact_bucket() -> None:
    resource = statements()["ReadBaseImageArtifacts"]["Resource"]
    _condition, when_set, when_empty = resource["Fn::If"]
    assert when_set == {"Fn::Sub": "${BaseImageBucketArn}/rayito/*"}
    assert when_empty == {"Fn::Sub": "${ArtifactBucketArn}/rayito/*"}


def _s3_resources(value: Any) -> list[str]:
    """Every `Fn::Sub` string of a statement's `Resource`, through `Fn::If`."""
    if isinstance(value, list):
        return [found for item in value for found in _s3_resources(item)]
    if isinstance(value, dict):
        if "Fn::Sub" in value:
            return [value["Fn::Sub"]]
        if "Fn::If" in value:
            return [found for branch in value["Fn::If"][1:] for found in _s3_resources(branch)]
    return [value] if isinstance(value, str) else []


def test_no_s3_statement_reaches_beyond_the_rayito_namespace() -> None:
    # The artifact bucket may also be the persistence and transfer bucket
    # (infra/iam.yaml supports it): `<bucket>/*` would let the builder read
    # every sandbox's HOME checkpoint and every staged transfer.
    for sid, statement in statements().items():
        actions = statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]]
        if not any(action.startswith("s3:") for action in actions):
            continue
        for resource in _s3_resources(statement["Resource"]):
            assert not resource.endswith("Arn}/*"), (sid, resource)
            assert "/rayito/" in resource, (sid, resource)
