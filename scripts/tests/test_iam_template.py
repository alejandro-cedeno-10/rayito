"""Las tres filas del triaje de 2026-09-22 que viven en `spike/m0/iam.yaml`
(H-01, C-13 y la mitad de código de C-09), fijadas sobre la plantilla ya
parseada y no sobre su texto: `cfn-lint` valida la forma, no la política, así
que sin estos tests un revert de una sola línea vuelve a abrir el hallazgo sin
poner nada en rojo."""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = REPO_ROOT / "spike" / "m0" / "iam.yaml"
ARTIFACT_NAMESPACE = "rayito"
ARTIFACT_DENY_SID = "NeverTheImageArtifacts"
REFUSED_PREFIX_CHARACTERS = ("*", '"', "'", "(", ")", "!")


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
