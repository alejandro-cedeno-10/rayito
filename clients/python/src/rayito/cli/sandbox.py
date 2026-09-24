"""`rayito sandbox list | info | kill | logs` sobre el plano de control del
SDK, y `create | connect | exec | metrics` sobre el agente del sandbox.

Los cuatro últimos usan el access token del sandbox, que llega sólo por
`--token-file` o `RAYITO_ACCESS_TOKEN` (nunca por argv) y nunca se imprime.
Códigos de salida: el del comando o del shell remoto en `exec`, `connect` y
`create` sin `--detach` (124 si el agente lo cortó por timeout); 1 si la
operación falla; 2 para uso o entorno (sin token, par `K=V` mal formado,
fichero de token existente).
"""

from __future__ import annotations

import dataclasses
import json
import os
import shlex
import sys
import time
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer

from rayito._limits import MICROVM_STATES, TERMINAL_STATES
from rayito._models import SandboxInfo, SandboxListItem, SandboxMetrics
from rayito._payload import generate_access_token
from rayito.cli._console import EXIT_USAGE, age, echo, emit_json, fail, iso_utc, table
from rayito.cli._logs import (
    DEFAULT_EVENT_LIMIT,
    LogsNotFound,
    default_log_group,
    epoch_ms,
    event_timestamp,
    find_streams,
    iter_events,
    parse_since,
)
from rayito.cli._session import Clients, clients_of, json_mode
from rayito.cli._terminal import EXIT_TIMEOUT, run_terminal
from rayito.cli._tokens import (
    TOKEN_ENV_VAR,
    InvalidPairError,
    TokenFileError,
    parse_pairs,
    resolve_token,
    write_token_file,
)
from rayito.exceptions import (
    CommandExitException,
    SandboxException,
    SandboxNotFoundException,
    TimeoutException,
)
from rayito.sandbox_sync.main import Sandbox

sandbox_app = typer.Typer(
    no_args_is_help=True,
    help="MicroVMs: list, info, kill, logs, create, connect, exec, metrics.",
)

LIST_COLUMNS = ("sandbox_id", "state", "template", "template_version", "started_at", "age")


def list_row(item: SandboxListItem) -> dict[str, Any]:
    return {
        "sandbox_id": item.sandbox_id,
        "state": item.state,
        "template": item.template_name,
        "template_arn": item.template,
        "template_version": item.template_version,
        "started_at": iso_utc(item.started_at),
        "age": age(item.started_at),
    }


def list_sandboxes(
    clients: Clients, template: str | None, template_version: str | None, all_states: bool
) -> list[SandboxListItem]:
    return list(
        Sandbox.list(
            template=template,
            template_version=template_version,
            states=MICROVM_STATES if all_states else None,
            control_plane=clients.control_plane,
        )
    )


@sandbox_app.command("list")
def list_command(
    ctx: typer.Context,
    template: Annotated[
        str | None, typer.Option("--template", help="Nombre o ARN de la imagen.")
    ] = None,
    template_version: Annotated[str | None, typer.Option("--template-version")] = None,
    all_states: Annotated[
        bool, typer.Option("--all-states", help="Incluye TERMINATING y TERMINATED.")
    ] = False,
) -> None:
    """MicroVMs no terminados (sin sondear ningún endpoint)."""
    clients = clients_of(ctx)
    rows = [
        list_row(item) for item in list_sandboxes(clients, template, template_version, all_states)
    ]
    if json_mode(ctx):
        emit_json(rows)
        return
    table(LIST_COLUMNS, [[row[column] for column in LIST_COLUMNS] for row in rows])


def info_document(info: SandboxInfo) -> dict[str, Any]:
    document = dataclasses.asdict(info)
    document["template_name"] = info.template_name
    document["endpoint_url"] = info.endpoint_url
    document["started_at"] = iso_utc(info.started_at)
    document["terminated_at"] = iso_utc(info.terminated_at)
    document["expires_at"] = iso_utc(info.expires_at)
    return document


