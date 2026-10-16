"""Las herramientas del servidor MCP (nombre, descripción, anotaciones,
argumentos y campos de salida) frente al golden `testdata/mcp/tools.json`.

El golden es el contrato que el servidor MCP de TypeScript reproduce (ADR-027):
un test de vitest compara sus herramientas con este mismo fichero. Si cambias
una herramienta, regenéralo con
`RAYITO_UPDATE_MCP_TOOLS=1 uv run pytest tests/unit/test_mcp_contract.py` y
revisa el diff: es la superficie pública del servidor.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Final

from mcp import Client
from mcp.types import Tool

from rayito.mcp import McpSettings, build_server

REPO_ROOT_DEPTH: Final = 4
TESTDATA: Final = Path(__file__).resolve().parents[REPO_ROOT_DEPTH] / "testdata" / "mcp"
TOOLS_GOLDEN: Final = TESTDATA / "tools.json"
UPDATE_ENV: Final = "RAYITO_UPDATE_MCP_TOOLS"
JSON_INDENT: Final = 2
CONTEXT_PARAMETER: Final = "ctx"
NULL_TYPE: Final = "null"
CONSTRAINTS: Final = ("minimum", "maximum")


def property_type(schema: dict[str, Any]) -> str:
    if "type" in schema:
        return str(schema["type"])
    return next(
        str(option["type"]) for option in schema["anyOf"] if option.get("type") != NULL_TYPE
    )


def describe_input(tool: Tool) -> list[dict[str, Any]]:
    schema = tool.input_schema
    required = set(schema.get("required", []))
    inputs: list[dict[str, Any]] = []
    for name, spec in schema["properties"].items():
        if name == CONTEXT_PARAMETER:
            continue
        entry: dict[str, Any] = {
            "name": name,
            "type": property_type(spec),
            "required": name in required,
            "description": spec["description"],
        }
        if spec.get("default") is not None:
            entry["default"] = spec["default"]
        for constraint in CONSTRAINTS:
            if constraint in spec:
                entry[constraint] = spec[constraint]
        inputs.append(entry)
    return inputs


def describe_tool(tool: Tool) -> dict[str, Any]:
    annotations = (
        {}
        if tool.annotations is None
        else tool.annotations.model_dump(by_alias=True, exclude_none=True)
    )
    output = None if tool.output_schema is None else sorted(tool.output_schema["properties"])
    return {
        "name": tool.name,
        "description": " ".join((tool.description or "").split()),
        "annotations": annotations,
        "input": describe_input(tool),
        "output": output,
    }


async def describe_server() -> dict[str, Any]:
    async with Client(build_server(McpSettings()), raise_exceptions=True) as client:
        tools = (await client.list_tools()).tools
        assert client.server_info is not None
        instructions = " ".join((client.instructions or "").split())
        return {
            "server": {"name": client.server_info.name, "instructions": instructions},
            "tools": [describe_tool(tool) for tool in tools],
        }


async def test_tools_match_the_shared_golden() -> None:
    described = await describe_server()
    if os.environ.get(UPDATE_ENV) == "1":
        TOOLS_GOLDEN.write_text(
            json.dumps(described, indent=JSON_INDENT, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    golden = json.loads(TOOLS_GOLDEN.read_text(encoding="utf-8"))
    assert described == golden
