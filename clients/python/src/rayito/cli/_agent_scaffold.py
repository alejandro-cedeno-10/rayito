"""Andamiaje de `rayito agent init`: los ficheros que escribe para cada
runtime. Pura (devuelve un mapa ruta -> contenido); `write_scaffold` es la
única que toca el disco. Los casos esperados viven en
`testdata/cli/agent-scaffold/` y los lee también la CLI de TypeScript, para
que las dos escriban exactamente lo mismo."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

SCHEMA: Final = "rayito.agent-project/1"
PROJECT_FILE_NAME: Final = "rayito-agent.toml"
INSTRUCTIONS_FILE_NAME: Final = "AGENTS.md"
REQUIREMENTS_FILE_NAME: Final = "requirements.txt"
DEFAULT_PROJECT_NAME: Final = "my-agent"
MAX_PROJECT_NAME_LENGTH: Final = 63
SCAFFOLD_FALLBACK_REGION: Final = "us-east-1"
PYTHON_LANGUAGE: Final = "python"
TYPESCRIPT_LANGUAGE: Final = "typescript"
LANGUAGES: Final = (PYTHON_LANGUAGE, TYPESCRIPT_LANGUAGE)
SCAFFOLD_RUNTIMES: Final = (
    "opencode",
    "deepagents",
    "langgraph",
    "strands",
    "strands-harness",
    "hermes",
    "custom",
)
#: Proveedor -> (preset de la pasarela, modelo por defecto o `None` si el
#: usuario tiene que elegirlo). Sólo los presets cuya pasarela no pide más
#: datos que el secreto; el resto se escribe a mano (guía de proveedores).
PROVIDER_PRESETS: Final[Mapping[str, tuple[str, str | None]]] = {
    "bedrock": ("bedrock", "us.anthropic.claude-haiku-4-5-20251001-v1:0"),
    "anthropic": ("anthropic", "claude-haiku-4-5"),
    "openai": ("openai", None),
}
DEFAULT_PROVIDER: Final = "bedrock"
SECRET_NAME: Final = "provider-key"
GATEWAY_NAME: Final = "model"
ENTRYPOINT_RUNTIMES: Final = frozenset({"langgraph", "strands"})
PYTHON_ENTRYPOINTS: Final[Mapping[str, str]] = {
    "langgraph": "graph:build",
    "strands": "agent:agent",
}
TYPESCRIPT_ENTRYPOINT: Final = "agent.ts"
EXCLUDES: Final = (".venv", "node_modules", "__pycache__", ".git")
INSTRUCTIONS_TEXT: Final = "Responde en español, con frases cortas.\n"
CUSTOM_COMMAND: Final = ("bash", "run.sh")

STRANDS_PY: Final = '''"""Agente de Strands. `ctx.model` ya apunta a la pasarela del modelo."""

from strands import Agent, tool


@tool
def word_count(text: str) -> int:
    """Cuenta las palabras de un texto."""
    return len(text.split())


def agent(ctx):
    return Agent(model=ctx.model, tools=[word_count])
'''

STRANDS_TS: Final = """import { Agent, type Model, tool } from "@strands-agents/sdk";
import { z } from "zod";

const wordCount = tool({
  name: "word_count",
  description: "Cuenta las palabras de un texto.",
  inputSchema: z.object({ text: z.string() }),
  callback: ({ text }) => text.split(/\\s+/).filter(Boolean).length,
});

