"""`rayito sandbox create | connect | exec | metrics` contra un plano de
control en memoria y el `rayd` falso, y el puente de terminal en modo
tubería."""

from __future__ import annotations

import dataclasses
import importlib
import io
import json
import os
import select
import shlex
import stat
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from rayito._aws import LaunchRequest
from rayito._models import SandboxInfo
from rayito.cli._session import Clients
from rayito.cli._terminal import pump_descriptor, run_terminal, translate_windows_key
from rayito.cli._tokens import (
    InvalidPairError,
    TokenFileError,
    parse_pairs,
    resolve_token,
    write_token_file,
)
from rayito.cli.app import app
from rayito.cli.sandbox import connect_sandbox

from ..conftest import ACCESS_TOKEN, RaydEndpoint
from ..fake_process import CannedReply
from .conftest import FakeControlPlane, sandbox_info

SANDBOX_ID = "microvm-x"
NO_TOKEN_ENV = {"RAYITO_ACCESS_TOKEN": None}
NO_TOKEN_WITH_TEMPLATE_ENV = {"RAYITO_ACCESS_TOKEN": None, "RAYITO_TEMPLATE": "rayito-base"}


@dataclass
class LaunchingPlane(FakeControlPlane):
    """El plano en memoria de la CLI con `run-microvm`: apunta el nuevo
    sandbox al `rayd` falso y anota si el fichero de token ya existía."""

    endpoint: str = ""
    launches: list[LaunchRequest] = field(default_factory=list)
    token_file: Path | None = None
    token_file_existed: list[bool] = field(default_factory=list)

    def run_microvm(self, request: Any) -> SandboxInfo:
        self.launches.append(request)
        if self.token_file is not None:
            self.token_file_existed.append(self.token_file.exists())
        info = sandbox_info(SANDBOX_ID, "PENDING", endpoint=self.endpoint)
        self.infos[SANDBOX_ID] = dataclasses.replace(info, state="RUNNING")
        return info


@pytest.fixture
def plane(fake_rayd: RaydEndpoint) -> LaunchingPlane:
    launching = LaunchingPlane(endpoint=fake_rayd.host)
    launching.infos[SANDBOX_ID] = sandbox_info(SANDBOX_ID, endpoint=fake_rayd.host)
    return launching


@pytest.fixture
def agent_clients(clients: Clients, plane: LaunchingPlane, fake_rayd: RaydEndpoint) -> Clients:
    return dataclasses.replace(clients, control_plane_override=plane, transport=fake_rayd.transport)


@pytest.fixture
def token_file(tmp_path: Path) -> Path:
    path = tmp_path / "token"
    path.write_text(f"{ACCESS_TOKEN}\n", encoding="utf-8")
    return path


def test_detach_needs_somewhere_to_keep_the_token(
    runner: CliRunner, agent_clients: Clients, plane: LaunchingPlane
) -> None:
    result = runner.invoke(
        app, ["sandbox", "create", "--detach"], obj=agent_clients, env=NO_TOKEN_ENV
    )
    assert result.exit_code == 2
    assert "--token-file" in result.stderr
    assert plane.launches == []


def test_detached_create_writes_the_token_file_first_and_never_prints_it(
    runner: CliRunner, agent_clients: Clients, plane: LaunchingPlane, tmp_path: Path
) -> None:
    target = tmp_path / "t"
    plane.token_file = target
    result = runner.invoke(
        app,
        ["--json", "sandbox", "create", "--detach", "--token-file", str(target)],
        obj=agent_clients,
        env=NO_TOKEN_WITH_TEMPLATE_ENV,
    )
    assert result.exit_code == 0, result.stderr
    assert plane.token_file_existed == [True]
    token = target.read_text(encoding="utf-8")
    assert token
    document = json.loads(result.stdout)
    assert document["sandbox_id"] == SANDBOX_ID
    assert set(document) == {"sandbox_id", "endpoint", "template", "template_version", "expires_at"}
    assert token not in result.stdout and token not in result.stderr
    assert plane.terminated == []
    if os.name != "nt":
        assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_existing_token_file_exits_2_without_launching(
    runner: CliRunner, agent_clients: Clients, plane: LaunchingPlane, tmp_path: Path
) -> None:
    target = tmp_path / "t"
    target.write_text("previous", encoding="utf-8")
    result = runner.invoke(
        app,
        ["sandbox", "create", "--detach", "--token-file", str(target)],
        obj=agent_clients,
        env=NO_TOKEN_ENV,
    )
    assert result.exit_code == 2
    assert "ya existe" in result.stderr
    assert plane.launches == []
    assert target.read_text(encoding="utf-8") == "previous"


