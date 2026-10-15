"""La superficie de la CLI (rutas, opciones, tipos, valores por defecto y
obligatoriedad) frente al golden `testdata/cli/surface.json`.

El golden es el contrato que la CLI de TypeScript tiene que reproducir
(ADR-027): el test de vitest compara su árbol de comandos con este mismo
fichero y `scripts/check_sdk_parity.py --cli` informa de qué falta. Si cambias
una opción, regenéralo con
`RAYITO_UPDATE_CLI_SURFACE=1 uv run pytest tests/unit/cli/test_cli_surface.py`
y revisa el diff: es la superficie pública.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Final

import typer.core
import typer.main

from rayito.cli.app import app

GOLDEN: Final = Path(__file__).resolve().parents[5] / "testdata" / "cli" / "surface.json"
UPDATE_ENV: Final = "RAYITO_UPDATE_CLI_SURFACE"
JSON_INDENT: Final = 2


def describe_default(value: object) -> object:
    """El valor por defecto en forma serializable y estable entre ejecuciones."""
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, list | tuple):
        return [describe_default(item) for item in value]
    return str(value)


def describe_parameter(parameter: Any) -> dict[str, Any]:
    description: dict[str, Any] = {
        "name": parameter.name,
        "kind": "option" if isinstance(parameter, typer.core.TyperOption) else "argument",
        "flags": [*parameter.opts, *parameter.secondary_opts],
        "type": parameter.type.name,
        "required": parameter.required,
        "multiple": parameter.multiple,
        "default": describe_default(parameter.default),
    }
    if isinstance(parameter, typer.core.TyperOption):
        description["is_flag"] = parameter.is_flag
        description["envvar"] = parameter.envvar
    choices = getattr(parameter.type, "choices", None)
    if choices is not None:
        description["choices"] = list(choices)
    return description


def describe_command(command: Any, path: tuple[str, ...]) -> list[dict[str, Any]]:
    """Una entrada por comando hoja o grupo, en orden alfabético de ruta."""
    entry: dict[str, Any] = {
        "path": list(path),
        "group": isinstance(command, typer.core.TyperGroup),
        "hidden": command.hidden,
        "parameters": [
            describe_parameter(parameter)
            for parameter in command.params
            if parameter.name != "help"
        ],
    }
    entries = [entry]
    if isinstance(command, typer.core.TyperGroup):
        for name in sorted(command.commands):
            entries += describe_command(command.commands[name], (*path, name))
    return entries


def current_surface() -> dict[str, Any]:
    root = typer.main.get_command(app)
    return {"commands": describe_command(root, ())}


def serialise(surface: dict[str, Any]) -> str:
    return json.dumps(surface, indent=JSON_INDENT, ensure_ascii=False) + "\n"


def test_cli_surface_matches_the_golden() -> None:
    rendered = serialise(current_surface())
    if os.environ.get(UPDATE_ENV):
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(rendered, encoding="utf-8")
    assert GOLDEN.read_text(encoding="utf-8") == rendered, (
        f"la superficie de la CLI cambió: regenera el golden con {UPDATE_ENV}=1"
    )


def test_golden_lists_every_documented_group() -> None:
    paths = {tuple(entry["path"]) for entry in json.loads(GOLDEN.read_text("utf-8"))["commands"]}
    for group in ("image", "sandbox", "stack", "events", "template", "domain", "agent"):
        assert (group,) in paths
    assert ("doctor",) in paths
