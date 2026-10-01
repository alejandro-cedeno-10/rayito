"""`rayito stack list | deploy | status | destroy` (M15 foundations,
ADR-016): la CLI del convenio `OptionalStack`. `deploy` imprime siempre el
`CostStatement` del componente y pide confirmación salvo `--yes`; `destroy`
dice qué se conserva; `list` no hace ninguna llamada a AWS.
"""

from __future__ import annotations

from typing import Annotated

import typer

from rayito._stacks._model import StackComponent
from rayito._stacks._service import OptionalStacks
from rayito.cli._console import echo, emit_json, table
from rayito.cli._session import clients_of, json_mode
from rayito.exceptions import UnimplementedError

stack_app = typer.Typer(
    no_args_is_help=True,
    help="Componentes OptionalStack en tu propia cuenta: list, deploy, status, destroy.",
)


def _stacks(ctx: typer.Context) -> OptionalStacks:
    clients = clients_of(ctx)
    return OptionalStacks(region=clients.region, session=clients.session)


def _resolve(stacks: OptionalStacks, component: str) -> StackComponent:
    for candidate in stacks.components():
        if candidate.name == component:
            return candidate
    raise typer.BadParameter(f"componente desconocido: {component!r}", param_hint="component")


def _parse_pairs(values: list[str], *, option: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for item in values:
        if "=" not in item:
            raise typer.BadParameter(f"formato K=V esperado: {item!r}", param_hint=option)
        key, _, value = item.partition("=")
        parsed[key] = value
    return parsed


def _print_cost(component: StackComponent) -> None:
    echo(f"Coste y activación de {component.name}")
    echo(f"  Activa: rayito stack deploy {component.name}")
    echo(f"  Recursos y llamadas AWS: {', '.join(component.cost.creates) or '(ninguno)'}")
    echo(f"  Coste aproximado (reposo): {component.cost.idle_monthly}")
    if component.cost.per_use:
        echo(f"  Coste por uso: {'; '.join(component.cost.per_use)}")
    echo(f"  Cómo apagarla: rayito stack destroy {component.name} ({component.cost.removal})")
    if component.cost.source:
        echo(f"  Fuente: {component.cost.source}")


@stack_app.command("list")
def list_command(ctx: typer.Context) -> None:
    """El catálogo completo; nunca hace una llamada a AWS."""
    stacks = _stacks(ctx)
    components = stacks.components()
    if json_mode(ctx):
        emit_json(
            [
                {
                    "name": component.name,
                    "supported": component.supported,
                    "idleMonthly": component.cost.idle_monthly,
                    "description": component.description,
                }
                for component in components
            ]
        )
        return
    table(
        ("name", "supported", "idle_monthly"),
        (
            (component.name, component.supported, component.cost.idle_monthly)
            for component in components
        ),
    )


@stack_app.command("status")
def status_command(
    ctx: typer.Context,
    component: Annotated[str, typer.Argument()],
    stack_name: Annotated[str | None, typer.Option("--stack-name")] = None,
) -> None:
    stacks = _stacks(ctx)
    _resolve(stacks, component)
    status = stacks.status(component, stack_name=stack_name)
    if json_mode(ctx):
        emit_json(
            None
            if status is None
            else {"name": status.name, "state": status.state, "outputs": status.outputs}
        )
        return
    if status is None:
        echo(f"{component}: no desplegado")
        return
    echo(f"{component}: {status.state}")
    for key, value in status.outputs.items():
        echo(f"  {key} = {value}")


@stack_app.command("deploy")
def deploy_command(
    ctx: typer.Context,
    component: Annotated[str, typer.Argument()],
    stack_name: Annotated[str | None, typer.Option("--stack-name")] = None,
    param: Annotated[list[str], typer.Option("--param", help="Parámetro K=V; repetible.")] = [],  # noqa: B006
    artifact_bucket: Annotated[str | None, typer.Option("--artifact-bucket")] = None,
    tag: Annotated[list[str], typer.Option("--tag", help="Etiqueta K=V; repetible.")] = [],  # noqa: B006
    yes: Annotated[bool, typer.Option("--yes", help="No pedir confirmación.")] = False,
) -> None:
    stacks = _stacks(ctx)
    resolved = _resolve(stacks, component)
    if not json_mode(ctx):
        _print_cost(resolved)
    if not resolved.supported:
        raise UnimplementedError(f"rayito stack deploy {component}", resolved.description)
    if not yes and not json_mode(ctx) and not typer.confirm("¿Desplegar esta pila?"):
        raise typer.Exit(1)
    status = stacks.deploy(
        component,
        stack_name=stack_name,
        parameters=_parse_pairs(param, option="--param"),
        artifact_bucket=artifact_bucket,
        tags=_parse_pairs(tag, option="--tag"),
    )
    if json_mode(ctx):
        emit_json({"name": status.name, "state": status.state, "outputs": status.outputs})
        return
    echo(f"{component}: {status.state}")


@stack_app.command("destroy")
def destroy_command(
    ctx: typer.Context,
    component: Annotated[str, typer.Argument()],
    stack_name: Annotated[str | None, typer.Option("--stack-name")] = None,
    yes: Annotated[bool, typer.Option("--yes", help="No pedir confirmación.")] = False,
) -> None:
    stacks = _stacks(ctx)
    resolved = _resolve(stacks, component)
    if not json_mode(ctx):
        echo(f"Al borrar: {resolved.cost.removal}")
    if not yes and not json_mode(ctx) and not typer.confirm(f"¿Borrar la pila de {component}?"):
        raise typer.Exit(1)
    stacks.destroy(component, stack_name=stack_name)
    if json_mode(ctx):
        emit_json({"name": component, "state": "destroyed"})
        return
    echo(f"{component}: borrado")