def test_detached_create_uses_the_environment_token_and_prints_the_id(
    runner: CliRunner, agent_clients: Clients, plane: LaunchingPlane
) -> None:
    result = runner.invoke(
        app,
        ["sandbox", "create", "rayito-base", "--detach", "--metadata", "team=x", "-e", "A=1"],
        obj=agent_clients,
        env={"RAYITO_ACCESS_TOKEN": ACCESS_TOKEN},
    )
    assert result.exit_code == 0, result.stderr
    assert result.stdout.strip() == SANDBOX_ID
    assert ACCESS_TOKEN not in result.stdout
    payload = json.loads(plane.launches[0].run_hook_payload)
    assert payload["envs"] == {"A": "1"}
    assert payload["metadata"] == {"team": "x"}


def test_malformed_pair_exits_2(
    runner: CliRunner, agent_clients: Clients, plane: LaunchingPlane, token_file: Path
) -> None:
    arguments = ["sandbox", "exec", SANDBOX_ID, "-e", "SECRETVALUE"]
    result = runner.invoke(
        app, [*arguments, "--token-file", str(token_file), "--", "true"], obj=agent_clients
    )
    assert result.exit_code == 2
    assert "SECRETVALUE" not in result.stderr
    assert plane.tokens == []


def test_connect_without_a_token_exits_2(
    runner: CliRunner, agent_clients: Clients, plane: LaunchingPlane
) -> None:
    result = runner.invoke(
        app, ["sandbox", "connect", SANDBOX_ID], obj=agent_clients, env=NO_TOKEN_ENV
    )
    assert result.exit_code == 2
    assert "access token" in result.stderr
    assert plane.tokens == []


def test_exec_streams_and_passes_the_remote_exit_code(
    runner: CliRunner, agent_clients: Clients, fake_rayd: RaydEndpoint, token_file: Path
) -> None:
    fake_rayd.process.reply_when("sh -c", CannedReply(stdout="hi\n", exit_code=3))
    command = ["sh", "-c", "echo hi; exit 3"]
    result = runner.invoke(
        app,
        ["sandbox", "exec", SANDBOX_ID, "--token-file", str(token_file), "--", *command],
        obj=agent_clients,
    )
    assert result.exit_code == 3, result.stderr
    assert "hi" in result.stdout
    assert fake_rayd.process.commands()[-1] == shlex.join(command)
    assert fake_rayd.process.start_requests[-1].timeout_ms == 0


def test_exec_background_prints_the_pid(
    runner: CliRunner, agent_clients: Clients, fake_rayd: RaydEndpoint, token_file: Path
) -> None:
    result = runner.invoke(
        app,
        ["sandbox", "exec", SANDBOX_ID, "-b", "--token-file", str(token_file), "--", "sleep", "5"],
        obj=agent_clients,
    )
    assert result.exit_code == 0, result.stderr
    assert int(result.stdout.strip()) in fake_rayd.process.processes
    assert fake_rayd.process.commands()[-1] == "sleep 5"


def test_metrics_in_json(
    runner: CliRunner, agent_clients: Clients, fake_rayd: RaydEndpoint, token_file: Path
) -> None:
    fake_rayd.servicer.mem_cache_bytes = 4096
    result = runner.invoke(
        app,
        ["--json", "sandbox", "metrics", SANDBOX_ID, "--token-file", str(token_file)],
        obj=agent_clients,
    )
    assert result.exit_code == 0, result.stderr
    document = json.loads(result.stdout)
    assert document["cpu_count"] == 1
    assert document["mem_total"] == 2 * 1024 * 1024 * 1024
    assert document["mem_cache"] == 4096
    assert ACCESS_TOKEN not in result.stdout


