"""Lector de plantillas de CloudFormation como datos para los tests de
`infra/`: PyYAML con las etiquetas cortas (`!Ref`, `!Sub`, `!If`...) vueltas
a su forma larga. `cfn-lint` (`make infra-lint`) no valida la política, así
que cada test de plantilla la fija leyendo el YAML."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
INFRA = REPO_ROOT / "infra"
SDK_SOURCE = REPO_ROOT / "clients" / "python" / "src"


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


def load_template(name: str) -> dict[str, Any]:
    document = yaml.load(
        (INFRA / f"{name}.yaml").read_text(encoding="utf-8"), Loader=TemplateLoader
    )
    assert isinstance(document, dict)
    return document


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def policy_statements(role: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        item
        for policy in role["Properties"]["Policies"]
        for item in policy["PolicyDocument"]["Statement"]
    ]


def allowed_actions(role: dict[str, Any]) -> set[str]:
    return {
        action
        for item in policy_statements(role)
        if item["Effect"] == "Allow"
        for action in as_list(item["Action"])
    }
