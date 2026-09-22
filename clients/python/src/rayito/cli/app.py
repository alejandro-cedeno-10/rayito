"""La aplicación `typer`: opciones globales, los grupos `image` y `sandbox`,
el comando `doctor` y `run_shim` para los scripts de `scripts/`.

Códigos de salida: 0 éxito; 1 la operación falló (build no lanzable,
candidato de prune que sobrevive, id desconocido en `kill`, `FAIL` del
doctor, `ClientError` sin tratar impreso como `AWS error <Code>: <Message>`);
2 uso o entorno (argumentos, sin región, sin credenciales, sin extra).
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Sequence
from typing import Annotated, Any

import typer
import typer.core

from rayito.cli._console import translated_failures
from rayito.cli.doctor import doctor
from rayito.cli.image import image_app
from rayito.cli.sandbox import sandbox_app

PROG_NAME = "rayito"
SHIM_HINT = "ejecuta con: uv run --project clients/python python scripts/{script} …"
EXIT_USAGE = 2


class RayitoGroup(typer.core.TyperGroup):
    """Traduce los errores de AWS y del SDK a una línea y un código de salida
    en un solo sitio, para el callback raíz y para cada comando."""

    def invoke(self, ctx: Any) -> Any:
        with translated_failures():
            return super().invoke(ctx)


app = typer.Typer(
    cls=RayitoGroup,
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
    help="Rayito: sandboxes sobre AWS Lambda MicroVMs",
)
app.add_typer(image_app, name="image")
app.add_typer(sandbox_app, name="sandbox")
app.command("doctor")(doctor)


@app.callback()
def root(
    ctx: typer.Context,
    profile: Annotated[
        str | None, typer.Option("--profile", help="Perfil de AWS (por defecto AWS_PROFILE).")
    ] = None,
    region: Annotated[
        str | None, typer.Option("--region", help="Región de AWS (por defecto AWS_REGION).")
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Un único documento JSON por stdout; progreso por stderr."),
    ] = False,
    verbose: Annotated[
        bool, typer.Option("--verbose", help="Logs INFO del SDK y de la CLI.")
    ] = False,
) -> None:
    """Rayito: sandboxes sobre AWS Lambda MicroVMs."""
    ctx.meta["profile"] = profile
    ctx.meta["region"] = region
    ctx.meta["json"] = json_output
    if verbose:
        logging.basicConfig(level=logging.INFO)
        logging.getLogger("rayito").setLevel(logging.INFO)


def exit_status(code: object) -> int:
    if code is None:
        return 0
    if isinstance(code, int):
        return code
    print(code, file=sys.stderr)
    return 1


def run_shim(argv: Sequence[str]) -> int:
    """Invoca la aplicación como lo haría `rayito` y devuelve el código de
    salida: los errores de uso salen con 2 y el mensaje de typer por stderr;
    `typer.Exit(code)` se propaga tal cual."""
    try:
        app(list(argv), prog_name=PROG_NAME)
    except SystemExit as exc:
        return exit_status(exc.code)
    return 0


def shim_hint(script: str) -> str:
    return SHIM_HINT.format(script=script)
