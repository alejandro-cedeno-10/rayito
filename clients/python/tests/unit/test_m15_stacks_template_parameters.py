"""Cada `StackComponent` soportado declara exactamente los `Parameters` de
su plantilla empaquetada (M15, revisión del PR #77): `_resolved_parameters`
rechaza cualquier nombre que el componente no liste, así que un parámetro de
la plantilla que falte aquí (como pasó con `AllowWrite` de `efs-volumes`)
queda inalcanzable desde `deploy(parameters=...)` y `--param`."""

from __future__ import annotations

import re

import pytest

from rayito._stacks._model import StackComponent
from rayito._stacks._packaging import load_template
from rayito._stacks._registry import COMPONENTS

#: Una clave de primer nivel (`Parameters:`, `Resources:`...) empieza en la
#: columna 0; las de un parámetro, en la columna 2 (`infra/*.yaml` sigue el
#: sangrado de dos espacios de `cfn-lint`).
TOP_LEVEL_KEY = re.compile(r"^(\w+):", re.MULTILINE)
PARAMETER_KEY = re.compile(r"^  (\w+):\s*$", re.MULTILINE)


def template_parameter_names(template: str) -> set[str]:
    sections = TOP_LEVEL_KEY.split(template)
    # `split` alterna [antes, clave, cuerpo, clave, cuerpo, ...].
    bodies = dict(zip(sections[1::2], sections[2::2], strict=True))
    return set(PARAMETER_KEY.findall(bodies.get("Parameters", "")))


def component_parameter_names(component: StackComponent) -> set[str]:
    declared = {parameter.name for parameter in component.parameters}
    return declared | {artifact.parameter_key for artifact in component.artifacts}


SUPPORTED = [component for component in COMPONENTS if component.supported]


@pytest.mark.parametrize("component", SUPPORTED, ids=lambda component: component.name)
def test_component_parameters_match_the_template_parameters(component: StackComponent) -> None:
    template = load_template(component)
    assert component_parameter_names(component) == template_parameter_names(template)
