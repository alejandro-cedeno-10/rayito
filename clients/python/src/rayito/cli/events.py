"""`rayito events deploy|status|destroy|webhook add|webhook list|webhook
remove|list` (M15, m15-events-webhooks): la CLI de `LifecycleEvents`.
`rayito stack deploy events-webhooks` (genérico) también funciona; estos
comandos son el atajo con los nombres de parámetro propios de la función.
"""

from __future__ import annotations

from typing import Annotated

import typer

from rayito._lifecycle_events._domain import (
    DEFAULT_GET_EVENTS_LIMIT,
    DEFAULT_RECONCILER_INTERVAL_MINUTES,
    DEFAULT_STACK_NAME,
)
from rayito._lifecycle_events._service import LifecycleEvents
from rayito._stacks._model import StackComponent
from rayito._stacks._registry import component_by_name
from rayito.cli._console import echo, emit_json, table
from rayito.cli._session import clients_of, json_mode
from rayito.cli.stack import confirm_deploy, confirm_destroy, parse_pairs

#: `events-webhooks`'s own `StackComponent`, the same one `rayito stack
#: deploy events-webhooks` resolves by name: `confirm_deploy`/
#: `confirm_destroy` (`cli/stack.py`) print its `CostStatement` and what
#: `destroy` removes exactly as that command does (§4.6).
_COMPONENT: StackComponent = component_by_name("events-webhooks")  # type: ignore[assignment]

events_app = typer.Typer(
    no_args_is_help=True, help="Eventos de ciclo de vida y webhooks (events=)."
)
webhook_app = typer.Typer(no_args_is_help=True, help="Webhooks registrados.")
events_app.add_typer(webhook_app, name="webhook")


def _events(ctx: typer.Context, stack_name: str) -> LifecycleEvents:
    clients = clients_of(ctx)
    return LifecycleEvents(stack_name=stack_name, region=clients.region, session=clients.session)


@events_app.command("deploy")
def deploy_command(
    ctx: typer.Context,
    artifact_bucket: Annotated[str, typer.Option("--artifact-bucket")],
    log_group_name: Annotated[str, typer.Option("--log-group-name")],
    reconciler_interval_minutes: Annotated[
        int, typer.Option("--reconciler-interval-minutes")
    ] = DEFAULT_RECONCILER_INTERVAL_MINUTES,
    stack_name: Annotated[str, typer.Option("--stack-name")] = DEFAULT_STACK_NAME,
    tag: Annotated[
        list[str],
        typer.Option(
            "--tag",
            help="Etiqueta K=V de la pila y de sus recursos; repetible (cuentas cuya "
            "política de organización exige etiquetas al crear SQS/Lambda).",
        ),
    ] = [],  # noqa: B006
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    ev = _events(ctx, stack_name)
    tags = parse_pairs(tag, option="--tag")
    confirm_deploy(ctx, _COMPONENT, yes=yes)
    status = ev.deploy(
        artifact_bucket=artifact_bucket,
        log_group_name=log_group_name,
        reconciler_interval_minutes=reconciler_interval_minutes,
        tags=tags,
    )
    if json_mode(ctx):
        emit_json({"name": status.name, "state": status.state, "outputs": status.outputs})
        return
    echo(f"events-webhooks: {status.state}")


@events_app.command("status")
def status_command(
    ctx: typer.Context,
    stack_name: Annotated[str, typer.Option("--stack-name")] = DEFAULT_STACK_NAME,
) -> None:
    status = _events(ctx, stack_name).status()
    if json_mode(ctx):
        emit_json(
            None
            if status is None
            else {"name": status.name, "state": status.state, "outputs": status.outputs}
        )
        return
    echo("events-webhooks: no desplegado" if status is None else f"events-webhooks: {status.state}")


@events_app.command("destroy")
def destroy_command(
    ctx: typer.Context,
    stack_name: Annotated[str, typer.Option("--stack-name")] = DEFAULT_STACK_NAME,
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    confirm_destroy(ctx, _COMPONENT, yes=yes)
    _events(ctx, stack_name).destroy()
    if json_mode(ctx):
        emit_json({"name": "events-webhooks", "state": "destroyed"})
        return
    echo("events-webhooks: borrado")


@events_app.command("list")
def list_events_command(
    ctx: typer.Context,
    sandbox_id: Annotated[str | None, typer.Option("--sandbox-id")] = None,
    event_type: Annotated[list[str], typer.Option("--type")] = [],  # noqa: B006
    limit: Annotated[int, typer.Option("--limit")] = DEFAULT_GET_EVENTS_LIMIT,
    order: Annotated[str, typer.Option("--order")] = "desc",
    stack_name: Annotated[str, typer.Option("--stack-name")] = DEFAULT_STACK_NAME,
) -> None:
    events = _events(ctx, stack_name).get_events(
        sandbox_id=sandbox_id, types=event_type or None, limit=limit, order=order
    )
    if json_mode(ctx):
        emit_json(
            [
                {
                    "eventId": event.event_id,
                    "sandboxId": event.sandbox_id,
                    "type": event.type,
                    "killReason": event.kill_reason,
                    "generation": event.generation,
                    "occurredAtMs": event.occurred_at_ms,
                }
                for event in events
            ]
        )
        return
    table(
        ("event_id", "sandbox_id", "type", "occurred_at_ms"),
        ((e.event_id, e.sandbox_id, e.type, e.occurred_at_ms) for e in events),
    )


@webhook_app.command("add")
def webhook_add_command(
    ctx: typer.Context,
    url: Annotated[str, typer.Argument()],
    secret_name: Annotated[str, typer.Option("--secret-name")],
    event_type: Annotated[list[str], typer.Option("--type")],
    stack_name: Annotated[str, typer.Option("--stack-name")] = DEFAULT_STACK_NAME,
) -> None:
    webhook = _events(ctx, stack_name).register_webhook(
        url, secret_name=secret_name, types=event_type
    )
    if json_mode(ctx):
        emit_json(
            {"webhookId": webhook.webhook_id, "url": webhook.url, "types": list(webhook.types)}
        )
        return
    echo(f"webhook {webhook.webhook_id}: {webhook.url} ({', '.join(webhook.types)})")


@webhook_app.command("list")
def webhook_list_command(
    ctx: typer.Context,
    stack_name: Annotated[str, typer.Option("--stack-name")] = DEFAULT_STACK_NAME,
) -> None:
    webhooks = _events(ctx, stack_name).list_webhooks()
    if json_mode(ctx):
        emit_json(
            [{"webhookId": w.webhook_id, "url": w.url, "types": list(w.types)} for w in webhooks]
        )
        return
    table(
        ("webhook_id", "url", "types"),
        ((w.webhook_id, w.url, ",".join(w.types)) for w in webhooks),
    )


@webhook_app.command("remove")
def webhook_remove_command(
    ctx: typer.Context,
    webhook_id: Annotated[str, typer.Argument()],
    stack_name: Annotated[str, typer.Option("--stack-name")] = DEFAULT_STACK_NAME,
) -> None:
    _events(ctx, stack_name).delete_webhook(webhook_id)
    if json_mode(ctx):
        emit_json({"webhookId": webhook_id, "state": "deleted"})
        return
    echo(f"webhook {webhook_id}: borrado")
