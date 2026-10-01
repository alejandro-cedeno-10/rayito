"""`rayito domain deploy | status | destroy` (m15-custom-domain, ADR-024):
una fachada fina sobre `rayito stack {deploy,status,destroy} custom-domain`
(`cli/stack.py`) con los nombres de parámetro propios de `CustomDomain`
(`--public-domain`, `--certificate-arn`). Para parámetros avanzados o un
nombre de pila distinto usa `rayito stack` directamente; esta CLI no repite
ninguna opción que ya ofrezca esa."""

from __future__ import annotations

from typing import Annotated

import typer

from rayito._custom_domain._service import STACK_COMPONENT, CustomDomain
from rayito._stacks._registry import component_by_name
from rayito.cli._console import echo, emit_json
from rayito.cli._session import clients_of, json_mode
from rayito.exceptions import CustomDomainException

domain_app = typer.Typer(
    no_args_is_help=True,
    help="Dominio propio sobre CloudFront (domain=/CustomDomain): deploy, status, destroy.",
)


def _domain(ctx: typer.Context, public_domain: str, stack_name: str | None) -> CustomDomain:
    clients = clients_of(ctx)
    return CustomDomain(
        public_domain=public_domain,
        stack_name=stack_name,
        region=clients.region,
        session=clients.session,
    )


@domain_app.command("deploy")
def deploy_command(
    ctx: typer.Context,
    public_domain: Annotated[str, typer.Option("--public-domain")],
    certificate_arn: Annotated[str, typer.Option("--certificate-arn")],
    stack_name: Annotated[str | None, typer.Option("--stack-name")] = None,
    yes: Annotated[bool, typer.Option("--yes", help="No pedir confirmación.")] = False,
) -> None:
    domain = _domain(ctx, public_domain, stack_name)
    if not json_mode(ctx):
        component = component_by_name(STACK_COMPONENT)
        assert component is not None  # registrado por foundations, siempre presente
        echo(f"Coste y activación de {STACK_COMPONENT}")
        echo(f"  Recursos y llamadas AWS: {', '.join(component.cost.creates)}")
        echo(f"  Coste aproximado (reposo): {component.cost.idle_monthly}")
        echo(f"  Cómo apagarla: rayito domain destroy ({component.cost.removal})")
    if not yes and not json_mode(ctx) and not typer.confirm("¿Desplegar esta pila?"):
        raise typer.Exit(1)
    status = domain.deploy(certificate_arn=certificate_arn)
    if json_mode(ctx):
        emit_json({"name": status.name, "state": status.state, "outputs": status.outputs})
        return
    echo(f"{STACK_COMPONENT}: {status.state}")


@domain_app.command("status")
def status_command(
    ctx: typer.Context,
    public_domain: Annotated[str, typer.Option("--public-domain")],
    stack_name: Annotated[str | None, typer.Option("--stack-name")] = None,
) -> None:
    domain = _domain(ctx, public_domain, stack_name)
    status = domain.status()
    if json_mode(ctx):
        emit_json(
            None
            if status is None
            else {"name": status.name, "state": status.state, "outputs": status.outputs}
        )
        return
    if status is None:
        echo(f"{STACK_COMPONENT}: no desplegado")
        return
    echo(f"{STACK_COMPONENT}: {status.state}")
    for key, value in status.outputs.items():
        echo(f"  {key} = {value}")


@domain_app.command("destroy")
def destroy_command(
    ctx: typer.Context,
    public_domain: Annotated[str, typer.Option("--public-domain")],
    stack_name: Annotated[str | None, typer.Option("--stack-name")] = None,
    yes: Annotated[bool, typer.Option("--yes", help="No pedir confirmación.")] = False,
) -> None:
    domain = _domain(ctx, public_domain, stack_name)
    if not yes and not json_mode(ctx) and not typer.confirm("¿Borrar la pila de dominio propio?"):
        raise typer.Exit(1)
    try:
        domain.destroy()
    except CustomDomainException as exc:
        raise typer.BadParameter(str(exc)) from exc
    if json_mode(ctx):
        emit_json({"name": STACK_COMPONENT, "state": "destroyed"})
        return
    echo(f"{STACK_COMPONENT}: borrado")
