"""`rayito agent template build` (`ai-agent-fast-start`): construye la
imagen de `AgentTemplate` con los runtimes del agente de IA. Mismas banderas
que `rayito template build` más `--no-deepagents`, `--no-prefetch` y
`--kernel-warmup`; sin
fichero de spec, porque la receta es la del SDK."""

from __future__ import annotations

from typing import Annotated

import typer

from rayito._agent._template import (
    AGENT_TEMPLATE_RUNTIMES,
    DEFAULT_AGENT_TEMPLATE_BASE,
    DEFAULT_AGENT_TEMPLATE_NAME,
    AgentTemplate,
)
from rayito._images import DEFAULT_BUILD_TIMEOUT_SECONDS
from rayito._limits import AGENT_MIN_MEMORY_MIB
from rayito.cli._console import echo, emit_json, fail
from rayito.cli._session import clients_of, json_mode
from rayito.exceptions import BuildException, InvalidArgumentException

agent_app = typer.Typer(no_args_is_help=True, help="Agente de IA dentro del sandbox.")
agent_template_app = typer.Typer(
    no_args_is_help=True, help="Plantilla de imagen con los runtimes del agente (AgentTemplate)."
)
agent_app.add_typer(agent_template_app, name="template")


def agent_template_of(
    *,
    name: str,
    base: str,
    memory_mb: int,
    deepagents: bool,
    prefetch: bool,
    kernel_warmup: bool,
) -> AgentTemplate:
    """La `AgentTemplate` que piden las banderas de la CLI."""
    runtimes = tuple(r for r in AGENT_TEMPLATE_RUNTIMES if deepagents or r != "deepagents")
    return AgentTemplate(
        name=name,
        base=base,
        runtimes=runtimes,
        prefetch=prefetch,
        kernel_warmup=kernel_warmup,
        memory_mib=memory_mb,
    )


@agent_template_app.command("build")
def build_command(
    ctx: typer.Context,
    bucket: Annotated[str, typer.Option("--bucket", help="Bucket S3 del artefacto de build.")],
    name: Annotated[
        str, typer.Option("--name", help="Nombre de la imagen a crear/actualizar.")
    ] = DEFAULT_AGENT_TEMPLATE_NAME,
    base: Annotated[
        str, typer.Option("--base", help="Imagen base (publicada con rayito image publish).")
    ] = DEFAULT_AGENT_TEMPLATE_BASE,
    memory_mb: Annotated[int, typer.Option("--memory-mb")] = AGENT_MIN_MEMORY_MIB,
    deepagents: Annotated[
        bool, typer.Option("--deepagents/--no-deepagents", help="Instala el venv de deepagents.")
    ] = True,
    prefetch: Annotated[
        bool,
        typer.Option("--prefetch/--no-prefetch", help="Hornea el demonio de precarga."),
    ] = True,
    kernel_warmup: Annotated[
        bool,
        typer.Option(
            "--kernel-warmup/--no-kernel-warmup",
            help=(
                "Mantiene el calentamiento del kernel de run_code (numpy, pandas...). "
                "Apagado por defecto: create() vuelve antes y el snapshot pesa menos."
            ),
        ),
    ] = False,
    force: Annotated[
        bool, typer.Option("--force", help="Reconstruye aunque nada cambiara.")
    ] = False,
    timeout: Annotated[
        float, typer.Option("--timeout", help="Segundos de espera del build.")
    ] = DEFAULT_BUILD_TIMEOUT_SECONDS,
) -> None:
    """Construye la imagen --name con OpenCode, ripgrep y (salvo
    --no-deepagents) deepagents, todo fijado por hash.

    Cada versión nueva cuesta almacenamiento de snapshot (≈ 3,0 GB, mínimo una
    semana: ≈ $0,056/semana); las versiones se borran con `rayito image`.
    """
    clients = clients_of(ctx)
    emit_json_mode = json_mode(ctx)
    try:
        template = agent_template_of(
            name=name,
            base=base,
            memory_mb=memory_mb,
            deepagents=deepagents,
            prefetch=prefetch,
            kernel_warmup=kernel_warmup,
        )
        info = template.build(
            bucket=bucket,
            force=force,
            timeout=timeout,
            on_build_logs=None if emit_json_mode else echo,
            region=clients.region,
            session=clients.session,
        )
    except InvalidArgumentException as exc:
        fail(str(exc))
    except BuildException as exc:
        fail(f"build fallido: {exc}")
    if emit_json_mode:
        emit_json({"templateId": info.template_id, "buildId": info.build_id, "alias": info.alias})
    else:
        echo(f"template_id={info.template_id}")
        echo(f"build_id={info.build_id}")
