"""`AsyncSandbox.pty` y `AsyncPtyHandle`: misma superficie que la versión
síncrona sobre `grpc.aio`, contra el mismo `rayd` falso."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator

import pytest

from rayito import AsyncCommandHandle, AsyncPtyHandle, AsyncSandbox, PtySize
from rayito._limits import DEFAULT_PORT
from rayito.exceptions import (
    CommandExitException,
    InvalidArgumentException,
    NotFoundException,
    SandboxException,
    TimeoutException,
)

from .conftest import (
    ACCESS_TOKEN,
    IMAGE_ARN,
    SANDBOX_ID,
    RaydEndpoint,
    StubbedControlPlane,
    auth_token_response,
    microvm_response,
)

READ_BUDGET_SECONDS = 5.0


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


@pytest.fixture
async def sandbox(
    control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint
) -> AsyncIterator[AsyncSandbox]:
    stub_launch(control_plane, fake_rayd)
    created = await AsyncSandbox.create(
        IMAGE_ARN,
        idle=None,
        access_token=ACCESS_TOKEN,
        control_plane=control_plane.plane,
        transport=fake_rayd.transport,
    )
    try:
        yield created
    finally:
        control_plane.microvms.add_response(
            "terminate_microvm", {}, expected_params={"microvmIdentifier": SANDBOX_ID}
        )
        await created.kill()


def output_line(text: str) -> bytes:
    return f"\r\n{text}\r\n".encode()


async def read_until(
    handle: AsyncPtyHandle, needle: bytes, timeout: float = READ_BUDGET_SECONDS
) -> bytes:
    buffer = b""
    deadline = time.monotonic() + timeout
    iterator = handle.__aiter__()
    while needle not in buffer:
        assert time.monotonic() < deadline, f"{needle!r} no llegó; buffer={buffer!r}"
        try:
            _, _, pty_bytes = await asyncio.wait_for(iterator.__anext__(), timeout=timeout)
        except StopAsyncIteration:
            raise AssertionError(f"el stream terminó sin {needle!r}; buffer={buffer!r}") from None
        assert pty_bytes is not None
        buffer += pty_bytes
    return buffer


async def test_async_create_echo_resize_and_kill(
    sandbox: AsyncSandbox, fake_rayd: RaydEndpoint
) -> None:
    chunks: list[bytes] = []
    pty = await sandbox.pty.create(
        size=PtySize(cols=100, rows=30), on_data=chunks.append, timeout=None
    )
    assert isinstance(pty, AsyncPtyHandle)
    assert isinstance(pty, AsyncCommandHandle)
    assert pty.pid > 0
    await sandbox.pty.send_input(pty.pid, "echo async-pty\n")
    buffer = await read_until(pty, output_line("async-pty"))
    assert b"".join(chunks) == buffer
    assert "async-pty" in pty.stdout
    assert pty.stderr == ""
    await pty.send_input("stty size\n")
    await read_until(pty, b"30 100\r\n")
    await pty.resize(PtySize(cols=120, rows=40))
    await pty.send_stdin("stty size\n")
    await read_until(pty, b"40 120\r\n")
    listed = [info for info in await sandbox.commands.list() if info.pid == pty.pid]
    assert listed[0].kind == "pty"
    assert await sandbox.pty.kill(pty.pid) is True
    with pytest.raises(CommandExitException) as excinfo:
        await pty.wait()
    assert excinfo.value.exit_code == 137
    assert await sandbox.pty.kill(pty.pid) is False
    assert await pty.kill() is False
    assert fake_rayd.pty.create_requests[-1].timeout_ms == 0
    assert repr(pty).startswith("AsyncPtyHandle(pid=")


async def test_async_connect_replay_exit_and_errors(sandbox: AsyncSandbox) -> None:
    pty = await sandbox.pty.create(timeout=None)
    await pty.send_input("echo uno\n")
    await read_until(pty, output_line("uno"))
    replay = await sandbox.pty.connect(pty.pid, from_seq=1)
    await read_until(replay, output_line("uno"))
    assert replay.last_seq == pty.last_seq
    with pytest.raises(NotFoundException):
        await sandbox.pty.connect(pty.pid, from_seq=pty.last_seq + 50)
    with pytest.raises(NotFoundException):
        await sandbox.pty.connect(999_999)
    await pty.send_input("exit 3\n")
    with pytest.raises(CommandExitException) as excinfo:
        await pty.wait()
    assert excinfo.value.exit_code == 3
    with pytest.raises(NotFoundException):
        await sandbox.pty.send_input(pty.pid, "x")
    process = await sandbox.commands.run("sleep 30", background=True)
    with pytest.raises(InvalidArgumentException):
        await sandbox.pty.send_input(process.pid, "x")
    with pytest.raises(InvalidArgumentException):
        await sandbox.commands.send_stdin(pty.pid, "x")
    assert await process.kill() is True


async def test_async_timeout_disconnect_and_validation(
    sandbox: AsyncSandbox, fake_rayd: RaydEndpoint
) -> None:
    timed = await sandbox.pty.create(timeout=0.2)
    with pytest.raises(TimeoutException):
        await timed.wait()
    assert timed.error == "deadline_exceeded"
    pty = await sandbox.pty.create(timeout=None)
    pty.disconnect()
    with pytest.raises(SandboxException, match="desconectado"):
        await pty.wait()
    assert [chunk async for chunk in pty] == []
    assert await pty.kill() is True
    with pytest.raises(InvalidArgumentException):
        await sandbox.pty.create(shell="bash")
    with pytest.raises(InvalidArgumentException):
        await sandbox.pty.resize(1, PtySize(cols=5000, rows=1))
    assert len(fake_rayd.pty.peers) == 2
