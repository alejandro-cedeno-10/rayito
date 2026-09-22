"""`SandboxLease`: el único sandbox de un proceso servidor (design D3).

Se crea perezosamente en la primera herramienta que lo necesita bajo un
`asyncio.Lock` (dos primeras llamadas concurrentes crean un solo MicroVM), la
auto-suspensión la hace AWS con el `idlePolicy` del sandbox (ningún timer
aquí) y `close()` lo mata cuando el servidor termina. Importa sólo `rayito`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field

from botocore.exceptions import BotoCoreError

from rayito._aws import ControlPlane
from rayito._sandbox_base import TEMPLATE_ENV_VAR
from rayito._transport import TransportSettings
from rayito.exceptions import (
    AuthenticationException,
    CapacityException,
    QuotaExceededException,
    SandboxException,
    SandboxNotFoundException,
)
from rayito.mcp._settings import McpSettings
from rayito.sandbox_async.main import AsyncSandbox

logger = logging.getLogger("rayito.mcp")

CREATION_FAILURE_PREFIX = "no se pudo crear el sandbox: "
CreationFailure = (
    SandboxException,
    AuthenticationException,
    CapacityException,
    QuotaExceededException,
    BotoCoreError,
)


class MissingTemplateError(RuntimeError):
    """`RAYITO_TEMPLATE` no está definido y una herramienta lo necesita."""

    def __init__(self) -> None:
        super().__init__(
            f"{TEMPLATE_ENV_VAR} no está definido: configura la imagen (nombre o ARN) "
            "en el entorno del servidor"
        )


class SandboxCreationError(RuntimeError):
    """`AsyncSandbox.create` falló; `__cause__` es la excepción del SDK y el
    lease queda vacío para que la siguiente llamada reintente."""

    def __init__(self, cause: BaseException) -> None:
        super().__init__(f"{CREATION_FAILURE_PREFIX}{cause}")


@dataclass
class SandboxLease:
    settings: McpSettings
    control_plane: ControlPlane | None = None
    transport: TransportSettings | None = None
    _sandbox: AsyncSandbox | None = field(default=None, init=False, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    @property
    def sandbox_id(self) -> str | None:
        return None if self._sandbox is None else self._sandbox.sandbox_id

    def require_template(self) -> str:
        if self.settings.template is None:
            raise MissingTemplateError()
        return self.settings.template

    async def acquire(self) -> AsyncSandbox:
        """El sandbox del proceso, creándolo si aún no existe."""
        async with self._lock:
            if self._sandbox is None:
                self._sandbox = await self._create()
            return self._sandbox

    async def peek(self) -> AsyncSandbox | None:
        """El sandbox si existe; nunca crea uno."""
        return self._sandbox

    async def reset(self) -> None:
        """Olvida un sandbox que el SDK reportó como terminal
        (`SandboxNotFoundException`: expiró o lo mataron fuera). El MicroVM ya
        no existe, así que sólo se liberan los canales; la siguiente
        `acquire()` crea otro."""
        async with self._lock:
            stale = self._sandbox
            self._sandbox = None
        if stale is None:
            return
        await stale.close()

    async def close(self) -> None:
        """`kill()` + `close()` una sola vez; nada si nunca hubo sandbox."""
        async with self._lock:
            sandbox = self._sandbox
            self._sandbox = None
        if sandbox is None:
            return
        logger.info("terminando el sandbox %s", sandbox.sandbox_id)
        try:
            with contextlib.suppress(SandboxNotFoundException):
                await sandbox.kill()
        finally:
            await sandbox.close()

    async def _create(self) -> AsyncSandbox:
        template = self.require_template()
        logger.info("creando el sandbox (template %s)", template)
        try:
            sandbox = await AsyncSandbox.create(
                template,
                template_version=self.settings.template_version,
                timeout=self.settings.timeout_seconds,
                idle=self.settings.idle_policy,
                execution_role_arn=self.settings.execution_role_arn,
                ingress=["ALL_INGRESS"],
                logging=self.settings.logging,
                control_plane=self.control_plane,
                transport=self.transport,
            )
        except CreationFailure as exc:
            logger.warning("no se pudo crear el sandbox: %s", type(exc).__name__)
            raise SandboxCreationError(exc) from exc
        logger.info("sandbox %s listo", sandbox.sandbox_id)
        return sandbox
