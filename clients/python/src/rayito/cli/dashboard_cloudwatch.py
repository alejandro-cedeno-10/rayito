"""`rayito dashboard cloudwatch` (se registra en `dashboard.py`): imprime el JSON de un dashboard de
CloudWatch (Logs Insights sobre `/rayito/*` y, si se piden, métricas OTLP y el
uso de la cuota). No llama a AWS ni necesita credenciales: sólo genera texto,
y generarlo es gratis; desplegarlo en CloudWatch cuesta (el dashboard pasados los
gratuitos y cada widget de Logs Insights por GB escaneado, ver `COST_NOTE`).
Para crearlo, `aws cloudwatch put-dashboard` con el fichero que escribas o
`rayito stack deploy dashboard-cloudwatch`, que despliega el mismo dashboard
(ADR-026, fase 3).

Códigos de salida: 0; 2 por uso (sin región, prefijo o grupo inválido,
fichero de salida ilegible).
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from rayito._aws_region import REGION_ENV_VAR, resolve_region
from rayito._dashboard._cloudwatch_board import (
    DEFAULT_LOG_GROUP_PREFIX,
    build_board,
    render_board,
)
from rayito._stacks.components.dashboard_cloudwatch import DASHBOARD_IDLE_COST, LOGS_INSIGHTS_COST
from rayito._stacks.components.quota_alerts import CUSTOM_METRIC_MONTHLY_COST
from rayito.cli._console import EXIT_USAGE, echo, fail
from rayito.exceptions import InvalidArgumentException

JSON_INDENT = 2
NO_REGION_MESSAGE = f"sin región: pasa --region o exporta {REGION_ENV_VAR}"
COST_NOTE = (
    "Generar el JSON es gratis y no llama a AWS. Desplegarlo en CloudWatch (put-dashboard o la "
    f"pila dashboard-cloudwatch) cuesta: {DASHBOARD_IDLE_COST}. {LOGS_INSIGHTS_COST} "
    "(precios de CloudWatch, us-east-1, consultados 2026-10-13)."
)
COMMAND_HELP = (
    f"Imprime el JSON de un dashboard de CloudWatch con los logs de tus sandboxes.\n\n{COST_NOTE}"
)
QUOTA_COST_NOTE = (
    "requiere la pila quota-alerts, que publica esa métrica (~"
    f"{CUSTOM_METRIC_MONTHLY_COST} USD al mes)"
)


def region_of(ctx: typer.Context) -> str:
    """La región de las opciones globales o del entorno; las pruebas inyectan
    una sesión en `ctx.obj`. Nunca crea una sesión: generar no necesita AWS."""
    injected = getattr(ctx.obj, "region", None)
    region = injected or resolve_region(ctx.meta.get("region"))
    if not region:
        fail(NO_REGION_MESSAGE, code=EXIT_USAGE)
    return str(region)


def cloudwatch_command(
    ctx: typer.Context,
    log_group: Annotated[
        list[str] | None,
        typer.Option(
            "--log-group",
            help="Grupo de logs a consultar, repetible. Sin él, todos los que empiezan por "
            "--log-group-prefix.",
        ),
    ] = None,
    log_group_prefix: Annotated[
        str,
        typer.Option(
            "--log-group-prefix",
            help="Prefijo de los grupos de logs (con la barra final para no abarcar otros).",
        ),
    ] = DEFAULT_LOG_GROUP_PREFIX,
    otlp: Annotated[
        bool,
        typer.Option(
            "--otlp",
            help="Añade CPU, memoria y disco de las métricas OTLP de rayd (telemetry=), con "
            "PromQL (las consultas desde la consola no se facturan). No verificado en AWS "
            "real todavía.",
        ),
    ] = False,
    quota: Annotated[
        bool,
        typer.Option(
            "--quota",
            help="Añade el uso de la cuota de memoria: grafica la métrica que publica la pila "
            f"quota-alerts, así que está vacía hasta que se despliegue; {QUOTA_COST_NOTE}.",
        ),
    ] = False,
    output: Annotated[
        Path | None,
        typer.Option(
            "--output", "-o", help="Escribe el JSON en este fichero en vez de imprimirlo."
        ),
    ] = None,
) -> None:
    """Imprime el JSON de un dashboard de CloudWatch con los logs de tus sandboxes."""
    region = region_of(ctx)
    try:
        board = build_board(
            region=region,
            log_group_prefix=log_group_prefix,
            log_groups=log_group or (),
            include_otlp=otlp,
            include_quota=quota,
        )
    except InvalidArgumentException as exc:
        fail(str(exc), code=EXIT_USAGE)
    text = render_board(board, indent=JSON_INDENT)
    if output is None:
        echo(text)
        return
    try:
        output.write_text(text + "\n", encoding="utf-8")
    except OSError:
        fail("--output: no se pudo escribir el fichero", code=EXIT_USAGE)
    echo(f"Dashboard escrito en {output.name}.")


#: `--help` muestra el docstring, y un f-string no lo es: se asigna aquí para
#: que el coste (con las cifras de `limits.json`) salga en la ayuda.
cloudwatch_command.__doc__ = COMMAND_HELP
