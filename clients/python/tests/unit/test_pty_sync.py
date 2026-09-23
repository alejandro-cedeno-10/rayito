"""`Sandbox.pty` y `PtyHandle` contra el `rayd` falso (gRPC real en loopback)
y el plano de control con Stubber."""

from __future__ import annotations

import time
from collections.abc import Iterator

import pytest

from rayito import CommandHandle, PtyHandle, PtySize, Sandbox
from rayito._limits import DEFAULT_PORT
from rayito.exceptions import (
    AuthenticationException,
    CommandExitException,
    InvalidArgumentException,
    NotFoundException,
    SandboxException,
    SandboxStateException,
    TimeoutException,
)
from rayito.v1 import pty_pb2_grpc

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
def sandbox(control_plane: StubbedControlPlane, fake_rayd: RaydEndpoint) -> Iterator[Sandbox]:
    stub_launch(control_plane, fake_rayd)
    created = Sandbox.create(
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
        created.kill()


def output_line(text: str) -> bytes:
    """Una línea producida por el shell (no su eco de la entrada)."""
    return f"\r\n{text}\r\n".encode()


def read_until(handle: PtyHandle, needle: bytes, timeout: float = READ_BUDGET_SECONDS) -> bytes:
    """Itera el handle acumulando bytes hasta que aparece `needle`."""
    buffer = b""
    deadline = time.monotonic() + timeout
    iterator = iter(handle)
    while needle not in buffer:
        assert time.monotonic() < deadline, f"{needle!r} no llegó; buffer={buffer!r}"
        try:
            _, _, pty_bytes = next(iterator)
        except StopIteration:
            raise AssertionError(f"el stream terminó sin {needle!r}; buffer={buffer!r}") from None
        assert pty_bytes is not None
        buffer += pty_bytes
    return buffer


def test_create_echo_iteration_callbacks_and_stdout(
    sandbox: Sandbox, fake_rayd: RaydEndpoint
) -> None:
    chunks: list[bytes] = []
    pty = sandbox.pty.create(size=PtySize(cols=100, rows=30), on_data=chunks.append, timeout=None)
    assert isinstance(pty, PtyHandle)
    assert isinstance(pty, CommandHandle)
    assert pty.pid > 0
    assert pty.exit_code is None
    assert pty.error is None
    assert pty.reconnects == 0
    sandbox.pty.send_input(pty.pid, "echo hola\n")
    buffer = read_until(pty, output_line("hola"))
    assert b"echo hola\r\n" in buffer
    assert chunks
    assert all(isinstance(chunk, bytes) for chunk in chunks)
    assert b"".join(chunks) == buffer
    assert "hola" in pty.stdout
    assert pty.stderr == ""
    assert pty.last_seq > 0
    request = fake_rayd.pty.create_requests[-1]
    assert (request.size.cols, request.size.rows) == (100, 30)
    assert request.timeout_ms == 0
    assert fake_rayd.pty.deadlines["Create"][-1] is None
    assert repr(pty).startswith("PtyHandle(pid=")
    assert sandbox.pty.kill(pty.pid) is True


def test_wait_delivers_pty_output_to_on_pty(sandbox: Sandbox) -> None:
    pty = sandbox.pty.create(timeout=None)
    pty.send_input("echo hola; exit 0\n")
    chunks: list[bytes] = []
    stdout: list[str] = []
    result = pty.wait(on_pty=chunks.append, on_stdout=stdout.append)
    assert b"hola" in b"".join(chunks)
    assert stdout == []
    assert result.exit_code == 0


def test_pty_is_listed_and_refused_by_process_rpcs(sandbox: Sandbox) -> None:
    pty = sandbox.pty.create(timeout=None)
    listed = [info for info in sandbox.commands.list() if info.pid == pty.pid]
    assert len(listed) == 1
    assert listed[0].kind == "pty"
    assert listed[0].cmd == "/bin/bash"
    assert listed[0].args == ("-i", "-l")
    with pytest.raises(InvalidArgumentException):
        sandbox.commands.send_stdin(pty.pid, "x")
    with pytest.raises(InvalidArgumentException):
        sandbox.commands.close_stdin(pty.pid)
    with pytest.raises(InvalidArgumentException):
        sandbox.commands.connect(pty.pid)
    assert sandbox.commands.kill(pty.pid) is True
    with pytest.raises(CommandExitException) as excinfo:
        pty.wait()
    assert excinfo.value.exit_code == 137


def test_pty_rpcs_refuse_a_plain_process_pid(sandbox: Sandbox) -> None:
    process = sandbox.commands.run("sleep 30", background=True)
    with pytest.raises(InvalidArgumentException):
        sandbox.pty.send_input(process.pid, "x")
    with pytest.raises(InvalidArgumentException):
        sandbox.pty.resize(process.pid, PtySize())
    with pytest.raises(InvalidArgumentException):
        sandbox.pty.kill(process.pid)
    with pytest.raises(InvalidArgumentException):
        sandbox.pty.connect(process.pid)
    assert process.kill() is True


def test_resize_is_reflected_by_stty_and_recorded(
    sandbox: Sandbox, fake_rayd: RaydEndpoint
) -> None:
    pty = sandbox.pty.create(size=PtySize(cols=100, rows=30), timeout=None)
    pty.send_input("stty size\n")
    read_until(pty, b"30 100\r\n")
    pty.resize(PtySize(cols=120, rows=40))
    pty.send_input("stty size\n")
    read_until(pty, b"40 120\r\n")
    resize = fake_rayd.pty.resize_requests[-1]
    assert (resize.pid, resize.size.cols, resize.size.rows) == (pty.pid, 120, 40)
    with pytest.raises(InvalidArgumentException):
        sandbox.pty.resize(pty.pid, PtySize(cols=0, rows=1))
    assert pty.kill() is True


def test_identity_terminal_and_environment(sandbox: Sandbox) -> None:
    pty = sandbox.pty.create(timeout=None, envs={"FOO": "bar"})
    pty.send_input("id -u; tty\n")
    buffer = read_until(pty, b"/dev/pts/")
    assert b"1000\r\n" in buffer
    pty.send_input("echo $TERM $LANG $FOO\n")
    read_until(pty, b"xterm-256color C.UTF-8 bar\r\n")
    override = sandbox.pty.create(timeout=None, envs={"TERM": "vt100"})
    override.send_input("echo $TERM\n")
    read_until(override, b"vt100\r\n")
    assert pty.kill() is True
    assert override.kill() is True


def test_kill_then_wait_signaled_and_second_kill_false(sandbox: Sandbox) -> None:
    pty = sandbox.pty.create(timeout=None)
    assert sandbox.pty.kill(pty.pid) is True
    with pytest.raises(CommandExitException) as excinfo:
        pty.wait()
    assert excinfo.value.exit_code == 137
    assert excinfo.value.error == "signaled"
    assert pty.exit_code == 137
    assert sandbox.pty.kill(pty.pid) is False
    assert pty.kill() is False
    with pytest.raises(NotFoundException):
        sandbox.pty.send_input(pty.pid, "x")
    with pytest.raises(NotFoundException):
        sandbox.pty.resize(pty.pid, PtySize())
    assert pty.pid not in {info.pid for info in sandbox.commands.list()}


def test_exit_code_through_the_shell_and_retained_connect(sandbox: Sandbox) -> None:
    pty = sandbox.pty.create(timeout=None)
    pty.send_input("echo before\n")
    read_until(pty, b"before\r\n")
    pty.send_stdin("exit 3\n")
    with pytest.raises(CommandExitException) as excinfo:
        pty.wait()
    assert excinfo.value.exit_code == 3
    assert excinfo.value.error == "exited"
    assert "before" in excinfo.value.stdout
    retained = sandbox.pty.connect(pty.pid, from_seq=1)
    with pytest.raises(CommandExitException) as replayed:
        retained.wait()
    assert replayed.value.exit_code == 3
    assert "before" in retained.stdout
    assert retained.last_seq == pty.last_seq
    clean = sandbox.pty.create(timeout=None)
    clean.send_input("exit\n")
    assert clean.wait().exit_code == 0


def test_connect_replays_from_seq_and_maps_errors(
    sandbox: Sandbox, fake_rayd: RaydEndpoint
) -> None:
    pty = sandbox.pty.create(timeout=None)
    pty.send_input("echo uno\n")
    read_until(pty, output_line("uno"))
    replay = sandbox.pty.connect(pty.pid, from_seq=1)
    read_until(replay, output_line("uno"))
    assert replay.last_seq == pty.last_seq
    tail = sandbox.pty.connect(pty.pid, from_seq=pty.last_seq + 1)
    sandbox.pty.send_input(pty.pid, "echo dos\n")
    buffer = read_until(tail, output_line("dos"))
    assert b"uno" not in buffer
    with pytest.raises(NotFoundException):
        sandbox.pty.connect(pty.pid, from_seq=pty.last_seq + 50)
    with pytest.raises(NotFoundException):
        sandbox.pty.connect(999_999)
    assert [request.from_seq for request in fake_rayd.pty.connect_requests[:2]] == [
        1,
        pty.last_seq + 1,
    ]
    assert pty.kill() is True


def stream_channel_of(sandbox: Sandbox) -> object:
    """Lectura fresca del canal de streams (se abre en el primer uso)."""
    return sandbox._stream_channel


def test_streams_use_the_stream_channel_and_unaries_the_other(
    sandbox: Sandbox, fake_rayd: RaydEndpoint
) -> None:
    assert stream_channel_of(sandbox) is None
    pty = sandbox.pty.create(timeout=None)
    assert stream_channel_of(sandbox) is not None
    sandbox.pty.send_input(pty.pid, "echo x\n")
    read_until(pty, b"x\r\n")
    assert len(fake_rayd.pty.peers) == 2
    assert pty.kill() is True


def test_server_timeout_is_timeout_exception(sandbox: Sandbox, fake_rayd: RaydEndpoint) -> None:
    pty = sandbox.pty.create(timeout=0.2)
    assert fake_rayd.pty.create_requests[-1].timeout_ms == 200
    deadline = fake_rayd.pty.deadlines["Create"][-1]
    assert deadline is not None and 4.0 <= deadline <= 5.5
    with pytest.raises(TimeoutException):
        pty.wait()
    assert pty.error == "deadline_exceeded"
    assert pty.exit_code == 143


def test_default_timeout_is_sixty_seconds(sandbox: Sandbox, fake_rayd: RaydEndpoint) -> None:
    pty = sandbox.pty.create()
    assert fake_rayd.pty.create_requests[-1].timeout_ms == 60_000
    assert pty.kill() is True


def test_create_validation_and_agent_errors(sandbox: Sandbox, fake_rayd: RaydEndpoint) -> None:
    with pytest.raises(InvalidArgumentException):
        sandbox.pty.create(shell="bash")
    with pytest.raises(InvalidArgumentException):
        sandbox.pty.create(size=(80, 24))  # type: ignore[arg-type]
    assert fake_rayd.pty.create_requests == []
    with pytest.raises(InvalidArgumentException, match="cwd"):
        sandbox.pty.create(cwd="/does/not/exist")
    with pytest.raises(AuthenticationException) as excinfo:
        sandbox.pty.create(user="root")
    assert excinfo.value.proxy_rejected is False
    fake_rayd.pty.pty_devices = False
    with pytest.raises(InvalidArgumentException, match="pty devices"):
        sandbox.pty.create()
    fake_rayd.pty.pty_devices = True
    false_shell = sandbox.pty.create(shell="/bin/false", timeout=None)
    with pytest.raises(CommandExitException) as failed:
        false_shell.wait()
    assert failed.value.exit_code == 1


def test_phase_gate_on_create_waits_for_a_resume_then_gives_up(
    sandbox: Sandbox, fake_rayd: RaydEndpoint
) -> None:
    sandbox._reconnect_timeout = 0.3
    fake_rayd.process.phase = "suspending"
    with pytest.raises(SandboxStateException, match="suspend"):
        sandbox.pty.create(timeout=None)
    fake_rayd.process.phase = None
    pty = sandbox.pty.create(timeout=None)
    assert pty.kill() is True


def test_disconnect_keeps_the_pty_listed(sandbox: Sandbox) -> None:
    pty = sandbox.pty.create(timeout=None)
    pty.disconnect()
    assert pty.pid in {info.pid for info in sandbox.commands.list()}
    with pytest.raises(SandboxException, match="desconectado"):
        pty.wait()
    assert list(pty) == []
    assert pty.kill() is True


def test_iteration_ignores_keepalives_and_yields_pty_tuples(sandbox: Sandbox) -> None:
    pty = sandbox.pty.create(timeout=None)
    pty.send_input("echo a\n")
    iterator = iter(pty)
    stdout, stderr, data = next(iterator)
    assert stdout is None
    assert stderr is None
    assert isinstance(data, bytes)
    pty.send_input("exit\n")
    rest = list(iterator)
    assert all(chunk[0] is None and chunk[1] is None for chunk in rest)
    assert pty.wait().exit_code == 0


def test_pty_service_stub_is_the_unary_stub(sandbox: Sandbox) -> None:
    assert sandbox._stub(pty_pb2_grpc.PtyServiceStub, stream=False) is sandbox._pty_stub
