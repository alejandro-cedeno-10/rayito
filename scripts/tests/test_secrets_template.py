"""`infra/secrets-access.yaml` (M13a), leída como datos: sólo dos políticas
IAM gestionadas y ningún otro recurso (coste $0, no crea ningún secreto);
ninguna acción fuera de la lista permitida; ningún `Resource: "*"` salvo
`ListSecrets`; las sentencias KMS sólo existen con `KmsKeyArn` y exigen
`kms:ViaService` de Secrets Manager. `cfn-lint` valida la forma, no la
política: sin estos tests un cambio de una línea ampliaría el acceso."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = REPO_ROOT / "infra" / "secrets-access.yaml"
NO_VALUE = {"Ref": "AWS::NoValue"}
READER_ACTIONS = frozenset(
    {"secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"}
)
ADMIN_ACTIONS = READER_ACTIONS | {
    "secretsmanager:CreateSecret",
    "secretsmanager:PutSecretValue",
    "secretsmanager:UpdateSecret",
    "secretsmanager:DeleteSecret",
    "secretsmanager:ListSecrets",
}
KMS_ACTIONS = frozenset({"kms:Decrypt", "kms:GenerateDataKey"})
ALLOWED_ACTIONS = ADMIN_ACTIONS | KMS_ACTIONS
PREFIX_ARN = "arn:${AWS::Partition}:secretsmanager:${AWS::Region}:${AWS::AccountId}:secret:${SecretPrefix}*"


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


def statements(policy: str) -> list[Any]:
    resource = template()["Resources"][policy]
    return list(resource["Properties"]["PolicyDocument"]["Statement"])


def resolved(statement: Any) -> Iterator[dict[str, Any]]:
    """Las sentencias reales de una entrada, incluidas las dos ramas de un
    `Fn::If` (la rama que hoy no se toma también concede cuando cambia la
    condición)."""
    if isinstance(statement, dict) and "Fn::If" in statement:
        _, when_true, when_false = statement["Fn::If"]
        for branch in (when_true, when_false):
            if branch != NO_VALUE:
                yield from resolved(branch)
    elif isinstance(statement, dict):
        yield statement


def all_statements() -> Iterator[tuple[str, dict[str, Any]]]:
    for policy in ("RayitoSecretsReader", "RayitoSecretsAdmin"):
        for entry in statements(policy):
            for statement in resolved(entry):
                yield policy, statement


def actions_of(statement: dict[str, Any]) -> set[str]:
    action = statement["Action"]
    return {action} if isinstance(action, str) else set(action)


def test_only_two_managed_policies_and_no_secret_is_created() -> None:
    resources = template()["Resources"]
    assert set(resources) == {"RayitoSecretsReader", "RayitoSecretsAdmin"}
    for resource in resources.values():
        assert resource["Type"] == "AWS::IAM::ManagedPolicy"


WEBHOOK_DENY_SID = "NeverWebhookSigningSecrets"


def test_every_action_is_on_the_allow_list_and_every_statement_allows() -> None:
    for policy, statement in all_statements():
        if statement.get("Sid") == WEBHOOK_DENY_SID:
            continue
        assert statement["Effect"] == "Allow", policy
        assert actions_of(statement) <= ALLOWED_ACTIONS, (policy, statement["Sid"])
        assert not any("*" in action for action in actions_of(statement)), statement[
            "Sid"
        ]


def test_reader_and_admin_grant_exactly_their_sets() -> None:
    reader = set().union(
        *(
            actions_of(s)
            for p, s in all_statements()
            if p == "RayitoSecretsReader" and s["Effect"] == "Allow"
        )
    )
    admin = set().union(
        *(actions_of(s) for p, s in all_statements() if p == "RayitoSecretsAdmin")
    )
    assert reader == READER_ACTIONS | {"kms:Decrypt"}
    assert admin == ADMIN_ACTIONS | KMS_ACTIONS


def test_no_star_resource_except_list_secrets() -> None:
    for policy, statement in all_statements():
        resource = statement["Resource"]
        if resource == "*":
            assert actions_of(statement) == {"secretsmanager:ListSecrets"}, (
                policy,
                statement,
            )
        elif actions_of(statement) & ADMIN_ACTIONS and statement["Effect"] == "Allow":
            assert resource == {"Fn::Sub": PREFIX_ARN}, (policy, statement["Sid"])


def test_kms_statements_exist_only_with_a_key_and_only_via_secrets_manager() -> None:
    for policy in ("RayitoSecretsReader", "RayitoSecretsAdmin"):
        for entry in statements(policy):
            kms = [s for s in resolved(entry) if actions_of(s) & KMS_ACTIONS]
            if not kms:
                continue
            assert "Fn::If" in entry and entry["Fn::If"][0] == "HasKmsKey", policy
            assert entry["Fn::If"][2] == NO_VALUE
            for statement in kms:
                assert actions_of(statement) <= KMS_ACTIONS
                assert statement["Resource"] == {"Ref": "KmsKeyArn"}
                via = statement["Condition"]["StringEquals"]["kms:ViaService"]
                assert via == {"Fn::Sub": "secretsmanager.${AWS::Region}.amazonaws.com"}


def test_the_prefix_parameter_can_never_be_empty() -> None:
    prefix = template()["Parameters"]["SecretPrefix"]
    assert prefix["Default"] == "rayito/"
    assert prefix["MinLength"] == 1
    assert prefix["AllowedPattern"] == "^[A-Za-z0-9/_+=.@-]*/$"
    assert template()["Conditions"]["HasKmsKey"] == {
        "Fn::Not": [{"Fn::Equals": [{"Ref": "KmsKeyArn"}, ""]}]
    }


def test_the_reader_can_never_read_a_webhook_signing_secret() -> None:
    # `secrets=` puts values into an untrusted sandbox: a webhook signing
    # secret there lets the sandbox forge signed deliveries.
    from rayito._secrets import WEBHOOK_SECRET_PREFIX

    reader = [s for p, s in all_statements() if p == "RayitoSecretsReader"]
    (deny,) = [s for s in reader if s.get("Sid") == WEBHOOK_DENY_SID]
    assert deny["Effect"] == "Deny"
    assert actions_of(deny) == {"secretsmanager:GetSecretValue"}
    assert deny["Resource"] == {
        "Fn::Sub": "arn:${AWS::Partition}:secretsmanager:${AWS::Region}:${AWS::AccountId}"
        f":secret:{WEBHOOK_SECRET_PREFIX}*"
    }
    admin = [s for p, s in all_statements() if p == "RayitoSecretsAdmin"]
    assert all(s["Effect"] == "Allow" for s in admin), "admin creates and rotates them"


def test_the_prefix_pattern_requires_a_trailing_slash() -> None:
    import re

    pattern = re.compile(template()["Parameters"]["SecretPrefix"]["AllowedPattern"])
    assert pattern.fullmatch("rayito/")
    assert pattern.fullmatch("team/app/")
    for refused in ("rayito", "rayito-", "", "rayito/*"):
        assert not pattern.fullmatch(refused), refused
