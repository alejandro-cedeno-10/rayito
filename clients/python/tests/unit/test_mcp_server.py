"""El servidor MCP en memoria (`Client(server)`) contra el `rayd` falso y el
Stubber: las seis herramientas, el ciclo de vida del `SandboxLease`, la tabla
de errores de design D7 y la higiene de logging de D11."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest
from mcp import Client
from mcp.types import ImageContent

import rayito
from rayito._sandbox_base import ACCESS_TOKEN_ENV_VAR
from rayito.exceptions import SandboxNotFoundException, SandboxStateException
from rayito.mcp import McpSettings
from rayito.sandbox_async.commands import AsyncCommands

from .conftest import (
    ACCESS_TOKEN,
    IMAGE_ARN,
    JWE,
    SANDBOX_ID,
    RaydEndpoint,
    StubbedControlPlane,
    list_item,
)
from .fake_code import ONE_PIXEL_PNG_BASE64
from .mcp_support import (
    error_text,
    first_json,
    launched_client,
    mcp_server,
    stub_launch,
    stub_terminate,
)

TOOL_NAMES = ["run_code", "run_command", "read_file", "write_file", "list_files", "list_sandboxes"]
CODE_MARKER = "MARKER_CODE_7f3a"
FILE_MARKER = "MARKER_FILE_9c1d"


@pytest.fixture(autouse=True)
def access_token_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ACCESS_TOKEN_ENV_VAR, ACCESS_TOKEN)


async def test_server_identity_and_instructions(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    async with Client(mcp_server(control_plane, fake_rayd), raise_exceptions=True) as client:
        assert client.server_info is not None
        assert client.server_info.name == "rayito"
        assert client.server_info.version == rayito.__version__
        assert client.instructions is not None
        assert "run_code" in client.instructions


async def test_tool_list_names_annotations_and_schemas(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    async with Client(mcp_server(control_plane, fake_rayd), raise_exceptions=True) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    assert list(tools) == TOOL_NAMES
    assert tools["run_code"].output_schema is None
    for name in TOOL_NAMES[1:]:
        assert tools[name].output_schema is not None, name
    hints = {
        name: (
            tool.annotations.read_only_hint,
            tool.annotations.destructive_hint,
            tool.annotations.idempotent_hint,
            tool.annotations.open_world_hint,
        )
        for name, tool in tools.items()
        if tool.annotations is not None
    }
    assert hints == {
        "run_code": (False, False, None, True),
        "run_command": (False, False, None, True),
        "read_file": (True, None, True, None),
        "write_file": (False, True, True, None),
        "list_files": (True, None, True, None),
        "list_sandboxes": (True, None, True, None),
    }
    assert "ctx" not in tools["run_code"].input_schema["properties"]
    assert tools["run_code"].input_schema["properties"]["timeout"]["default"] == 300
    assert tools["run_command"].input_schema["properties"]["timeout"]["default"] == 60
    assert tools["list_files"].input_schema["properties"]["depth"]["maximum"] == 5
    for tool in tools.values():
        assert tool.description and "sandbox" in tool.description


async def test_list_sandboxes_never_creates_a_sandbox(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    control_plane.microvms.add_response(
        "list_microvms", {"items": [list_item("x", "RUNNING"), list_item("y", "SUSPENDED")]}
    )
    async with Client(mcp_server(control_plane, fake_rayd), raise_exceptions=True) as client:
        result = await client.call_tool("list_sandboxes")
    assert result.is_error is False
    assert result.structured_content is not None
    sandboxes = result.structured_content["sandboxes"]
    assert [item["sandbox_id"] for item in sandboxes] == ["x", "y"]
    assert [item["state"] for item in sandboxes] == ["RUNNING", "SUSPENDED"]
    assert all(item["current"] is False for item in sandboxes)
    assert all(item["template"] == IMAGE_ARN for item in sandboxes)


async def test_first_command_creates_the_sandbox_and_the_second_reuses_it(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    async with launched_client(control_plane, fake_rayd) as client:
        first = await client.call_tool("run_command", {"cmd": "echo hola"})
        second = await client.call_tool("run_command", {"cmd": "echo otra"})
    assert first.is_error is False
    assert first.structured_content == {
        "stdout": "hola\n",
        "stderr": "",
        "exit_code": 0,
        "truncated": False,
    }
    assert second.structured_content is not None
    assert second.structured_content["stdout"] == "otra\n"


async def test_concurrent_first_calls_create_one_sandbox(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    async with launched_client(control_plane, fake_rayd) as client:
        results = await asyncio.gather(
            client.call_tool("run_command", {"cmd": "echo a"}),
            client.call_tool("run_command", {"cmd": "echo b"}),
        )
    outputs = sorted(
        result.structured_content["stdout"]
        for result in results
        if result.structured_content is not None
    )
    assert outputs == ["a\n", "b\n"]


async def test_list_sandboxes_marks_the_current_sandbox(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    async with launched_client(control_plane, fake_rayd) as client:
        await client.call_tool("run_command", {"cmd": "echo hola"})
        control_plane.microvms.add_response(
            "list_microvms",
            {"items": [list_item("other", "RUNNING"), list_item(SANDBOX_ID, "RUNNING")]},
        )
        result = await client.call_tool("list_sandboxes")
    assert result.structured_content is not None
    current = [item["current"] for item in result.structured_content["sandboxes"]]
    assert current == [False, True]


async def test_exit_codes_are_data(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    async with launched_client(control_plane, fake_rayd) as client:
        exit_three = await client.call_tool("run_command", {"cmd": "exit 3"})
        unknown = await client.call_tool("run_command", {"cmd": "nosuchcmd"})
    assert exit_three.is_error is False
    assert exit_three.structured_content is not None
    assert exit_three.structured_content["exit_code"] == 3
    assert unknown.is_error is False
    assert unknown.structured_content is not None
    assert unknown.structured_content["exit_code"] == 127
    assert "command not found" in unknown.structured_content["stderr"]


async def test_run_command_honours_cwd_and_timeout(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    async with launched_client(control_plane, fake_rayd) as client:
        result = await client.call_tool("run_command", {"cmd": "pwd", "cwd": "/tmp"})
        timed_out = await client.call_tool("run_command", {"cmd": "sleep 10", "timeout": 1})
    assert result.structured_content is not None
    assert result.structured_content["stdout"] == "/tmp\n"
    assert fake_rayd.process.start_requests[0].process.cwd == "/tmp"
    assert "superó el timeout de 1 s" in error_text(timed_out)


async def test_files_round_trip_and_missing_file(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    async with launched_client(control_plane, fake_rayd) as client:
        written = await client.call_tool(
            "write_file", {"path": "/home/user/a.txt", "content": "hola"}
        )
        read = await client.call_tool("read_file", {"path": "/home/user/a.txt"})
        listed = await client.call_tool("list_files", {"path": "/home/user"})
        missing = await client.call_tool("read_file", {"path": "/home/user/missing.txt"})
    assert written.structured_content == {"path": "/home/user/a.txt", "size": 4}
    assert read.structured_content == {
        "path": "/home/user/a.txt",
        "content": "hola",
        "size": 4,
        "truncated": False,
    }
    assert listed.structured_content is not None
    entries = {entry["name"]: entry for entry in listed.structured_content["entries"]}
    assert entries["a.txt"]["type"] == "file"
    assert entries["a.txt"]["size"] == 4
    assert error_text(missing).startswith("Error executing tool read_file")
    assert "path not found" in error_text(missing)


async def test_run_code_text_image_and_error(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    async with launched_client(control_plane, fake_rayd) as client:
        text = await client.call_tool("run_code", {"code": "1+1"})
        plot = await client.call_tool("run_code", {"code": "plot"})
        error = await client.call_tool("run_code", {"code": "raise ValueError('x')"})
    assert text.is_error is False
    assert first_json(text)["text"] == "2"
    assert first_json(text)["error"] is None
    assert plot.is_error is False
    assert "image/png" in first_json(plot)["results"][0]["mime_types"]
    image = plot.content[1]
    assert isinstance(image, ImageContent)
    assert image.mime_type == "image/png"
    assert image.data == ONE_PIXEL_PNG_BASE64
    assert error.is_error is False
    assert first_json(error)["error"]["name"] == "ValueError"


async def test_run_code_keeps_kernel_state_between_calls(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    async with launched_client(control_plane, fake_rayd) as client:
        await client.call_tool("run_code", {"code": "x = 42"})
        result = await client.call_tool("run_code", {"code": "x"})
    assert first_json(result)["text"] == "42"


async def test_missing_template_is_a_tool_error_without_aws_calls(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    server = mcp_server(control_plane, fake_rayd, McpSettings(template=None))
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("run_command", {"cmd": "echo x"})
        assert "RAYITO_TEMPLATE" in error_text(result)
        listed = await client.call_tool("list_sandboxes")
        assert "RAYITO_TEMPLATE" in error_text(listed)


async def test_failed_creation_is_reported_and_retried(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    control_plane.microvms.add_client_error(
        "run_microvm",
        service_error_code="InsufficientCapacityException",
        service_message="no hay capacidad",
        http_status_code=503,
    )
    async with Client(mcp_server(control_plane, fake_rayd), raise_exceptions=True) as client:
        failed = await client.call_tool("run_command", {"cmd": "echo hola"})
        text = error_text(failed)
        assert text.startswith("Error executing tool run_command")
        assert "no se pudo crear el sandbox: no hay capacidad" in text
        stub_launch(control_plane, fake_rayd)
        retried = await client.call_tool("run_command", {"cmd": "echo hola"})
        assert retried.is_error is False
        stub_terminate(control_plane)


async def test_no_terminate_when_no_sandbox_was_created(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    async with Client(mcp_server(control_plane, fake_rayd), raise_exceptions=True) as client:
        await client.list_tools()
    control_plane.microvms.assert_no_pending_responses()


async def test_terminate_exactly_once_on_client_exit(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> None:
    stub_launch(control_plane, fake_rayd)
    async with Client(mcp_server(control_plane, fake_rayd), raise_exceptions=True) as client:
        await client.call_tool("run_command", {"cmd": "echo hola"})
        stub_terminate(control_plane)
    control_plane.microvms.assert_no_pending_responses()


async def test_vanished_sandbox_resets_the_lease(
    control_plane: StubbedControlPlane,
    fake_rayd: RaydEndpoint,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def gone(self: AsyncCommands, cmd: str, **kwargs: Any) -> Any:
        raise SandboxNotFoundException(f"el sandbox {SANDBOX_ID} está TERMINATED: timeout")

    async with launched_client(control_plane, fake_rayd) as client:
        await client.call_tool("run_command", {"cmd": "echo hola"})
        with monkeypatch.context() as patched:
            patched.setattr(AsyncCommands, "run", gone)
            vanished = await client.call_tool("run_command", {"cmd": "echo hola"})
        stub_launch(control_plane, fake_rayd)
        recreated = await client.call_tool("run_command", {"cmd": "echo hola"})
    text = error_text(vanished)
    assert f"el sandbox {SANDBOX_ID} ya no existe" in text
    assert "la siguiente llamada crea uno nuevo" in text
    assert recreated.is_error is False
    control_plane.microvms.assert_no_pending_responses()


async def test_suspending_sandbox_asks_to_retry_without_resetting_the_lease(
    control_plane: StubbedControlPlane,
    fake_rayd: RaydEndpoint,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def suspending(self: AsyncCommands, cmd: str, **kwargs: Any) -> Any:
        raise SandboxStateException("el sandbox está suspending; reintenta cuando vuelva a RUNNING")

    async with launched_client(control_plane, fake_rayd) as client:
        await client.call_tool("run_command", {"cmd": "echo hola"})
        with monkeypatch.context() as patched:
            patched.setattr(AsyncCommands, "run", suspending)
            in_transition = await client.call_tool("run_command", {"cmd": "echo hola"})
        retried = await client.call_tool("run_command", {"cmd": "echo otra"})
    text = error_text(in_transition)
    assert "en transición" in text
    assert "reintenta" in text
    assert "ya no existe" not in text
    assert retried.is_error is False
    assert retried.structured_content is not None
    assert retried.structured_content["stdout"] == "otra\n"
    control_plane.microvms.assert_no_pending_responses()


async def test_payloads_are_never_logged(
    control_plane: StubbedControlPlane,
    fake_rayd: RaydEndpoint,
    caplog: pytest.LogCaptureFixture,
) -> None:
    stub_launch(control_plane, fake_rayd)
    server = mcp_server(
        control_plane, fake_rayd, McpSettings(template=IMAGE_ARN, idle_seconds=0, log_level="DEBUG")
    )
    with caplog.at_level(logging.DEBUG):
        async with Client(server, raise_exceptions=True) as client:
            await client.call_tool("run_code", {"code": f"print('{CODE_MARKER}')"})
            await client.call_tool(
                "write_file", {"path": "/home/user/m.txt", "content": FILE_MARKER}
            )
            await client.call_tool("run_command", {"cmd": f"echo {CODE_MARKER}"})
            stub_terminate(control_plane)
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "run_code" in logged
    assert CODE_MARKER not in logged
    assert FILE_MARKER not in logged
    assert JWE not in logged
    assert not [record for record in caplog.records if record.name.startswith("botocore")]
