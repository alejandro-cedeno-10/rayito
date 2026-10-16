"""La CLI `rayito` de Python y el servidor MCP contra el guest local y Floci
(`make local-e2e`): los mismos escenarios que corren en el carril de
TypeScript (`clients/typescript/tests/shared/cli-scenarios.ts`) y que corren
contra AWS real (`tests/e2e/test_m7_cli.py`, `test_m7_mcp.py`), de modo que un
cambio en una CLI que rompa lo que la otra hace se vea sin gastar en AWS.

La CLI corre en proceso (`CliRunner`) con el plano de control y el transporte
del guest inyectados en `Clients`. El servidor MCP corre por stdio como
subproceso (`python -m rayito.mcp`), con el cliente oficial del SDK de MCP; ese
subproceso no sabe hablar con el guest local, así que aquí sólo se comprueba el
protocolo y el contrato compartido `testdata/mcp/tools.json`. Las herramientas
contra un sandbox real las cubre el e2e contra AWS real.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Final

import boto3
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from typer.testing import CliRunner

from rayito._sizing import SIZE_NAMES
from rayito.cli._session import Clients
from rayito.cli.app import app

from ..unit.test_mcp_contract import TOOLS_GOLDEN, describe_tool
from .conftest import LocalSettings
from .guest import LocalGuestControlPlane

pytestmark = pytest.mark.local

EXEC_FAILURE_CODE: Final = 7
EXEC_TIMEOUT_CODE: Final = 124
EXEC_TIMEOUT_SECONDS: Final = 2
SLEEP_SECONDS: Final = 20
SESSION_TIMEOUT_SECONDS: Final = 900
RUN_LABEL: Final = "rayito-pycli-local"
LOCAL_IMAGE_NAME: Final = "rayito-local"
MCP_TIMEOUT_SECONDS: Final = "900"


def invoke(clients: Clients, *args: str) -> tuple[int, str, str]:
    result = CliRunner().invoke(app, list(args), obj=clients)
    return result.exit_code, result.stdout, result.stderr


def invoke_json(clients: Clients, *args: str) -> tuple[int, Any]:
    code, stdout, _ = invoke(clients, "--json", *args)
    return code, json.loads(stdout) if stdout.strip() else None


@pytest.fixture
def clients(
    local_settings: LocalSettings,
    aws_session: boto3.session.Session,
    control_plane: LocalGuestControlPlane,
    template_arn: str,
) -> Clients:
    return Clients(
        session=aws_session,
        region=local_settings.region,
        control_plane_override=control_plane,
        transport=local_settings.transport,
    )


def test_stack_list_and_status_create_nothing(clients: Clients) -> None:
    code, listed = invoke_json(clients, "stack", "list")
    assert code == 0
    assert "metadata-index" in [row["name"] for row in listed]
    code, status = invoke_json(clients, "stack", "status", "metadata-index")
    assert code == 0
    assert status is None or isinstance(status["state"], str)


def test_image_list_and_sizes_read_the_catalogue(clients: Clients) -> None:
    code, images = invoke_json(clients, "image", "list")
    assert code == 0
    assert LOCAL_IMAGE_NAME in [image["name"] for image in images]
    code, sizes = invoke_json(clients, "image", "sizes")
    assert code == 0
    assert len(sizes) == len(SIZE_NAMES)


def test_a_sandbox_end_to_end_from_the_cli(
    clients: Clients, template_arn: str, tmp_path: Path
) -> None:
    token_file = tmp_path / "token"
    code, launch = invoke_json(
        clients,
        "sandbox",
        "create",
        template_arn,
        "--detach",
        "--token-file",
        str(token_file),
        "--timeout",
        str(SESSION_TIMEOUT_SECONDS),
        "--metadata",
        f"run={RUN_LABEL}",
    )
    assert code == 0
    sandbox_id = launch["sandbox_id"]
    assert sandbox_id.startswith("microvm-")

    code, info = invoke_json(clients, "sandbox", "info", sandbox_id)
    assert code == 0
    assert info["state"] == "RUNNING"
    assert info["metadata"] == {"run": RUN_LABEL}

    def run(*rest: str) -> tuple[int, str, str]:
        return invoke(
            clients, "sandbox", "exec", sandbox_id, "--token-file", str(token_file), *rest
        )

    assert run("--", "echo", "hola mundo")[:2] == (0, "hola mundo\n")
    assert run("--", "sh", "-c", f"exit {EXEC_FAILURE_CODE}")[0] == EXEC_FAILURE_CODE
    timed_out = run("--timeout", str(EXEC_TIMEOUT_SECONDS), "--", "sleep", str(SLEEP_SECONDS))
    assert timed_out[0] == EXEC_TIMEOUT_CODE

    code, metrics = invoke_json(
        clients, "sandbox", "metrics", sandbox_id, "--token-file", str(token_file)
    )
    assert code == 0
    assert metrics["cpu_count"] > 0

    code, listed = invoke_json(clients, "sandbox", "list", "--metadata", f"run={RUN_LABEL}")
    assert code == 0
    assert [row["sandbox_id"] for row in listed] == [sandbox_id]

    code, killed = invoke_json(clients, "sandbox", "kill", sandbox_id)
    assert code == 0
    assert killed == [{"sandbox_id": sandbox_id, "outcome": "terminated"}]


@pytest.fixture
def mcp_parameters() -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "rayito.mcp"],
        env={
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "RAYITO_MCP_TIMEOUT_SECONDS": MCP_TIMEOUT_SECONDS,
            "RAYITO_MCP_IDLE_SECONDS": "0",
        },
    )


async def test_mcp_stdio_announces_the_shared_contract(
    mcp_parameters: StdioServerParameters,
) -> None:
    golden = json.loads(TOOLS_GOLDEN.read_text(encoding="utf-8"))
    async with stdio_client(mcp_parameters) as (read, write), ClientSession(read, write) as session:
        initialized = await session.initialize()
        tools = (await session.list_tools()).tools
    assert initialized.server_info.name == golden["server"]["name"]
    assert " ".join((initialized.instructions or "").split()) == golden["server"]["instructions"]
    assert [describe_tool(tool) for tool in tools] == golden["tools"]


async def test_mcp_stdio_tool_without_a_template_fails_saying_so(
    mcp_parameters: StdioServerParameters,
) -> None:
    async with stdio_client(mcp_parameters) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        result = await session.call_tool("run_command", {"cmd": "echo hola"})
    assert result.is_error
    assert "RAYITO_TEMPLATE" in json.dumps([block.model_dump() for block in result.content])