def test_connect_forwards_piped_input_and_returns_the_shell_code(
    runner: CliRunner, agent_clients: Clients, fake_rayd: RaydEndpoint, token_file: Path
) -> None:
    result = runner.invoke(
        app,
        ["sandbox", "connect", SANDBOX_ID, "--token-file", str(token_file)],
        obj=agent_clients,
        input="echo hola\n",
    )
    assert result.exit_code == 0, result.stderr
    assert "hola" in result.stdout
    [pty] = fake_rayd.pty.ptys.values()
    assert pty.inputs == [b"echo hola\n", b"\x04"]
    request = fake_rayd.pty.create_requests[-1]
    assert (request.size.cols, request.size.rows, request.timeout_ms) == (80, 24, 0)


def binary_stream(data: bytes = b"") -> io.TextIOWrapper:
    return io.TextIOWrapper(io.BytesIO(data), encoding="utf-8")


def written(stream: io.TextIOWrapper) -> bytes:
    buffer = stream.buffer
    assert isinstance(buffer, io.BytesIO)
    return buffer.getvalue()


def test_run_terminal_pipe_mode_round_trip(agent_clients: Clients, fake_rayd: RaydEndpoint) -> None:
    sandbox = connect_sandbox(agent_clients, SANDBOX_ID, ACCESS_TOKEN)
    stdout = binary_stream()
    try:
        code = run_terminal(sandbox, stdin=binary_stream(b"echo hola\nexit\n"), stdout=stdout)
    finally:
        sandbox.close()
    assert code == 0
    assert b"hola" in written(stdout)
    [pty] = fake_rayd.pty.ptys.values()
    assert pty.inputs[0] == b"echo hola\nexit\n"


def test_run_terminal_sends_eot_at_end_of_input(
    agent_clients: Clients, fake_rayd: RaydEndpoint
) -> None:
    sandbox = connect_sandbox(agent_clients, SANDBOX_ID, ACCESS_TOKEN)
    try:
        code = run_terminal(sandbox, stdin=binary_stream(b"echo hola\n"), stdout=binary_stream())
    finally:
        sandbox.close()
    [pty] = fake_rayd.pty.ptys.values()
    assert (code, pty.inputs) == (0, [b"echo hola\n", b"\x04"])


def test_run_terminal_returns_the_shell_exit_code(
    agent_clients: Clients, fake_rayd: RaydEndpoint
) -> None:
    sandbox = connect_sandbox(agent_clients, SANDBOX_ID, ACCESS_TOKEN)
    try:
        code = run_terminal(sandbox, stdin=binary_stream(b"exit 7\n"), stdout=binary_stream())
    finally:
        sandbox.close()
    assert code == 7


def emulate_terminal(
    controller: int, terminal: int, termios: Any, typed: bytes, stop: threading.Event
) -> threading.Thread:
    """El otro extremo de la PTY local, como un emulador de terminal: teclea
    `typed` en cuanto la terminal está en modo raw (así TCSAFLUSH no lo
    descarta) y lee todo lo que la terminal escribe (sin lector,
    `tcsetattr` con TCSADRAIN/TCSAFLUSH se bloquea en macOS). Lee con
    `select` hasta `stop`, nunca bloqueado en `read`."""

    def run() -> None:
        deadline = time.monotonic() + 10
        while termios.tcgetattr(terminal)[3] & termios.ICANON and time.monotonic() < deadline:
            time.sleep(0.01)
        os.write(controller, typed)
        while not stop.is_set():
            ready, _, _ = select.select([controller], [], [], 0.05)
            if ready:
                try:
                    os.read(controller, 4096)
                except OSError:
                    return

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


