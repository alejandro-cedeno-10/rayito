"""`rayito template build | status | logs` (m15-templates): construye,
sondea y explica un build de `Template`. `build` imprime cada línea del log
del build (salvo en `--json`); `status`/`logs` sin `--version` usan la
versión más reciente de la imagen, sin importar si terminó bien o mal.

El fichero de spec (`.py`) debe definir una variable de módulo `template`
con una instancia de `rayito.Template`: `runpy.run_path` lo ejecuta tal
cual (el mismo nivel de confianza que ejecutar cualquier script local; no
hay sandboxing propio aquí, igual que `rayito image publish` no sandboxa
su Dockerfile).
"""

from __future__ import annotations

import runpy
from pathlib import Path
from typing import Annotated

import typer

from rayito._templates._build import read_recent_logs, status_by_name
from rayito._templates._dsl import Template
from rayito.cli._console import echo, emit_json, fail
from rayito.cli._session import clients_of, json_mode
from rayito.exceptions import BuildException

template_app = typer.Typer(
    no_args_is_help=True, help="Templates declarativos (Template.build): build, status, logs."
)


def _load_template(spec_path: Path) -> Template:
    if not spec_path.is_file():
        fail(f"no existe: {spec_path}")
    namespace = runpy.run_path(str(spec_path))
    loaded = namespace.get("template")
    if not isinstance(loaded, Template):
        fail(f"{spec_path}: define una variable de módulo `template` (rayito.Template)")
    return loaded


@template_app.command("build")
def build_command(
    ctx: typer.Context,
    spec: Annotated[Path, typer.Argument(help="Fichero .py con una variable `template`.")],
    name: Annotated[str, typer.Option("--name", help="Nombre de la imagen a crear/actualizar.")],
    bucket: Annotated[str, typer.Option("--bucket", help="Bucket S3 del artefacto de build.")],
    memory_mb: Annotated[int, typer.Option("--memory-mb")] = 2048,
    force: Annotated[
        bool, typer.Option("--force", help="Reconstruye aunque nada cambiara.")
    ] = False,
    timeout: Annotated[
        float, typer.Option("--timeout", help="Segundos de espera del build.")
    ] = 1800.0,
) -> None:
    clients = clients_of(ctx)
    emit_json_mode = json_mode(ctx)
    template = _load_template(spec)
    try:
        info = Template.build(
            template,
            name,
            bucket=bucket,
            memory_mb=memory_mb,
            force=force,
            timeout=timeout,
            on_build_logs=None if emit_json_mode else echo,
            region=clients.region,
            session=clients.session,
        )
    except BuildException as exc:
        fail(f"build fallido: {exc}")
    if emit_json_mode:
        emit_json({"templateId": info.template_id, "buildId": info.build_id, "alias": info.alias})
    else:
        echo(f"template_id={info.template_id}")
        echo(f"build_id={info.build_id}")


@template_app.command("status")
def status_command(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Nombre de la imagen del template.")],
    version: Annotated[
        str | None,
        typer.Option("--version", help="Versión a consultar; por defecto la más reciente."),
    ] = None,
) -> None:
    clients = clients_of(ctx)
    status = status_by_name(name, version, region=clients.region, session=clients.session)
    if json_mode(ctx):
        emit_json(
            {
                "state": status.state,
                "templateId": status.info.template_id if status.info else None,
                "buildId": status.info.build_id if status.info else None,
                "errorMessage": status.error_message,
            }
        )
        return
    echo(f"state={status.state}")
    if status.info is not None:
        echo(f"template_id={status.info.template_id}")
        echo(f"build_id={status.info.build_id}")
    if status.error_message is not None:
        echo(f"error={status.error_message}")


@template_app.command("logs")
def logs_command(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Nombre de la imagen del template.")],
    limit: Annotated[int, typer.Option("--limit", help="Máximo de líneas a leer.")] = 500,
) -> None:
    clients = clients_of(ctx)
    lines = read_recent_logs(name, region=clients.region, session=clients.session, limit=limit)
    if json_mode(ctx):
        emit_json({"lines": lines})
        return
    for line in lines:
        echo(line)
