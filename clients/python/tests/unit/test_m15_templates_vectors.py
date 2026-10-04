"""Vectores compartidos con el SDK TypeScript
(`testdata/templates/dockerfile-cases.json`): mismas instrucciones, mismas
líneas Dockerfile; mismos helpers, mismo `ready_cmd`. Si este test y
`m15-templates-vectors.test.ts` pasan, ambos SDKs hornean lo mismo."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from rayito._templates import _ready_cmds
from rayito._templates._dockerfile import (
    LAYER_BEGIN_MARKER,
    LAYER_END_MARKER,
    render_appended_layer,
)
from rayito._templates._instructions import (
    CopyStep,
    EnvStep,
    RunStep,
    TemplateSpec,
    UserStep,
    WireStep,
    WorkdirStep,
)
from rayito.exceptions import InvalidArgumentException

VECTORS = json.loads(
    (Path(__file__).parents[4] / "testdata" / "templates" / "dockerfile-cases.json").read_text()
)
_STEP_TYPES: dict[str, Any] = {
    "copy": CopyStep,
    "env": EnvStep,
    "run": RunStep,
    "workdir": WorkdirStep,
    "user": UserStep,
}


def _steps(raw: list[dict[str, Any]]) -> tuple[WireStep, ...]:
    return tuple(
        _STEP_TYPES[item["kind"]](**{k: v for k, v in item.items() if k != "kind"}) for item in raw
    )


@pytest.mark.parametrize("case", VECTORS["layer_cases"], ids=lambda case: case["name"])
def test_layer_cases_render_the_shared_lines(case: dict[str, Any]) -> None:
    layer = render_appended_layer(TemplateSpec(steps=_steps(case["steps"])))
    lines = layer.splitlines()
    assert lines[0] == LAYER_BEGIN_MARKER
    assert lines[-1] == LAYER_END_MARKER
    assert lines[1:-1] == case["lines"]


@pytest.mark.parametrize("case", VECTORS["rejected_steps"], ids=lambda case: case["name"])
def test_rejected_steps_raise_invalid_argument(case: dict[str, Any]) -> None:
    with pytest.raises(InvalidArgumentException):
        render_appended_layer(TemplateSpec(steps=_steps(case["steps"])))


@pytest.mark.parametrize(
    "case", VECTORS["ready_cases"], ids=lambda case: f"{case['helper']}{case['args']}"
)
def test_ready_helpers_compile_to_the_shared_command(case: dict[str, Any]) -> None:
    helper = getattr(_ready_cmds, case["helper"])
    first, *rest = case["args"]
    # `wait_for_url(url, *, status=...)`: el segundo argumento del vector
    # es el `status` posicional de `waitForUrl(url, status)` en TypeScript.
    kwargs = {"status": rest[0]} if rest else {}
    assert helper(first, **kwargs).cmd == case["cmd"]
