"""El ciclo de vida y los subclientes del SDK contra el `rayd` real del guest
local (`make local-e2e`): `create()` por el plano de control local, comandos,
ficheros, PTY, `run_code`, `connect`, métricas, pausa y reanudación por los
hooks y `kill`. Lo que depende del proxy de AWS (el JWE, el 403 de un token
falso, los keepalives a través del proxy) solo lo cubren los e2e contra AWS
real (`tests/e2e`)."""

from __future__ import annotations

import asyncio
import contextlib
import secrets as stdlib_secrets
from collections.abc import Iterator
from typing import Final

import pytest

from rayito import AsyncSandbox, CommandExitException, PtySize, Sandbox
from rayito._limits import TERMINAL_STATES
from rayito.exceptions import SandboxNotFoundException

from .conftest import LocalSettings, create_local_sandbox
from .guest import LocalGuestControlPlane

pytestmark = pytest.mark.local

SANDBOX_USER: Final = "user"
PTY_MARKER: Final = "rayito-local-pty"
PTY_READ_BUDGET_SECONDS: Final = 20.0


@pytest.fixture(scope="module")
def shared(
    local_settings: LocalSettings, control_plane: LocalGuestControlPlane, template_arn: str
) -> Iterator[Sandbox]:
    """Un sandbox para todo el módulo: cada `create()` local rearranca el
    guest y calienta el kernel de nuevo."""
    created = create_local_sandbox(
        local_settings, control_plane, template_arn, metadata={"suite": "local"}
    )
    try:
        yield created
    finally:
        with contextlib.suppress(SandboxNotFoundException):
            created.kill()


def test_health_info_and_metadata(shared: Sandbox) -> None:
    health = shared.get_health()
    assert health.agent_ready is True
    assert health.kernel_ready is True
    assert shared.is_running() is True
    info = shared.get_info()
    assert info.sandbox_id == shared.sandbox_id
    assert info.state == "RUNNING"
    assert info.metadata == {"suite": "local"}
    assert shared.metadata == {"suite": "local"}


def test_commands_run_as_the_sandbox_user(shared: Sandbox) -> None:
    result = shared.commands.run("echo hola && whoami")
    assert result.stdout.split() == ["hola", SANDBOX_USER]
    assert result.exit_code == 0
    with_env = shared.commands.run("echo $RAYITO_LOCAL", envs={"RAYITO_LOCAL": "sí"}, cwd="/tmp")
    assert with_env.stdout.strip() == "sí"
    with pytest.raises(CommandExitException) as failed:
        shared.commands.run("exit 3")
    assert failed.value.exit_code == 3


def test_background_command_can_be_listed_and_killed(shared: Sandbox) -> None:
    handle = shared.commands.run("sleep 60", background=True)
    assert handle.pid in {process.pid for process in shared.commands.list()}
    assert handle.kill() is True
    with pytest.raises(CommandExitException):
        handle.wait()


def test_files_round_trip(shared: Sandbox) -> None:
    base = f"/home/{SANDBOX_USER}/local-{stdlib_secrets.token_hex(4)}"
    assert shared.files.make_dir(base) is True
    payload = stdlib_secrets.token_bytes(256 * 1024)
    shared.files.write(f"{base}/blob.bin", payload)
    shared.files.write(f"{base}/note.txt", "hola desde local")
    assert shared.files.read(f"{base}/blob.bin", format="bytes") == payload
    assert shared.files.read(f"{base}/note.txt") == "hola desde local"
    assert sorted(entry.name for entry in shared.files.list(base)) == ["blob.bin", "note.txt"]
    shared.files.rename(f"{base}/note.txt", f"{base}/renamed.txt")
    assert shared.files.exists(f"{base}/note.txt") is False
    owner = shared.commands.run(f"stat -c %U {base}/renamed.txt").stdout.strip()
    assert owner == SANDBOX_USER
    shared.files.remove(base)
    assert shared.files.exists(base) is False


def test_pty_echoes_and_resizes(shared: Sandbox) -> None:
    pty = shared.pty.create(size=PtySize(cols=100, rows=30), timeout=None)
    try:
        pty.send_input(f"echo {PTY_MARKER}\n")
        seen = b""
        for _, _, data in pty:
            seen += data or b""
            if f"{PTY_MARKER}\r\n".encode() in seen:
                break
        pty.resize(PtySize(cols=80, rows=24))
    finally:
        assert pty.kill() is True


def test_run_code_keeps_state_and_reports_errors(shared: Sandbox) -> None:
    assert shared.run_code("x = 41").error is None
    assert shared.run_code("x + 1").text == "42"
    failed = shared.run_code("1 / 0")
    assert failed.error is not None
    assert failed.error.name == "ZeroDivisionError"
    assert shared.run_code("import pandas as pd; len(pd.DataFrame({'a': [1, 2]}))").text == "2"
    context = shared.create_code_context(language="python")
    assert shared.run_code("x = 'aislado'; x", context=context).text == "'aislado'"
    assert shared.run_code("x").text == "41"
    shared.remove_code_context(context)


def test_connect_reuses_the_running_sandbox(
    shared: Sandbox, local_settings: LocalSettings, control_plane: LocalGuestControlPlane
) -> None:
    reconnected = Sandbox.connect(
        shared.sandbox_id,
        access_token=shared.access_token,
        control_plane=control_plane,
        transport=local_settings.transport,
    )
    try:
        assert reconnected.commands.run("echo de-nuevo").stdout.strip() == "de-nuevo"
    finally:
        reconnected.close()


def test_metrics(shared: Sandbox) -> None:
    metrics = shared.get_metrics()
    assert metrics.cpu_count >= 1
    assert metrics.mem_total_bytes > 0


def test_async_sandbox_parity(
    shared: Sandbox, local_settings: LocalSettings, control_plane: LocalGuestControlPlane
) -> None:
    async def run() -> str:
        sandbox = await AsyncSandbox.connect(
            shared.sandbox_id,
            access_token=shared.access_token,
            control_plane=control_plane,
            transport=local_settings.transport,
        )
        try:
            result = await sandbox.commands.run("echo async")
            return result.stdout.strip()
        finally:
            await sandbox.close()

    assert asyncio.run(run()) == "async"


def test_pause_resume_and_kill(
    local_settings: LocalSettings, control_plane: LocalGuestControlPlane, template_arn: str
) -> None:
    """Va al final: termina el sandbox del módulo (el guest aloja uno solo)."""
    sandbox = create_local_sandbox(local_settings, control_plane, template_arn, idle=None)
    try:
        sandbox.files.write("/tmp/antes-de-pausar", "sigue aquí")
        assert sandbox.pause() is True
        assert Sandbox.get_info(sandbox.sandbox_id, control_plane=control_plane).state == (
            "SUSPENDED"
        )
        sandbox.resume()
        assert sandbox.files.read("/tmp/antes-de-pausar") == "sigue aquí"
        assert sandbox.resume_generation >= 1
    finally:
        assert sandbox.kill() is True
    final = Sandbox.get_info(sandbox.sandbox_id, read_metadata=False, control_plane=control_plane)
    assert final.state in TERMINAL_STATES
