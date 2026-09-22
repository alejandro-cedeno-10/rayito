"""Aceptación de M7 `m7-mcp-server`: `python -m rayito.mcp` como subproceso
stdio manejado por el `Client` del SDK `mcp` (lo que hace el MCP Inspector,
guionizado) contra un MicroVM real. Comprueba las seis herramientas, la
creación perezosa en la primera llamada, el PNG de matplotlib como bloque de
imagen, un error del kernel como dato, `list_sandboxes` con `current` y, al
cerrar el cliente (EOF en stdin), que el MicroVM queda TERMINATING/TERMINATED.
Mismo guardrail que todo e2e: `RAYITO_MCP_TIMEOUT_SECONDS=900`.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
from typing import Any

import pytest
from mcp import Client, StdioServerParameters
from mcp.types import CallToolResult, ImageContent, TextContent

from rayito._aws import LambdaMicrovmsControlPlane
from rayito._limits import TERMINAL_STATES
from rayito.exceptions import SandboxNotFoundException

from .conftest import E2ESettings

pytestmark = pytest.mark.e2e

TOOL_NAMES = ["run_code", "run_command", "read_file", "write_file", "list_files", "list_sandboxes"]
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PLOT_CODE = "import matplotlib.pyplot as plt; plt.plot([1, 2, 3]); plt.show()"
TEST_FILE = "/home/user/mcp.txt"
TEST_CONTENT = "hola mcp"
SERVER_TIMEOUT_SECONDS = "900"
FIRST_CALL_TIMEOUT_SECONDS = 240.0
CALL_TIMEOUT_SECONDS = 120.0
TERMINATE_VISIBLE_TIMEOUT_SECONDS = 30.0
TERMINATE_POLL_INTERVAL_SECONDS = 0.5


def server_environment() -> dict[str, str]:
    """Todo `AWS_*` y `RAYITO_*` del proceso de test (credenciales, perfil,
    región, template, rol) más los límites del guardrail; el `Client` del SDK
    los añade a su allow-list de variables heredadas."""
    inherited = {
        key: value for key, value in os.environ.items() if key.startswith(("AWS_", "RAYITO_"))
    }
    return {
        **inherited,
        "RAYITO_MCP_TIMEOUT_SECONDS": SERVER_TIMEOUT_SECONDS,
        "RAYITO_MCP_IDLE_SECONDS": "0",
        "RAYITO_MCP_LOG_LEVEL": "DEBUG",
    }


def first_json(result: CallToolResult) -> dict[str, Any]:
    first = result.content[0]
    assert isinstance(first, TextContent)
    parsed: dict[str, Any] = json.loads(first.text)
    return parsed


def structured(result: CallToolResult) -> dict[str, Any]:
    assert result.is_error is False, result.content
    assert result.structured_content is not None
    content: dict[str, Any] = result.structured_content
    return content


def wait_for_terminal_state(
    sandbox_id: str, control_plane: LambdaMicrovmsControlPlane
) -> tuple[str, float]:
    """`get-microvm` es eventualmente consistente: tras el `terminate-microvm`
    del lifespan puede responder `RUNNING` un momento; un 404 cuenta como
    terminado."""
    started = time.perf_counter()
    while True:
        try:
            state = control_plane.get_microvm(sandbox_id).state
        except SandboxNotFoundException:
            state = "TERMINATED"
        elapsed = time.perf_counter() - started
        if state in TERMINAL_STATES or elapsed >= TERMINATE_VISIBLE_TIMEOUT_SECONDS:
            return state, elapsed
        time.sleep(TERMINATE_POLL_INTERVAL_SECONDS)


async def test_mcp_server_over_stdio_against_aws(
    e2e_settings: E2ESettings,
    control_plane: LambdaMicrovmsControlPlane,
    template_arn: str,
) -> None:
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "rayito.mcp"], env=server_environment()
    )
    total_started = time.perf_counter()
    async with Client(params, raise_exceptions=True) as client:
        tools = await client.list_tools()
        assert [tool.name for tool in tools.tools] == TOOL_NAMES

        started = time.perf_counter()
        echoed = await client.call_tool(
            "run_command", {"cmd": "echo hola"}, read_timeout_seconds=FIRST_CALL_TIMEOUT_SECONDS
        )
        first_call = time.perf_counter() - started
        print(f"\nmcp first call (create) {first_call:.2f} s", flush=True)
        assert structured(echoed)["stdout"] == "hola\n"
        assert structured(echoed)["exit_code"] == 0

        written = await client.call_tool(
            "write_file",
            {"path": TEST_FILE, "content": TEST_CONTENT},
            read_timeout_seconds=CALL_TIMEOUT_SECONDS,
        )
        assert structured(written)["size"] == len(TEST_CONTENT)
        read = await client.call_tool(
            "read_file", {"path": TEST_FILE}, read_timeout_seconds=CALL_TIMEOUT_SECONDS
        )
        assert structured(read)["content"] == TEST_CONTENT
        listed = await client.call_tool(
            "list_files", {"path": "/home/user"}, read_timeout_seconds=CALL_TIMEOUT_SECONDS
        )
        assert "mcp.txt" in [entry["name"] for entry in structured(listed)["entries"]]

        plotted = await client.call_tool(
            "run_code", {"code": PLOT_CODE}, read_timeout_seconds=CALL_TIMEOUT_SECONDS
        )
        assert plotted.is_error is False, plotted.content
        images = [block for block in plotted.content if isinstance(block, ImageContent)]
        assert images and images[0].mime_type == "image/png"
        assert base64.b64decode(images[0].data)[: len(PNG_SIGNATURE)] == PNG_SIGNATURE
        assert "image/png" in first_json(plotted)["results"][0]["mime_types"]
        divided = await client.call_tool(
            "run_code", {"code": "1/0"}, read_timeout_seconds=CALL_TIMEOUT_SECONDS
        )
        assert divided.is_error is False
        assert first_json(divided)["error"]["name"] == "ZeroDivisionError"

        sandboxes = await client.call_tool(
            "list_sandboxes", read_timeout_seconds=CALL_TIMEOUT_SECONDS
        )
        current = [item for item in structured(sandboxes)["sandboxes"] if item["current"]]
        assert len(current) == 1
        sandbox_id = str(current[0]["sandbox_id"])
        assert current[0]["template"] == template_arn

    state, elapsed = wait_for_terminal_state(sandbox_id, control_plane)
    print(
        f"{sandbox_id}: al cerrar el cliente -> {state} a los {elapsed:.2f} s; "
        f"total {time.perf_counter() - total_started:.2f} s",
        flush=True,
    )
    assert state in TERMINAL_STATES