@sandbox_app.command("info")
def info_command(
    ctx: typer.Context,
    sandbox_id: Annotated[str, typer.Argument(metavar="ID")],
    no_metadata: Annotated[
        bool, typer.Option("--no-metadata", help="No sondea Health para leer los metadatos.")
    ] = False,
) -> None:
    """`get-microvm` y, sobre un sandbox RUNNING, sus metadatos vía Health."""
    clients = clients_of(ctx)
    info = Sandbox.get_info(
        sandbox_id,
        read_metadata=not no_metadata,
        control_plane=clients.control_plane,
        transport=clients.transport,
    )
    document = info_document(info)
    if json_mode(ctx):
        emit_json(document)
        return
    for key, value in document.items():
        echo(f"{key}: {value}")


def kill_one(clients: Clients, sandbox_id: str) -> bool:
    try:
        return bool(Sandbox.kill(sandbox_id, control_plane=clients.control_plane))
    except SandboxNotFoundException:
        return False


@sandbox_app.command("kill")
def kill_command(
    ctx: typer.Context,
    sandbox_ids: Annotated[list[str] | None, typer.Argument(metavar="[ID]...")] = None,
    all_sandboxes: Annotated[
        bool, typer.Option("--all", help="Termina todos los MicroVMs no terminados.")
    ] = False,
    template: Annotated[
        str | None, typer.Option("--template", help="Con --all: sólo esta imagen.")
    ] = None,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Con --all: no pide confirmación.")
    ] = False,
) -> None:
    """`terminate-microvm` por id, o de todos los vivos con --all."""
    ids = list(sandbox_ids or [])
    if all_sandboxes and ids:
        raise typer.BadParameter("--all y los IDs son excluyentes", param_hint="--all")
    if not all_sandboxes and not ids:
        raise typer.BadParameter("pasa uno o más IDs, o --all", param_hint="ID")
    clients = clients_of(ctx)
    if all_sandboxes:
        ids = [item.sandbox_id for item in list_sandboxes(clients, template, None, False)]
        if not ids:
            report_kills(ctx, [])
            return
        for sandbox_id in ids:
            echo(sandbox_id, err=json_mode(ctx))
        if not yes and not typer.confirm(f"¿Terminar {len(ids)} MicroVMs?"):
            raise typer.Abort()
    outcomes = [(sandbox_id, kill_one(clients, sandbox_id)) for sandbox_id in ids]
    report_kills(ctx, outcomes)
    if not all_sandboxes and not all(found for _, found in outcomes):
        raise typer.Exit(1)


def report_kills(ctx: typer.Context, outcomes: list[tuple[str, bool]]) -> None:
    if json_mode(ctx):
        emit_json(
            [
                {"sandbox_id": sandbox_id, "outcome": "terminated" if found else "not found"}
                for sandbox_id, found in outcomes
            ]
        )
        return
    for sandbox_id, found in outcomes:
        echo(f"{sandbox_id} {'terminated' if found else 'not found'}")


@sandbox_app.command("logs")
def logs_command(
    ctx: typer.Context,
    sandbox_id: Annotated[str, typer.Argument(metavar="ID")],
    log_group: Annotated[
        str | None, typer.Option("--log-group", help="Por defecto /rayito/<nombre de la imagen>.")
    ] = None,
    limit: Annotated[int, typer.Option("--limit", help="Máximo de eventos.")] = DEFAULT_EVENT_LIMIT,
    since: Annotated[
        str | None, typer.Option("--since", help="ISO-8601 o relativo: 30m, 2h, 1d.")
    ] = None,
) -> None:
    """Eventos del stream de CloudWatch del sandbox (sólo con executionRoleArn)."""
    clients = clients_of(ctx)
    info = Sandbox.get_info(sandbox_id, read_metadata=False, control_plane=clients.control_plane)
    group = log_group or default_log_group(info.template)
    try:
        start_time_ms = None if since is None else epoch_ms(parse_since(since))
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--since") from exc
    try:
        streams = find_streams(clients.logs, group, info)
    except LogsNotFound as exc:
        echo(f"rayito: {exc}", err=True)
        raise typer.Exit(1) from exc
    events = collect_events(clients, group, streams, start_time_ms, limit)
    if json_mode(ctx):
        emit_json(events)
        return
    for event in events:
        echo(f"{event['timestamp']} {event['message']}")


