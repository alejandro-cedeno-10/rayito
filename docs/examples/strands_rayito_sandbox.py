"""Un sandbox de Rayito como `Sandbox` de Strands, en menos de cincuenta líneas.

Instalación: `pip install rayito strands-agents`. Entorno: `RAYITO_TEMPLATE`,
`AWS_REGION` y las credenciales de boto3. Con este adaptador las herramientas
de shell, ficheros y código de Strands corren dentro del MicroVM; el sandbox
se crea fuera, con `AsyncSandbox.create()`, y lo destruyes tú con `kill()`.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

from rayito import AsyncSandbox, CommandExitException, TimeoutException
from strands.sandbox import ExecutionResult, PosixShellSandbox, StreamChunk
from strands.sandbox.errors import SandboxTimeoutError


class RayitoSandbox(PosixShellSandbox):
    """Ejecuta cada comando con `sandbox.commands.run`; el resto de
    operaciones (ficheros, código) las deriva `PosixShellSandbox`."""

    def __init__(self, sandbox: AsyncSandbox) -> None:
        self._sandbox = sandbox

    async def execute_streaming(
        self,
        command: str,
        *,
        timeout: float | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[StreamChunk | ExecutionResult, None]:
        try:
            done = await self._sandbox.commands.run(command, cwd=cwd, envs=env, timeout=timeout)
            exit_code, stdout, stderr = done.exit_code, done.stdout, done.stderr
        except CommandExitException as failed:
            exit_code, stdout, stderr = failed.exit_code, failed.stdout, failed.stderr
        except TimeoutException as expired:
            raise SandboxTimeoutError(timeout) from expired
        if stdout:
            yield StreamChunk(stdout, "stdout")
        if stderr:
            yield StreamChunk(stderr, "stderr")
        yield ExecutionResult(exit_code=exit_code, stdout=stdout, stderr=stderr)
