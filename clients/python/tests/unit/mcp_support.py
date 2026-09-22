"""Apoyo compartido por los tests del servidor MCP: los stubs del plano de
control que necesita `SandboxLease` y la fábrica del servidor sobre el `rayd`
falso. El `rayd` falso verifica `x-access-token` contra `ACCESS_TOKEN`, y el
servidor no acepta un token explícito, así que los tests lo inyectan por
`RAYITO_ACCESS_TOKEN`, la variable que `create()` ya lee."""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator
from typing import Any

from mcp import Client
from mcp.server import MCPServer
from mcp.types import CallToolResult, TextContent

from rayito._limits import DEFAULT_PORT
from rayito.mcp import McpSettings, SandboxLease, build_server

from .conftest import (
    IMAGE_ARN,
    SANDBOX_ID,
    RaydEndpoint,
    StubbedControlPlane,
    auth_token_response,
    microvm_response,
)


def stub_launch(control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint) -> None:
    control_plane.microvms.add_response("run_microvm", microvm_response(endpoint=fake_rayd.host))
    control_plane.microvms.add_response(
        "create_microvm_auth_token",
        auth_token_response(),
        expected_params={
            "microvmIdentifier": SANDBOX_ID,
            "expirationInMinutes": 60,
            "allowedPorts": [{"port": DEFAULT_PORT}],
        },
    )


def stub_terminate(control_plane: StubbedControlPlane) -> None:
    control_plane.microvms.add_response(
        "terminate_microvm", {}, expected_params={"microvmIdentifier": SANDBOX_ID}
    )


def mcp_server(
    control_plane: StubbedControlPlane,
    fake_rayd: RaydEndpoint,
    settings: McpSettings | None = None,
) -> MCPServer[SandboxLease]:
    return build_server(
        settings or McpSettings(template=IMAGE_ARN, idle_seconds=0),
        control_plane=control_plane.plane,
        transport=fake_rayd.transport,
    )


@contextlib.asynccontextmanager
async def launched_client(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> AsyncIterator[Client]:
    """Un cliente en memoria cuyo servidor crea el sandbox en la primera
    herramienta que lo necesita (stub encolado) y lo termina al salir del
    contexto (stub encolado antes de salir; el Stubber es FIFO). Es un
    context manager y no una fixture porque el `Client` de `mcp` debe entrar
    y salir en la misma tarea de asyncio."""
    stub_launch(control_plane, fake_rayd)
    async with Client(mcp_server(control_plane, fake_rayd), raise_exceptions=True) as client:
        yield client
        stub_terminate(control_plane)


def first_json(result: CallToolResult) -> dict[str, Any]:
    first = result.content[0]
    assert isinstance(first, TextContent)
    parsed: dict[str, Any] = json.loads(first.text)
    return parsed


def error_text(result: CallToolResult) -> str:
    assert result.is_error is True
    first = result.content[0]
    assert isinstance(first, TextContent)
    return first.text
