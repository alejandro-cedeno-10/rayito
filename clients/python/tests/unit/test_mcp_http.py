"""Streamable HTTP de verdad: `streamable_http_app()` servida por uvicorn en
un puerto libre de loopback, el `Client` del SDK `mcp` por URL, y el
`terminate_microvm` del lifespan al parar uvicorn (design D9, D12)."""

from __future__ import annotations

import asyncio
import socket

import pytest
import uvicorn
from mcp import Client

from rayito._sandbox_base import ACCESS_TOKEN_ENV_VAR

from .conftest import ACCESS_TOKEN, RaydEndpoint, StubbedControlPlane
from .mcp_support import mcp_server, stub_launch, stub_terminate

STEP_TIMEOUT_SECONDS = 30
POLL_SECONDS = 0.05


@pytest.fixture(autouse=True)
def access_token_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ACCESS_TOKEN_ENV_VAR, ACCESS_TOKEN)


def free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    assert isinstance(port, int)
    return port


async def wait_until_started(server: uvicorn.Server) -> None:
    while not server.started:
        await asyncio.sleep(POLL_SECONDS)


async def test_http_round_trip_and_shutdown_terminates(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    stub_launch(control_plane, fake_rayd)
    port = free_loopback_port()
    app = mcp_server(control_plane, fake_rayd).streamable_http_app()
    uvicorn_server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    serve_task = asyncio.create_task(uvicorn_server.serve())
    await asyncio.wait_for(wait_until_started(uvicorn_server), STEP_TIMEOUT_SECONDS)
    try:
        async with Client(f"http://127.0.0.1:{port}/mcp", raise_exceptions=True) as client:
            tools = await asyncio.wait_for(client.list_tools(), STEP_TIMEOUT_SECONDS)
            result = await asyncio.wait_for(
                client.call_tool("run_command", {"cmd": "echo http"}), STEP_TIMEOUT_SECONDS
            )
        stub_terminate(control_plane)
    finally:
        uvicorn_server.should_exit = True
        await asyncio.wait_for(serve_task, STEP_TIMEOUT_SECONDS)
    assert len(tools.tools) == 6
    assert result.is_error is False
    assert result.structured_content is not None
    assert result.structured_content["stdout"] == "http\n"
    assert result.structured_content["exit_code"] == 0
    control_plane.microvms.assert_no_pending_responses()