def collect_events(
    clients: Clients, group: str, streams: list[str], start_time_ms: int | None, limit: int
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for stream in streams:
        remaining = limit - len(events)
        if remaining <= 0:
            break
        events.extend(
            {
                "timestamp": event_timestamp(event),
                "message": str(event["message"]).rstrip("\n"),
                "stream": stream,
            }
            for event in iter_events(
                clients.logs, group, stream, start_time_ms=start_time_ms, limit=remaining
            )
        )
    return events


DEFAULT_CREATE_TIMEOUT_SECONDS = 3600
DEFAULT_METRICS_INTERVAL_SECONDS = 5.0
MISSING_TOKEN_MESSAGE = f"falta el access token: --token-file o {TOKEN_ENV_VAR}"
DETACH_NEEDS_TOKEN_FILE_MESSAGE = (
    f"sin {TOKEN_ENV_VAR}, --detach necesita --token-file para poder volver a conectarte"
)
COMMAND_TIMEOUT_MESSAGE = "timeout del comando"
SANDBOX_GONE_MESSAGE = "el sandbox ya no existe"
METRICS_COLUMNS = (
    "timestamp",
    "cpu_used_pct",
    "cpu_count",
    "mem_used",
    "mem_total",
    "disk_used",
    "disk_total",
)

TokenFileOption = Annotated[
    Path | None,
    typer.Option(
        "--token-file",
        help=f"Fichero con el access token (gana a {TOKEN_ENV_VAR}); nunca por argv.",
    ),
]
EnvOption = Annotated[list[str] | None, typer.Option("--env", "-e", help="K=V; repetible.")]
UserOption = Annotated[str | None, typer.Option("--user", "-u", help="Usuario del sandbox.")]
CwdOption = Annotated[str | None, typer.Option("--cwd", "-c", help="Directorio de trabajo.")]


def usage_failure(message: str) -> NoReturn:
    fail(message, code=EXIT_USAGE)


def pairs_or_exit(values: list[str] | None, option: str) -> dict[str, str]:
    try:
        return parse_pairs(values, option=option)
    except InvalidPairError as exc:
        usage_failure(str(exc))


def token_or_exit(token_file: Path | None) -> str:
    try:
        token = resolve_token(token_file, os.environ)
    except TokenFileError as exc:
        usage_failure(str(exc))
    if token is None:
        usage_failure(MISSING_TOKEN_MESSAGE)
    return token


def launch_token(*, detach: bool, token_file: Path | None) -> str:
    """El token de `create`: `RAYITO_ACCESS_TOKEN` o uno nuevo. Con
    `--detach` se guarda en `--token-file` antes de `run-microvm`, así un
    fallo al escribirlo nunca deja un VM huérfano."""
    existing = os.environ.get(TOKEN_ENV_VAR) or None
    if detach and existing is None and token_file is None:
        usage_failure(DETACH_NEEDS_TOKEN_FILE_MESSAGE)
    token = existing or generate_access_token()
    if detach and token_file is not None:
        try:
            write_token_file(token_file, token)
        except TokenFileError as exc:
            usage_failure(str(exc))
    return token


def connect_sandbox(clients: Clients, sandbox_id: str, token: str) -> Sandbox:
    """`Sandbox.connect`: despierta un sandbox suspendido."""
    return Sandbox.connect(
        sandbox_id,
        access_token=token,
        control_plane=clients.control_plane,
        transport=clients.transport,
    )


def launch_document(sandbox: Sandbox) -> dict[str, Any]:
    info = sandbox.info
    return {
        "sandbox_id": info.sandbox_id,
        "endpoint": info.endpoint,
        "template": info.template_name,
        "template_version": info.template_version,
        "expires_at": iso_utc(info.expires_at),
    }


@sandbox_app.command("create")
def create_command(
    ctx: typer.Context,
    template: Annotated[
        str | None,
        typer.Argument(metavar="[TEMPLATE]", help="Nombre o ARN; por defecto RAYITO_TEMPLATE."),
    ] = None,
    timeout: Annotated[
        int, typer.Option("--timeout", help="Vida máxima del sandbox en segundos.")
    ] = DEFAULT_CREATE_TIMEOUT_SECONDS,
    metadata: Annotated[
        list[str] | None, typer.Option("--metadata", help="K=V no secreto; repetible.")
    ] = None,
    env: EnvOption = None,
    detach: Annotated[
        bool,
        typer.Option("--detach", "-d", help="Crea y sale sin terminal; imprime el sandbox_id."),
    ] = False,
    token_file: TokenFileOption = None,
    user: UserOption = None,
    cwd: CwdOption = None,
) -> None:
    """Crea un sandbox. Sin --detach abre una terminal y lo termina al salir
    (el código de salida es el del shell)."""
    clients = clients_of(ctx)
    metadata_pairs = pairs_or_exit(metadata, "--metadata")
    env_pairs = pairs_or_exit(env, "--env")
    token = launch_token(detach=detach, token_file=token_file)
    sandbox = Sandbox.create(
        template,
        timeout=timeout,
        metadata=metadata_pairs or None,
        envs=env_pairs or None,
        access_token=token,
        control_plane=clients.control_plane,
        transport=clients.transport,
    )
    if detach:
        report_launch(ctx, sandbox)
        sandbox.close()
        return
    try:
        code = run_terminal(sandbox, user=user, cwd=cwd)
    finally:
        sandbox.kill()
    raise typer.Exit(code)


def report_launch(ctx: typer.Context, sandbox: Sandbox) -> None:
    if json_mode(ctx):
        emit_json(launch_document(sandbox))
        return
    echo(sandbox.sandbox_id)


@sandbox_app.command("connect")
def connect_command(
    ctx: typer.Context,
    sandbox_id: Annotated[str, typer.Argument(metavar="ID")],
    user: UserOption = None,
    cwd: CwdOption = None,
    env: EnvOption = None,
    token_file: TokenFileOption = None,
) -> None:
    """Terminal interactiva en un sandbox existente (lo reanuda si está
    suspendido y no lo termina al salir)."""
    envs = pairs_or_exit(env, "--env")
    token = token_or_exit(token_file)
    sandbox = connect_sandbox(clients_of(ctx), sandbox_id, token)
    try:
        code = run_terminal(sandbox, user=user, cwd=cwd, envs=envs or None)
    finally:
        sandbox.close()
    raise typer.Exit(code)


@sandbox_app.command("exec")
def exec_command(
    ctx: typer.Context,
    sandbox_id: Annotated[str, typer.Argument(metavar="ID")],
    command: Annotated[
        list[str], typer.Argument(metavar="-- CMD...", help="Comando y argumentos.")
    ],
    background: Annotated[
        bool, typer.Option("--background", "-b", help="No espera: imprime el pid.")
    ] = False,
    cwd: CwdOption = None,
    user: UserOption = None,
    env: EnvOption = None,
    timeout: Annotated[
        float, typer.Option("--timeout", help="Segundos; 0 es sin plazo de servidor.")
    ] = 0,
    token_file: TokenFileOption = None,
) -> None:
    """Ejecuta `shlex.join(CMD)` en el sandbox con la salida en streaming;
    sale con el código remoto (124 si venció el timeout)."""
    envs = pairs_or_exit(env, "--env")
    token = token_or_exit(token_file)
    sandbox = connect_sandbox(clients_of(ctx), sandbox_id, token)
    try:
        code = run_remote_command(
            sandbox,
            shlex.join(command),
            background=background,
            envs=envs or None,
            user=user,
            cwd=cwd,
            timeout=None if timeout == 0 else timeout,
        )
    finally:
        sandbox.close()
    if code:
        raise typer.Exit(code)


def run_remote_command(
    sandbox: Sandbox,
    cmd: str,
    *,
    background: bool,
    envs: dict[str, str] | None,
    user: str | None,
    cwd: str | None,
    timeout: float | None,
) -> int:
    if background:
        handle = sandbox.commands.run(
            cmd, background=True, envs=envs, user=user, cwd=cwd, timeout=timeout
        )
        echo(str(handle.pid))
        return 0
    try:
        sandbox.commands.run(
            cmd,
            envs=envs,
            user=user,
            cwd=cwd,
            timeout=timeout,
            on_stdout=stream_writer(sys.stdout),
            on_stderr=stream_writer(sys.stderr),
        )
    except CommandExitException as exc:
        return exc.exit_code
    except TimeoutException:
        echo(COMMAND_TIMEOUT_MESSAGE, err=True)
        return EXIT_TIMEOUT
    return 0


def stream_writer(stream: Any) -> Any:
    """Cada chunk tal como llega, en UTF-8 (lo no codificable se sustituye)."""
    binary = getattr(stream, "buffer", None)

    def write(text: str) -> None:
        if binary is not None:
            binary.write(text.encode("utf-8", errors="replace"))
            binary.flush()
            return
        stream.write(text)
        stream.flush()

    return write


def metrics_document(metrics: SandboxMetrics) -> dict[str, Any]:
    document: dict[str, Any] = {
        "timestamp": iso_utc(metrics.timestamp),
        "cpu_used_pct": metrics.cpu_used_pct,
        "cpu_count": metrics.cpu_count,
        "mem_used": metrics.mem_used_bytes,
        "mem_total": metrics.mem_total_bytes,
        "disk_used": metrics.disk_used_bytes,
        "disk_total": metrics.disk_total_bytes,
    }
    if metrics.mem_cache_bytes:
        document["mem_cache"] = metrics.mem_cache_bytes
    return document


@sandbox_app.command("metrics")
def metrics_command(
    ctx: typer.Context,
    sandbox_id: Annotated[str, typer.Argument(metavar="ID")],
    follow: Annotated[
        bool, typer.Option("--follow", "-f", help="Repite hasta Ctrl-C o hasta que muera.")
    ] = False,
    interval: Annotated[
        float, typer.Option("--interval", help="Segundos entre muestras con --follow.")
    ] = DEFAULT_METRICS_INTERVAL_SECONDS,
    token_file: TokenFileOption = None,
) -> None:
    """Una muestra de CPU, memoria y disco (`HealthService.Metrics`);
    conectar despierta un sandbox suspendido."""
    token = token_or_exit(token_file)
    clients = clients_of(ctx)
    sandbox = connect_sandbox(clients, sandbox_id, token)
    try:
        print_metrics(ctx, clients, sandbox, follow=follow, interval=interval)
    finally:
        sandbox.close()


def print_metrics(
    ctx: typer.Context, clients: Clients, sandbox: Sandbox, *, follow: bool, interval: float
) -> None:
    try:
        while True:
            show_metrics(ctx, metrics_document(read_metrics(clients, sandbox)), follow=follow)
            if not follow:
                return
            time.sleep(interval)
    except KeyboardInterrupt:
        return


def read_metrics(clients: Clients, sandbox: Sandbox) -> SandboxMetrics:
    try:
        return sandbox.get_metrics()
    except SandboxException:
        if sandbox_gone(clients, sandbox.sandbox_id):
            fail(SANDBOX_GONE_MESSAGE)
        raise


def sandbox_gone(clients: Clients, sandbox_id: str) -> bool:
    try:
        return clients.control_plane.get_microvm(sandbox_id).state in TERMINAL_STATES
    except SandboxNotFoundException:
        return True


def show_metrics(ctx: typer.Context, document: dict[str, Any], *, follow: bool) -> None:
    if json_mode(ctx) and follow:
        echo(json.dumps(document))
        return
    if json_mode(ctx):
        emit_json(document)
        return
    columns = (*METRICS_COLUMNS, *(("mem_cache",) if "mem_cache" in document else ()))
    table(columns, [[document[column] for column in columns]])
