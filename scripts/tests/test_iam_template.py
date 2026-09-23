"""Las tres filas del triaje de 2026-09-22 que viven en `spike/m0/iam.yaml`
(H-01, C-13 y la mitad de código de C-09) y los statements de transferencias
de ADR-010 (sólo con `TransferBucket`, nunca en el execution role), fijados
sobre la plantilla ya parseada y no sobre su texto: `cfn-lint` valida la
forma, no la política, así que sin estos tests un revert de una sola línea
vuelve a abrir el hallazgo sin poner nada en rojo."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = REPO_ROOT / "spike" / "m0" / "iam.yaml"
LIMITS_JSON = REPO_ROOT / "limits.json"
ARTIFACT_NAMESPACE = "rayito"
ARTIFACT_DENY_SID = "NeverTheImageArtifacts"
REFUSED_PREFIX_CHARACTERS = ("*", '"', "'", "(", ")", "!")
TRANSFER_CONDITION = "HasTransferBucket"
TRANSFER_OBJECTS_SID = "TransferObjects"
TRANSFER_LISTING_SID = "TransferMissingKeyIs404"
TRANSFER_OBJECT_ACTIONS = frozenset(
    {"s3:PutObject", "s3:GetObject", "s3:DeleteObject", "s3:AbortMultipartUpload"}
)
TRANSFER_PARAMETERS = ("TransferBucket", "TransferPrefix")
NO_VALUE = {"Ref": "AWS::NoValue"}


class TemplateLoader(yaml.SafeLoader):
    """`SafeLoader` con las etiquetas cortas de CloudFormation (`!Ref`,
    `!Sub`, `!If`…) resueltas a su forma larga, que es la única manera de leer
    la plantilla como datos sin instalar un parser de CloudFormation."""


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


def parameter(name: str) -> dict[str, Any]:
    return template()["Parameters"][name]


def resource(name: str) -> dict[str, Any]:
    return template()["Resources"][name]


def strings_under(node: Any) -> Iterator[str]:
    """Toda cadena del subárbol, incluidas las que cuelgan de un `Fn::Sub` o de
    la rama no tomada de un `Fn::If`: una acción escondida en la rama que hoy
    no se toma sigue concediéndose cuando la condición cambia."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, list):
        for item in node:
            yield from strings_under(item)
    elif isinstance(node, dict):
        for value in node.values():
            yield from strings_under(value)


def actions_under(node: Any) -> Iterator[str]:
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "Action":
                yield from strings_under(value)
            else:
                yield from actions_under(value)
    elif isinstance(node, list):
        for item in node:
            yield from actions_under(item)


def statement_with_sid(node: Any, sid: str) -> dict[str, Any] | None:
    if isinstance(node, dict):
        if node.get("Sid") == sid:
            return node
        for value in node.values():
            found = statement_with_sid(value, sid)
            if found is not None:
                return found
    elif isinstance(node, list):
        for item in node:
            found = statement_with_sid(item, sid)
            if found is not None:
                return found
    return None


def key_spaces_overlap(first: str, second: str) -> bool:
    """Dos prefijos de S3 solapan cuando uno es prefijo del otro por segmentos:
    `rayito` contiene `rayito/images/*`, pero `rayito-home` no."""
    return f"{first}/".startswith(f"{second}/") or f"{second}/".startswith(f"{first}/")


def caller_statements() -> list[Any]:
    return resource("CallerPolicy")["Properties"]["PolicyDocument"]["Statement"]


def statements_guarded_by(condition: str) -> dict[str, dict[str, Any]]:
    """Los statements que sólo existen cuando `condition` es cierta: la rama
    tomada de un `Fn::If` cuya rama contraria es `AWS::NoValue`."""
    guarded: dict[str, dict[str, Any]] = {}
    for statement in caller_statements():
        branches = statement.get("Fn::If") if isinstance(statement, dict) else None
        if not branches or branches[0] != condition:
            continue
        _, taken, otherwise = branches
        assert otherwise == NO_VALUE, (
            f"la rama falsa de `{condition}` debe ser `AWS::NoValue`, no un statement"
        )
        guarded[taken["Sid"]] = taken
    return guarded


def mentions_a_transfer_parameter(node: Any) -> bool:
    return any(
        name in text for text in strings_under(node) for name in TRANSFER_PARAMETERS
    )


def not_equal(first: Any, second: Any) -> dict[str, Any]:
    return {"Fn::Not": [{"Fn::Equals": [first, second]}]}


def test_caller_policy_never_deletes_a_whole_image() -> None:
    actions = set(actions_under(resource("CallerPolicy")))
    assert "lambda:DeleteMicrovmImage" not in actions, (
        "CallerPolicy vuelve a conceder `lambda:DeleteMicrovmImage`, que no usa "
        "ningún camino de código (auditoría C-09)"
    )
    assert "lambda:DeleteMicrovmImageVersion" in actions, (
        "`rayito image prune` necesita `lambda:DeleteMicrovmImageVersion`"
    )


def test_persistence_prefix_default_is_disjoint_from_the_artifact_namespace() -> None:
    default = parameter("PersistencePrefix")["Default"]
    assert not key_spaces_overlap(default, ARTIFACT_NAMESPACE), (
        f"el prefijo de persistencia por defecto «{default}» solapa el espacio "
        f"de los artefactos de imagen «{ARTIFACT_NAMESPACE}/» (auditoría H-01)"
    )


