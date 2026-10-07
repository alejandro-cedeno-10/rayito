"""`rayito domain deploy | status | destroy` (m15-custom-domain, ADR-024):
una fachada fina sobre `rayito stack {deploy,status,destroy} custom-domain`
(`cli/stack.py`) con los nombres de parámetro propios de `CustomDomain`
(`--public-domain`, `--certificate-arn`). Para parámetros avanzados o un
nombre de pila distinto usa `rayito stack` directamente; esta CLI no repite
ninguna opción que ya ofrezca esa.

`status`/`destroy` sólo necesitan el nombre de la pila: van directas a
`OptionalStacks` (como `cli/stack.py`), nunca pasan por `CustomDomain`, así
que no piden `--public-domain` (no lo usarían para nada: `CustomDomain` lo
necesita sólo para `host_for()`/`register()`, no para `deploy`/`status`/
`destroy`, que ya identifican la pila por `STACK_COMPONENT`/`--stack-name`).
`deploy` sí construye una `CustomDomain` real, porque valida el formato de
`--public-domain` antes de tocar AWS."""

from __future__ import annotations

from typing import Annotated

import typer

from rayito._custom_domain._domain import WILDCARD_LABEL
from rayito._custom_domain._service import (
    CUSTOM_DOMAIN_WAIT_TIMEOUT_SECONDS,
    DISTRIBUTION_DOMAIN_NAME_OUTPUT_KEY,
    STACK_COMPONENT,
    CustomDomain,
)
from rayito._stacks._registry import component_by_name
from rayito._stacks._service import OptionalStacks
from rayito.cli._console import echo, emit_json
from rayito.cli._session import clients_of, json_mode

domain_app = typer.Typer(
    no_args_is_help=True,
    help="Dominio propio sobre CloudFront (experimental, CustomDomain): deploy, status, destroy.",
)


def _domain(ctx: typer.Context, public_domain: str, stack_name: str | None) -> CustomDomain:
    clients = clients_of(ctx)
    return CustomDomain(
        public_domain=public_domain,
        stack_name=stack_name,
        region=clients.region,
        session=clients.session,
    )


def _stacks(ctx: typer.Context) -> OptionalStacks:
    clients = clients_of(ctx)
    return OptionalStacks(region=clients.region, session=clients.session)


@domain_app.command("deploy")
def deploy_command(
    ctx: typer.Context,
    public_domain: Annotated[
        str, typer.Option("--public-domain", help="Dominio público (p. ej. sbx.example.com).")
    ],
    certificate_arn: Annotated[
        str, typer.Option("--certificate-arn", help="Certificado ACM, en us-east-1.")
    ],
    stack_name: Annotated[str | None, typer.Option("--stack-name")] = None,
    alternate_domain_names: Annotated[
        list[str] | None,
        typer.Option(
            "--alternate-domain-name",
            help=(
                "Nombre alternativo explícito (repetible) en lugar del comodín "
                "*.<public-domain>; p. ej. 8000-<alias>.<public-domain>."
            ),
        ),
    ] = None,
    yes: Annotated[bool, typer.Option("--yes", help="No pedir confirmación.")] = False,
) -> None:
    """Despliega (o actualiza) la pila custom-domain; imprime su coste y el CNAME."""
    domain = _domain(ctx, public_domain, stack_name)
    if not json_mode(ctx):
        component = component_by_name(STACK_COMPONENT)
        assert component is not None  # registrado por foundations, siempre presente
        echo(f"Coste y activación de {STACK_COMPONENT}")
        echo(f"  Recursos y llamadas AWS: {', '.join(component.cost.creates)}")
        echo(f"  Coste aproximado (reposo): {component.cost.idle_monthly}")
        if component.cost.per_use:
            echo(f"  Coste por uso: {'; '.join(component.cost.per_use)}")
        echo(f"  Cómo apagarla: rayito domain destroy ({component.cost.removal})")
    if not yes and not json_mode(ctx) and not typer.confirm("¿Desplegar esta pila?"):
        raise typer.Exit(1)
    status = domain.deploy(
        certificate_arn=certificate_arn, alternate_domain_names=alternate_domain_names or None
    )
    if json_mode(ctx):
        emit_json({"name": status.name, "state": status.state, "outputs": status.outputs})
        return
    echo(f"{STACK_COMPONENT}: {status.state}")
    target = status.outputs.get(DISTRIBUTION_DOMAIN_NAME_OUTPUT_KEY)
    if target:
        names = alternate_domain_names or [f"{WILDCARD_LABEL}.{domain.public_domain}"]
        echo(f"  DNS: apunta un CNAME/alias de {', '.join(names)} a {target}")


@domain_app.command("status")
def status_command(
    ctx: typer.Context,
    stack_name: Annotated[str | None, typer.Option("--stack-name")] = None,
) -> None:
    """Estado y salidas de la pila custom-domain (sólo lectura)."""
    status = _stacks(ctx).status(STACK_COMPONENT, stack_name=stack_name)
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
    stack_name: Annotated[str | None, typer.Option("--stack-name")] = None,
    yes: Annotated[bool, typer.Option("--yes", help="No pedir confirmación.")] = False,
) -> None:
    """Borra la pila custom-domain; dice antes qué se conserva."""
    component = component_by_name(STACK_COMPONENT)
    assert component is not None  # registrado por foundations, siempre presente
    if not json_mode(ctx):
        echo(f"Al borrar: {component.cost.removal}")
    if not yes and not json_mode(ctx) and not typer.confirm("¿Borrar la pila de dominio propio?"):
        raise typer.Exit(1)
    # `OptionalStacks.destroy` por sí sola usaría su propio
    # `DEFAULT_WAIT_TIMEOUT_SECONDS` (600 s, `_stacks/_service.py`), que no
    # alcanza para que CloudFront deshabilite y borre la distribución
    # (`CUSTOM_DOMAIN_WAIT_TIMEOUT_SECONDS`).
    _stacks(ctx).destroy(
        STACK_COMPONENT, stack_name=stack_name, wait_timeout=CUSTOM_DOMAIN_WAIT_TIMEOUT_SECONDS
    )
    if json_mode(ctx):
        emit_json({"name": STACK_COMPONENT, "state": "destroyed"})
        return
    echo(f"{STACK_COMPONENT}: borrado")
