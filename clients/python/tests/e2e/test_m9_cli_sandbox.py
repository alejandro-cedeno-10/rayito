"""Aceptación de `rayito sandbox create|exec|connect|metrics` contra AWS real
(`openspec/changes/m9-e2b-v2-surface/design.md` D23, tarea 11.4).

La CLI corre como la usaría cualquiera, `sys.executable -m rayito.cli` en un
subproceso, sobre un sandbox creado con `sandbox create --detach
--token-file` (`--timeout 900`, el guardrail de `conftest.py`):

- `exec -- echo hi` sale 0 con `hi\\n`; `exec -- sh -c 'exit 3'` sale 3;
- `connect` en modo tubería (todo SO): `echo hola-$((1+1))` y `exit` por
  stdin, `hola-2` en stdout, salida 0;
- `connect` en una pseudo-terminal real con `pty.fork()` (sólo POSIX; en
  win32 se salta con el motivo): se escribe `echo hola-$((1+1))\\r` tras el
  prompt, se lee hasta ver el eco **y** la salida, luego `exit\\r` y 0.
  La aceptación exige una corrida en Linux (el workflow e2e en
  `ubuntu-24.04-arm` o WSL);
- `--json sandbox metrics`: `cpu_count >= 1` y `mem_total > 0`;
- teardown: `sandbox kill <id>` y, pase lo que pase, `terminate-microvm`.

El access token sólo existe en el fichero temporal (modo 0600) que crea
`create --detach`: el entorno de los subprocesos no lleva
`RAYITO_ACCESS_TOKEN`, nada lo imprime y cada salida se comprueba sin él.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import select
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

from rayito._aws import LambdaMicrovmsControlPlane
from rayito.exceptions import SandboxException

from .conftest import TEST_SANDBOX_TIMEOUT_SECONDS, E2ESettings

pytestmark = pytest.mark.e2e

ACCESS_TOKEN_VAR = "RAYITO_ACCESS_TOKEN"
CLI = (sys.executable, "-m", "rayito.cli")
COMMAND_BUDGET_SECONDS = 300
PTY_BUDGET_SECONDS = 120.0
PTY_EXIT_BUDGET_SECONDS = 30.0
MARKER_COMMAND = "echo hola-$((1+1))"
MARKER_OUTPUT = "hola-2"
PROMPT = re.compile(rb"[$#] (?:\x1b\[[0-9;?]*[A-Za-z])*$")
READ_CHUNK_BYTES = 4096


@dataclass(frozen=True)
class CliSandbox:
    sandbox_id: str
    token_file: Path
    env: Mapping[str, str]

    @property
    def token(self) -> str:
        return self.token_file.read_text(encoding="utf-8").strip()


def report(label: str, value: object) -> None:
    print(f"\n[m9-cli] {label}: {value}", flush=True)


def cli_env(e2e_settings: E2ESettings) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key != ACCESS_TOKEN_VAR}
    if e2e_settings.region:
        env["AWS_REGION"] = e2e_settings.region
    env["PYTHONUNBUFFERED"] = "1"
    return env


def run_cli(
    args: Sequence[str],
    env: Mapping[str, str],
    *,
    stdin: bytes | None = None,
) -> tuple[int, str, str, float]:
    started = time.perf_counter()
    completed = subprocess.run(
        [*CLI, *args],
        env=dict(env),
        input=stdin,
        stdin=None if stdin is not None else subprocess.DEVNULL,
        capture_output=True,
        timeout=COMMAND_BUDGET_SECONDS,
        check=False,
    )
    elapsed = time.perf_counter() - started
    stdout = completed.stdout.decode("utf-8", errors="replace")
    stderr = completed.stderr.decode("utf-8", errors="replace")
    return completed.returncode, stdout, stderr, elapsed


def assert_no_token(sandbox: CliSandbox, *outputs: str) -> None:
    leaked = any(sandbox.token in output for output in outputs)
    assert not leaked, "el access token apareció en la salida de la CLI"


@pytest.fixture(scope="module")
def cli_sandbox(
    e2e_settings: E2ESettings,
    control_plane: LambdaMicrovmsControlPlane,
    template_arn: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[CliSandbox]:
    env = cli_env(e2e_settings)
    token_file = tmp_path_factory.mktemp("m9-cli") / "sandbox.token"
    code, stdout, stderr, elapsed = run_cli(
        [
            "--json",
            "sandbox",
            "create",
            template_arn,
            "--detach",
            "--token-file",
            str(token_file),
            "--timeout",
            str(TEST_SANDBOX_TIMEOUT_SECONDS),
        ],
        env,
    )
    assert code == 0, stderr
    sandbox_id = str(json.loads(stdout)["sandbox_id"])
    created = CliSandbox(sandbox_id=sandbox_id, token_file=token_file, env=env)
    assert_no_token(created, stdout, stderr)
    assert token_file.stat().st_size > 0
    if os.name == "posix":
        assert token_file.stat().st_mode & 0o777 == 0o600
    report(f"{sandbox_id}: sandbox create --detach", f"{elapsed:.2f} s")
    try:
        yield created
    finally:
        code, stdout, stderr, elapsed = run_cli(["sandbox", "kill", sandbox_id], env)
        report(f"sandbox kill {sandbox_id}", f"exit {code} en {elapsed:.2f} s")
        with contextlib.suppress(SandboxException):
            control_plane.terminate_microvm(sandbox_id)
        assert code == 0, stderr


def test_exec_hi_and_exit_code(cli_sandbox: CliSandbox) -> None:
    token_args = ["--token-file", str(cli_sandbox.token_file)]
    code, stdout, stderr, elapsed = run_cli(
        ["sandbox", "exec", cli_sandbox.sandbox_id, *token_args, "--", "echo", "hi"],
        cli_sandbox.env,
    )
    report("exec -- echo hi", f"exit {code} en {elapsed:.2f} s")
    assert (code, stdout) == (0, "hi\n"), stderr
    assert_no_token(cli_sandbox, stdout, stderr)

    code, stdout, stderr, elapsed = run_cli(
        ["sandbox", "exec", cli_sandbox.sandbox_id, *token_args, "--", "sh", "-c", "exit 3"],
        cli_sandbox.env,
    )
    report("exec -- sh -c 'exit 3'", f"exit {code} en {elapsed:.2f} s")
    assert code == 3, stderr
    assert_no_token(cli_sandbox, stdout, stderr)


def test_connect_in_pipe_mode(cli_sandbox: CliSandbox) -> None:
    code, stdout, stderr, elapsed = run_cli(
        ["sandbox", "connect", cli_sandbox.sandbox_id, "--token-file", str(cli_sandbox.token_file)],
        cli_sandbox.env,
        stdin=f"{MARKER_COMMAND}\nexit\n".encode(),
    )
    report("connect (tubería)", f"exit {code} en {elapsed:.2f} s")
    assert code == 0, stderr
    assert MARKER_OUTPUT in stdout, stdout[-2000:]
    assert_no_token(cli_sandbox, stdout, stderr)


def read_pty_until(
    fd: int, buffer: bytearray, done: Callable[[bytes], bool], budget: float
) -> bool:
    """Lee del maestro de la PTY hasta que `done(buffer)` (`True`) o el EOF
    del hijo (`False`); agotar el plazo es un fallo."""
    deadline = time.monotonic() + budget
    while not done(bytes(buffer)):
        remaining = deadline - time.monotonic()
        assert remaining > 0, f"la PTY no llegó a tiempo: {bytes(buffer[-2000:])!r}"
        ready, _, _ = select.select([fd], [], [], min(remaining, 1.0))
        if not ready:
            continue
        try:
            chunk = os.read(fd, READ_CHUNK_BYTES)
        except OSError:
            chunk = b""
        if not chunk:
            return False
        buffer.extend(chunk)
    return True


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="pty.fork() sólo existe en POSIX; la aceptación exige una corrida en Linux o WSL",
)
def test_connect_through_a_real_pty(cli_sandbox: CliSandbox) -> None:
    import pty

    args = [
        *CLI,
        "sandbox",
        "connect",
        cli_sandbox.sandbox_id,
        "--token-file",
        str(cli_sandbox.token_file),
    ]
    started = time.perf_counter()
    pid, fd = pty.fork()
    if pid == 0:
        try:
            os.execve(sys.executable, args, dict(cli_sandbox.env))
        finally:
            os._exit(127)
    buffer = bytearray()
    status: int | None = None
    try:
        prompted = read_pty_until(
            fd, buffer, lambda data: PROMPT.search(data) is not None, PTY_BUDGET_SECONDS
        )
        assert prompted, f"connect terminó sin prompt: {bytes(buffer[-2000:])!r}"
        report("connect (pty): prompt", f"{time.perf_counter() - started:.2f} s")
        os.write(fd, f"{MARKER_COMMAND}\r".encode())
        echoed = read_pty_until(
            fd,
            buffer,
            lambda data: MARKER_COMMAND.encode() in data and MARKER_OUTPUT.encode() in data,
            PTY_BUDGET_SECONDS,
        )
        assert echoed, f"sin eco o sin salida: {bytes(buffer[-2000:])!r}"
        os.write(fd, b"exit\r")
        read_pty_until(fd, buffer, lambda data: False, PTY_EXIT_BUDGET_SECONDS)
        _, status = os.waitpid(pid, 0)
    finally:
        if status is None:
            with contextlib.suppress(OSError):
                os.kill(pid, 9)
            with contextlib.suppress(ChildProcessError):
                os.waitpid(pid, 0)
        os.close(fd)
    output = buffer.decode("utf-8", errors="replace")
    assert_no_token(cli_sandbox, output)
    assert status is not None
    assert os.waitstatus_to_exitcode(status) == 0, output[-2000:]
    report("connect (pty): eco, salida y exit", f"{time.perf_counter() - started:.2f} s")


def test_metrics_json(cli_sandbox: CliSandbox) -> None:
    code, stdout, stderr, elapsed = run_cli(
        [
            "--json",
            "sandbox",
            "metrics",
            cli_sandbox.sandbox_id,
            "--token-file",
            str(cli_sandbox.token_file),
        ],
        cli_sandbox.env,
    )
    report("--json sandbox metrics", f"exit {code} en {elapsed:.2f} s")
    assert code == 0, stderr
    assert_no_token(cli_sandbox, stdout, stderr)
    document = json.loads(stdout)
    assert document["cpu_count"] >= 1, document
    assert document["mem_total"] > 0, document