def test_execution_role_is_denied_the_artifact_prefix() -> None:
    deny = statement_with_sid(resource("ExecutionRole"), ARTIFACT_DENY_SID)
    assert deny is not None, (
        f"falta el statement `{ARTIFACT_DENY_SID}` del execution role (auditoría H-01)"
    )
    assert deny["Effect"] == "Deny"
    assert list(strings_under(deny["Action"])) == ["s3:*"]
    assert any(
        f"/{ARTIFACT_NAMESPACE}/" in target
        for target in strings_under(deny["Resource"])
    ), f"el `Deny` no cubre `{ARTIFACT_NAMESPACE}/*` del bucket de artefactos"


def test_persistence_prefix_pattern_refuses_a_wildcard() -> None:
    pattern = re.compile(parameter("PersistencePrefix")["AllowedPattern"])
    assert pattern.fullmatch("rayito-home")
    assert pattern.fullmatch("tenants/acme")
    for character in REFUSED_PREFIX_CHARACTERS:
        candidate = f"home{character}"
        assert not pattern.fullmatch(candidate), (
            f"`AllowedPattern` acepta «{candidate}»: un `*` (o una comilla) en el "
            "prefijo ensancha el recurso IAM que debía acotar (auditoría C-13)"
        )


def test_transfer_prefix_default_is_disjoint_from_artifacts_and_persistence() -> None:
    default = parameter("TransferPrefix")["Default"]
    persistence = parameter("PersistencePrefix")["Default"]
    assert not key_spaces_overlap(default, ARTIFACT_NAMESPACE), (
        f"el prefijo de transferencias por defecto «{default}» solapa los "
        f"artefactos de imagen «{ARTIFACT_NAMESPACE}/»: la regla de ciclo de vida "
        "de un día los borraría"
    )
    assert not key_spaces_overlap(default, persistence), (
        f"el prefijo de transferencias por defecto «{default}» solapa el de "
        f"persistencia «{persistence}»: la regla de ciclo de vida borraría checkpoints"
    )


def test_transfer_prefix_default_matches_the_sdk_default() -> None:
    limits = json.loads(LIMITS_JSON.read_text(encoding="utf-8"))
    assert parameter("TransferPrefix")["Default"] == limits["transferDefaultPrefix"], (
        "el `TransferPrefix` por defecto de la plantilla y el `S3Staging(prefix=)` "
        "por defecto del SDK (`transferDefaultPrefix`) deben coincidir"
    )


def test_transfer_prefix_pattern_is_the_persistence_pattern() -> None:
    transfer = parameter("TransferPrefix")["AllowedPattern"]
    assert transfer == parameter("PersistencePrefix")["AllowedPattern"], (
        "`TransferPrefix` debe usar el mismo `AllowedPattern` que `PersistencePrefix` "
        "(sin comodines ni comillas, auditoría C-13)"
    )
    assert re.fullmatch(transfer, parameter("TransferPrefix")["Default"])


def test_transfer_prefix_rule_refuses_the_artifact_and_persistence_prefixes() -> None:
    rule = template()["Rules"]["TransferPrefixIsDisjoint"]
    assert rule["RuleCondition"] == not_equal({"Ref": "TransferBucket"}, "")
    assertions = [entry["Assert"] for entry in rule["Assertions"]]
    assert not_equal({"Ref": "TransferPrefix"}, ARTIFACT_NAMESPACE) in assertions
    assert (
        not_equal({"Ref": "TransferPrefix"}, {"Ref": "PersistencePrefix"}) in assertions
    )


def test_transfer_statements_are_conditional_on_the_bucket() -> None:
    assert parameter("TransferBucket")["Default"] == ""
    assert template()["Conditions"][TRANSFER_CONDITION] == not_equal(
        {"Ref": "TransferBucket"}, ""
    )
    guarded = statements_guarded_by(TRANSFER_CONDITION)
    assert set(guarded) == {TRANSFER_OBJECTS_SID, TRANSFER_LISTING_SID}

    objects = guarded[TRANSFER_OBJECTS_SID]
    assert objects["Effect"] == "Allow"
    assert set(strings_under(objects["Action"])) == TRANSFER_OBJECT_ACTIONS
    assert list(strings_under(objects["Resource"])) == [
        "arn:aws:s3:::${TransferBucket}/${TransferPrefix}/*"
    ]

    listing = guarded[TRANSFER_LISTING_SID]
    assert listing["Effect"] == "Allow"
    assert list(strings_under(listing["Action"])) == ["s3:ListBucket"]
    assert list(strings_under(listing["Resource"])) == [
        "arn:aws:s3:::${TransferBucket}"
    ]
    assert listing["Condition"] == {
        "StringLike": {"s3:prefix": {"Fn::Sub": "${TransferPrefix}/*"}}
    }

    unguarded = [
        statement
        for statement in caller_statements()
        if statement.get("Fn::If", [None])[0] != TRANSFER_CONDITION
    ]
    assert not any(
        mentions_a_transfer_parameter(statement) for statement in unguarded
    ), (
        "un statement sin la condición `HasTransferBucket` nombra el bucket o el "
        "prefijo de transferencias"
    )


def test_execution_role_has_no_transfer_statement() -> None:
    for role in ("ExecutionRole", "BuildRole"):
        document = resource(role)
        assert not mentions_a_transfer_parameter(document), (
            f"`{role}` nombra el bucket o el prefijo de transferencias: `rayd` no "
            "guarda credenciales para transferir (ADR-010, T1, C-07)"
        )
        assert statement_with_sid(document, TRANSFER_OBJECTS_SID) is None
        assert "s3:DeleteObject" not in set(actions_under(document))
