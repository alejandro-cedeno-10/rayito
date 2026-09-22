"""`build_server`: el `MCPServer` de Rayito con sus seis herramientas
(design D2, D5, D7, D10, D11).

Cada herramienta es `async def` sobre el `AsyncSandbox` del `SandboxLease`
que cede el lifespan; devuelve un `TypedDict` (salida estructurada) o, en
`run_code`, bloques de contenido. Los fallos accionables por el modelo son
`ToolError`; los exit codes y los errores del kernel son datos. El log lleva
nombres de herramienta, tamaños, duraciones, `sandbox_id`, exit codes y
nombres de excepción: nunca código, comandos, contenidos ni salidas.
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import AsyncIterator
from typing import Annotated, Any

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from rayito._aws import ControlPlane
from rayito._transport import TransportSettings
from rayito._version import __version__
from rayito.exceptions import (
    CommandExitException,
    DiskFullException,
    FileNotFoundException,
    InvalidArgumentException,
    RateLimitException,
    SandboxNotFoundException,
    SandboxStateException,
    TimeoutException,
)
from rayito.mcp._lease import MissingTemplateError, SandboxCreationError, SandboxLease
from rayito.mcp._results import (
    CommandOutput,
    ContentBlock,
    DirectoryListing,
    FileContent,
    SandboxList,
    WriteReceipt,
    command_output_from,
    directory_listing_from,
    execution_to_blocks,
    file_content_from,
    sandbox_list_from,
    write_receipt_from,
)
from rayito.mcp._settings import McpSettings
from rayito.sandbox_async.main import AsyncSandbox

logger = logging.getLogger("rayito.mcp")

SERVER_NAME = "rayito"
DEFAULT_CODE_TIMEOUT_SECONDS = 300
DEFAULT_COMMAND_TIMEOUT_SECONDS = 60
MAX_TOOL_TIMEOUT_SECONDS = 3600
DEFAULT_LIST_PATH = "/home/user"
MAX_LIST_DEPTH = 5

SERVER_INSTRUCTIONS = (
    "Todas las herramientas comparten un único sandbox Linux (usuario uid 1000, "
    "directorio /home/user) con un kernel Python que conserva el estado entre "
    "llamadas a run_code; hay salida a internet salvo que la imagen lo impida. "
    "El sandbox aparece en la primera llamada que lo necesita y se destruye cuando "
    "el servidor termina; si pasa un rato sin uso AWS lo suspende y la siguiente "
    "llamada lo reanuda sola. Un exit_code distinto de cero en run_command o un "
    "error del kernel en run_code son datos para leer, no fallos que reintentar "
    "a ciegas."
)

PAYLOAD_LOGGERS = ("botocore", "boto3", "urllib3")
PAYLOAD_LOGGERS_LEVEL = logging.WARNING

SANDBOX_GONE_EXCEPTIONS = (SandboxNotFoundException,)
SDK_MESSAGE_EXCEPTIONS = (
    FileNotFoundException,
    InvalidArgumentException,
    DiskFullException,
    RateLimitException,
)

CodeArg = Annotated[str, Field(description="Código Python que ejecutar en el kernel del sandbox.")]
CodeTimeoutArg = Annotated[
    int,
    Field(
        ge=1,
        le=MAX_TOOL_TIMEOUT_SECONDS,
        description="Segundos de reloj que el agente concede a la celda antes de interrumpirla.",
    ),
]
CommandArg = Annotated[
    str, Field(description="Comando que ejecuta /bin/sh -c como el usuario del sandbox.")
]
CommandTimeoutArg = Annotated[
    int,
    Field(
        ge=1,
        le=MAX_TOOL_TIMEOUT_SECONDS,
        description="Segundos de reloj antes de que el agente mate el comando.",
    ),
]
CwdArg = Annotated[str | None, Field(description="Directorio de trabajo; por defecto /home/user.")]
PathArg = Annotated[str, Field(description="Ruta absoluta o relativa a /home/user.")]
ContentArg = Annotated[str, Field(description="Contenido del fichero en UTF-8.")]
ListPathArg = Annotated[str, Field(description="Directorio que listar.")]
DepthArg = Annotated[
    int, Field(ge=1, le=MAX_LIST_DEPTH, description="Niveles de subdirectorios que incluir.")
]


def build_server(
    settings: McpSettings,
    *,
    control_plane: ControlPlane | None = None,
    transport: TransportSettings | None = None,
) -> MCPServer[SandboxLease]:
    """El servidor `rayito` con el lifespan que posee el `SandboxLease` y las
    seis herramientas; `control_plane`/`transport` sólo los inyectan los
    tests (fake `rayd` + Stubber)."""

    @contextlib.asynccontextmanager
    async def lease_lifespan(server: MCPServer[SandboxLease]) -> AsyncIterator[SandboxLease]:
        lease = SandboxLease(settings, control_plane=control_plane, transport=transport)
        logger.debug("servidor MCP arrancando: %s", settings)
        try:
            yield lease
        finally:
            await lease.close()

    server: MCPServer[SandboxLease] = MCPServer(
        SERVER_NAME,
        instructions=SERVER_INSTRUCTIONS,
        version=__version__,
        lifespan=lease_lifespan,
        log_level=settings.log_level,
    )
    silence_payload_loggers()
    register_tools(server, settings, control_plane=control_plane, transport=transport)
    return server


def silence_payload_loggers() -> None:
    """`MCPServer(log_level=)` configura el logger raíz, y botocore en DEBUG
    vuelca cuerpos de request y response: el `runHookPayload` (envs) y el JWE
    del proxy que devuelve `create-microvm-auth-token`. Esos loggers quedan
    en WARNING sea cual sea `RAYITO_MCP_LOG_LEVEL`."""
    for name in PAYLOAD_LOGGERS:
        logging.getLogger(name).setLevel(PAYLOAD_LOGGERS_LEVEL)


def register_tools(
    server: MCPServer[SandboxLease],
    settings: McpSettings,
    *,
    control_plane: ControlPlane | None,
    transport: TransportSettings | None,
) -> None:
    @server.tool(
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=False, open_world_hint=True
        )
    )
    async def run_code(
        ctx: Context[SandboxLease],
        code: CodeArg,
        timeout: CodeTimeoutArg = DEFAULT_CODE_TIMEOUT_SECONDS,
    ) -> list[ContentBlock]:
        """Ejecuta código Python en el kernel con estado del sandbox (las
        variables persisten entre llamadas). Devuelve un bloque JSON con
        `text`, `stdout`, `stderr`, `error`, `execution_count` y los mime
        types de cada resultado, seguido de las imágenes PNG/JPEG y los SVG
        que produjo la celda. Un error del kernel llega en `error`."""
        lease = lease_of(ctx)
        call = CallLog("run_code", code_chars=len(code))
        async with translated_errors(lease, timeout=timeout):
            sandbox = await lease.acquire()
            execution = await sandbox.run_code(code, timeout=timeout)
        call.finish(
            sandbox.sandbox_id,
            results=len(execution.results),
            error=None if execution.error is None else execution.error.name,
        )
        return execution_to_blocks(execution, sandbox_id=sandbox.sandbox_id)

    @server.tool(
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=False, open_world_hint=True
        )
    )
    async def run_command(
        ctx: Context[SandboxLease],
        cmd: CommandArg,
        timeout: CommandTimeoutArg = DEFAULT_COMMAND_TIMEOUT_SECONDS,
        cwd: CwdArg = None,
    ) -> CommandOutput:
        """Ejecuta un comando de shell en el sandbox y espera a que termine.
        Devuelve `stdout`, `stderr` y `exit_code`; un exit code distinto de
        cero es información, no un error."""
        lease = lease_of(ctx)
        call = CallLog("run_command", cmd_chars=len(cmd))
        async with translated_errors(lease, timeout=timeout):
            sandbox = await lease.acquire()
            output = await run_command_in(sandbox, cmd, timeout=timeout, cwd=cwd)
        call.finish(sandbox.sandbox_id, exit_code=output["exit_code"])
        return output

    @server.tool(annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True))
    async def read_file(ctx: Context[SandboxLease], path: PathArg) -> FileContent:
        """Lee un fichero de texto UTF-8 del sandbox. Devuelve `content`,
        `size` y `truncated` (la salida se corta a 100 000 caracteres)."""
        lease = lease_of(ctx)
        call = CallLog("read_file", path_chars=len(path))
        async with translated_errors(lease):
            sandbox = await lease.acquire()
            content = await sandbox.files.read(path)
        call.finish(sandbox.sandbox_id, size=len(content))
        return file_content_from(path, content)

    @server.tool(
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=True, idempotent_hint=True
        )
    )
    async def write_file(
        ctx: Context[SandboxLease], path: PathArg, content: ContentArg
    ) -> WriteReceipt:
        """Escribe un fichero de texto UTF-8 en el sandbox, creando los
        directorios intermedios y sobrescribiendo si ya existe. Devuelve
        `path` y `size`."""
        lease = lease_of(ctx)
        call = CallLog("write_file", path_chars=len(path), content_chars=len(content))
        async with translated_errors(lease):
            sandbox = await lease.acquire()
            entry = await sandbox.files.write(path, content)
        call.finish(sandbox.sandbox_id, size=entry.size)
        return write_receipt_from(entry)

    @server.tool(annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True))
    async def list_files(
        ctx: Context[SandboxLease],
        path: ListPathArg = DEFAULT_LIST_PATH,
        depth: DepthArg = 1,
    ) -> DirectoryListing:
        """Lista un directorio del sandbox hasta `depth` niveles. Cada entrada
        trae `name`, `path`, `type` (`file`, `dir`, `symlink` o null), `size`
        y `modified_time` (ISO 8601)."""
        lease = lease_of(ctx)
        call = CallLog("list_files", path_chars=len(path), depth=depth)
        async with translated_errors(lease):
            sandbox = await lease.acquire()
            entries = await sandbox.files.list(path, depth=depth)
        call.finish(sandbox.sandbox_id, entries=len(entries))
        return directory_listing_from(path, entries)

    @server.tool(annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True))
    async def list_sandboxes(ctx: Context[SandboxLease]) -> SandboxList:
        """Lista los sandboxes vivos de la imagen configurada en
        `RAYITO_TEMPLATE` (cualquier estado salvo terminado); `current` marca
        el de este servidor. Nunca crea un sandbox."""
        lease = lease_of(ctx)
        call = CallLog("list_sandboxes")
        async with translated_errors(lease):
            template = lease.require_template()
            current = await lease.peek()
            items = await AsyncSandbox.list(
                template=template, control_plane=control_plane, transport=transport
            )
        call.finish(lease.sandbox_id, listed=len(items))
        return sandbox_list_from(
            items, current_sandbox_id=None if current is None else current.sandbox_id
        )


def lease_of(ctx: Context[SandboxLease]) -> SandboxLease:
    return ctx.request_context.lifespan_context


async def run_command_in(
    sandbox: AsyncSandbox, cmd: str, *, timeout: int, cwd: str | None
) -> CommandOutput:
    try:
        result = await sandbox.commands.run(cmd, timeout=timeout, cwd=cwd)
    except CommandExitException as exc:
        return command_output_from(exc)
    return command_output_from(result)


@contextlib.asynccontextmanager
async def translated_errors(
    lease: SandboxLease, *, timeout: int | None = None
) -> AsyncIterator[None]:
    """La tabla de design D7: lo que el modelo puede corregir es `ToolError`
    con mensaje en español; sólo `SandboxNotFoundException` (la única señal
    terminal del SDK) resetea el lease; `SandboxStateException` es transitoria
    (suspend/resume en curso, `ConflictException`) y se pide reintentar sin
    tocar el sandbox; el resto propaga y el SDK lo convierte en un error
    genérico con traza en el servidor."""
    try:
        yield
    except (MissingTemplateError, SandboxCreationError) as exc:
        raise ToolError(str(exc)) from exc
    except TimeoutException as exc:
        raise ToolError(timeout_message(timeout)) from exc
    except SDK_MESSAGE_EXCEPTIONS as exc:
        raise ToolError(str(exc)) from exc
    except SandboxStateException as exc:
        logger.info("el sandbox %s está en transición: %s", lease.sandbox_id, type(exc).__name__)
        raise ToolError(transition_message(exc)) from exc
    except SANDBOX_GONE_EXCEPTIONS as exc:
        sandbox_id = lease.sandbox_id
        logger.warning("el sandbox %s ya no existe: %s", sandbox_id, type(exc).__name__)
        await lease.reset()
        raise ToolError(
            f"el sandbox {sandbox_id} ya no existe ({exc}); la siguiente llamada crea uno nuevo"
        ) from exc


def transition_message(exc: SandboxStateException) -> str:
    return f"el sandbox está en transición ({exc}); reintenta en unos segundos"


def timeout_message(timeout: int | None) -> str:
    if timeout is None:
        return "la operación superó el timeout del agente"
    return f"el comando superó el timeout de {timeout} s"


class CallLog:
    """Una línea de log por llamada: nombre, tamaños de argumentos, duración,
    `sandbox_id` y el desenlace (exit code, nombre del error, conteos)."""

    def __init__(self, tool: str, **sizes: int) -> None:
        self._tool = tool
        self._started = time.perf_counter()
        logger.debug("%s: inicio %s", tool, format_fields(sizes))

    def finish(self, sandbox_id: str | None, **outcome: Any) -> None:
        elapsed = time.perf_counter() - self._started
        logger.info(
            "%s: %.3f s sandbox=%s %s", self._tool, elapsed, sandbox_id, format_fields(outcome)
        )


def format_fields(fields: dict[str, Any]) -> str:
    return " ".join(f"{key}={value}" for key, value in fields.items())
