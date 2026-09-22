"""`rayito sandbox list | info | kill | logs` sobre el plano de control del SDK."""

from __future__ import annotations

import dataclasses
from typing import Annotated, Any

import typer

from rayito._limits import MICROVM_STATES
from rayito._models import SandboxInfo, SandboxListItem
from rayito.cli._console import age, echo, emit_json, iso_utc, table
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
from rayito.exceptions import SandboxNotFoundException
from rayito.sandbox_sync.main import Sandbox

sandbox_app = typer.Typer(no_args_is_help=True, help="MicroVMs vivos: list, info, kill, logs.")

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