export function agent(ctx: { model: Model }): Agent {
  return new Agent({ model: ctx.model, tools: [wordCount] });
}
"""

STRANDS_PACKAGE_JSON: Final = """{
  "name": "agent",
  "private": true,
  "type": "module",
  "dependencies": {
    "@strands-agents/sdk": "1.21.0",
    "zod": "^4.5.4"
  }
}
"""

LANGGRAPH_PY: Final = '''"""Grafo del agente. `ctx` trae el modelo ya cableado a la pasarela."""

from deepagents import create_deep_agent


def build(ctx):
    return create_deep_agent(
        model=ctx.model,
        system_prompt=ctx.instructions,
        subagents=ctx.subagents,
        backend=ctx.backend,
        middleware=ctx.middleware,
    )
'''

CUSTOM_RUN_SH: Final = """#!/usr/bin/env bash
set -euo pipefail
python3 -c 'import json, sys; request = json.loads(sys.stdin.readline()); print(request["prompt"])'
"""

STRANDS_REQUIREMENTS: Final = "strands-agents\n"


class ScaffoldConflictError(Exception):
    """`init` encontró ficheros que ya existen y no se pasó `--force`.
    `paths` son rutas relativas al directorio del proyecto."""

    def __init__(self, paths: list[str]) -> None:
        super().__init__(", ".join(paths))
        self.paths = paths


@dataclass(frozen=True)
class ScaffoldRequest:
    name: str
    runtime: str
    language: str
    provider: str
    model_id: str
    region: str


def project_name_of(directory: Path) -> str:
    """El nombre del proyecto: el del directorio en minúsculas, con `-` en
    lugar de cualquier otro carácter, y `my-agent` si no queda nada."""
    slug = re.sub(r"[^a-z0-9]+", "-", directory.resolve().name.lower()).strip("-")
    return slug[:MAX_PROJECT_NAME_LENGTH].strip("-") or DEFAULT_PROJECT_NAME


def _quoted(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _runtime_table(request: ScaffoldRequest) -> list[str]:
    lines = ["[runtime]", f"kind = {_quoted(request.runtime)}"]
    if request.runtime in ENTRYPOINT_RUNTIMES:
        entrypoint = (
            TYPESCRIPT_ENTRYPOINT
            if request.language == TYPESCRIPT_LANGUAGE
            else PYTHON_ENTRYPOINTS[request.runtime]
        )
        lines.append(f"entrypoint = {_quoted(entrypoint)}")
    if request.runtime == "strands" and request.language == TYPESCRIPT_LANGUAGE:
        lines.append(f"language = {_quoted(TYPESCRIPT_LANGUAGE)}")
    if request.runtime == "custom":
        command = ", ".join(_quoted(part) for part in CUSTOM_COMMAND)
        lines += [f"command = [{command}]", 'protocol = "text"']
    return lines


def _source_table(request: ScaffoldRequest) -> list[str]:
    lines = ["[source]", 'path = "."']
    if request.runtime == "strands" and request.language == PYTHON_LANGUAGE:
        lines.append(f"requirements = {_quoted(REQUIREMENTS_FILE_NAME)}")
    excludes = ", ".join(_quoted(item) for item in EXCLUDES)
    lines.append(f"exclude = [{excludes}]")
    return lines


def _model_tables(request: ScaffoldRequest) -> list[str]:
    preset, _ = PROVIDER_PRESETS[request.provider]
    model = [
        "[model]",
        f"provider = {_quoted(request.provider)}",
        f"id = {_quoted(request.model_id)}",
        f"gateway = {_quoted(GATEWAY_NAME)}",
    ]
    gateway = [
        f"[gateways.{GATEWAY_NAME}]",
        f"preset = {_quoted(preset)}",
        f"secret = {_quoted(SECRET_NAME)}",
    ]
    if request.provider == "bedrock":
        model.insert(3, f"region = {_quoted(request.region)}")
        gateway += [
            f"region = {_quoted(request.region)}",
            f"models = [{_quoted(request.model_id)}]",
        ]
    return [*model, "", *gateway]


def project_file(request: ScaffoldRequest) -> str:
    sections = [
        [f"schema = {_quoted(SCHEMA)}", f"name = {_quoted(request.name)}"],
        _runtime_table(request),
        _source_table(request),
        _model_tables(request),
        ["[agent]", f"instructions_file = {_quoted(INSTRUCTIONS_FILE_NAME)}"],
    ]
    return "\n\n".join("\n".join(section) for section in sections) + "\n"


def scaffold_files(request: ScaffoldRequest) -> dict[str, str]:
    """Todos los ficheros de `rayito agent init`, por ruta relativa."""
    files = {
        PROJECT_FILE_NAME: project_file(request),
        INSTRUCTIONS_FILE_NAME: INSTRUCTIONS_TEXT,
    }
    if request.runtime == "strands" and request.language == TYPESCRIPT_LANGUAGE:
        files[TYPESCRIPT_ENTRYPOINT] = STRANDS_TS
        files["package.json"] = STRANDS_PACKAGE_JSON
    elif request.runtime == "strands":
        files["agent.py"] = STRANDS_PY
        files[REQUIREMENTS_FILE_NAME] = STRANDS_REQUIREMENTS
    elif request.runtime == "langgraph":
        files["graph.py"] = LANGGRAPH_PY
    elif request.runtime == "custom":
        files["run.sh"] = CUSTOM_RUN_SH
    return files


def write_scaffold(directory: Path, files: Mapping[str, str], *, force: bool) -> list[str]:
    """Escribe `files` bajo `directory` y devuelve las rutas escritas. Sin
    `force`, si alguna existe no escribe nada y lanza `ScaffoldConflictError`."""
    existing = sorted(name for name in files if (directory / name).exists())
    if existing and not force:
        raise ScaffoldConflictError(existing)
    directory.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (directory / name).write_text(content, encoding="utf-8")
    return list(files)