@pytest.mark.skipif(sys.platform == "win32", reason="pty.openpty y termios sólo existen en POSIX")
def test_terminal_attributes_are_restored_on_posix(
    agent_clients: Clients, fake_rayd: RaydEndpoint
) -> None:
    pty_module = importlib.import_module("pty")
    termios = importlib.import_module("termios")
    controller, terminal = pty_module.openpty()
    before = termios.tcgetattr(terminal)
    stop = threading.Event()
    emulator = emulate_terminal(controller, terminal, termios, b"exit 0\n", stop)
    sandbox = connect_sandbox(agent_clients, SANDBOX_ID, ACCESS_TOKEN)
    try:
        with os.fdopen(terminal, "r", closefd=False) as stdin:
            try:
                code = run_terminal(sandbox, stdin=stdin, stdout=binary_stream())
            finally:
                sandbox.close()
        after = termios.tcgetattr(terminal)
        stop.set()
        emulator.join(5)
        os.write(controller, b"late\n")
        ready, _, _ = select.select([terminal], [], [], 2)
        leftover = os.read(terminal, 16) if ready else b""
    finally:
        stop.set()
        os.close(controller)
        os.close(terminal)
    assert code == 0
    assert without_kernel_flags(after, termios) == without_kernel_flags(before, termios)
    assert leftover == b"late\n"


def without_kernel_flags(attributes: list[Any], termios: Any) -> list[Any]:
    """Los atributos sin PENDIN: el kernel BSD/macOS lo enciende solo al
    volver a modo canónico para reprocesar la entrada pendiente; no es
    algo que `run_terminal` guarde ni restaure."""
    pending = getattr(termios, "PENDIN", 0)
    return [*attributes[:3], attributes[3] & ~pending, *attributes[4:]]


@pytest.mark.skipif(sys.platform == "win32", reason="select sobre tuberías sólo existe en POSIX")
def test_terminal_reader_stops_without_consuming_later_input() -> None:
    reading, writing = os.pipe()
    sent: list[bytes] = []
    stop = threading.Event()
    reader = threading.Thread(
        target=pump_descriptor, args=(reading, sent.append, os.read, stop), daemon=True
    )
    try:
        reader.start()
        os.write(writing, b"before")
        deadline = time.monotonic() + 5
        while not sent and time.monotonic() < deadline:
            time.sleep(0.01)
        stop.set()
        reader.join(5)
        os.write(writing, b"after")
        ready, _, _ = select.select([reading], [], [], 2)
        leftover = os.read(reading, 16) if ready else b""
    finally:
        os.close(writing)
        os.close(reading)
    assert not reader.is_alive()
    assert sent == [b"before"]
    assert leftover == b"after"


def test_resolve_token_prefers_the_file(tmp_path: Path) -> None:
    path = tmp_path / "t"
    path.write_text("  from-file \n", encoding="utf-8")
    environ = {"RAYITO_ACCESS_TOKEN": "from-env"}
    assert resolve_token(path, environ) == "from-file"
    assert resolve_token(None, environ) == "from-env"
    assert resolve_token(None, {}) is None
    assert resolve_token(None, {"RAYITO_ACCESS_TOKEN": ""}) is None
    with pytest.raises(TokenFileError) as missing:
        resolve_token(tmp_path / "absent", environ)
    assert "absent" in str(missing.value)


def test_write_token_file_refuses_to_overwrite(tmp_path: Path) -> None:
    path = tmp_path / "t"
    write_token_file(path, "secret-token")
    assert path.read_text(encoding="utf-8") == "secret-token"
    with pytest.raises(TokenFileError) as existing:
        write_token_file(path, "other")
    assert "secret-token" not in str(existing.value)
    assert path.read_text(encoding="utf-8") == "secret-token"


def test_parse_pairs() -> None:
    assert parse_pairs(["A=1", "B=x=y", "C="], option="--env") == {"A": "1", "B": "x=y", "C": ""}
    assert parse_pairs(None, option="--env") == {}
    for bad in (["NOEQUALS"], ["=v"]):
        with pytest.raises(InvalidPairError, match="--env"):
            parse_pairs(bad, option="--env")


@pytest.mark.parametrize(
    ("prefix", "code", "sequence"),
    [
        ("\xe0", "H", "\x1b[A"),
        ("\xe0", "P", "\x1b[B"),
        ("\x00", "K", "\x1b[D"),
        ("\x00", "M", "\x1b[C"),
        ("\xe0", "G", "\x1b[H"),
        ("\xe0", "O", "\x1b[F"),
        ("\xe0", "S", "\x1b[3~"),
        ("\xe0", "Z", None),
        ("a", "H", None),
    ],
)
def test_windows_extended_keys_become_vt_sequences(
    prefix: str, code: str, sequence: str | None
) -> None:
    assert translate_windows_key(prefix, code) == sequence
