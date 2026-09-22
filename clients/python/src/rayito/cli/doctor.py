"""`rayito doctor`: diez comprobaciones de la cuenta antes del primer
`Sandbox.create()`, con `OK | WARN | FAIL | SKIP` y salida 1 sólo con `FAIL`."""

from __future__ import annotations

import dataclasses
import logging
import os
from typing import Annotated, Any

import typer

from rayito._version import __version__
from rayito.cli._checks import (
    DEFAULT_TEMPLATE,
    LAUNCH_CHECKS,
    PRE_LAUNCH_CHECKS,
    CheckResult,
    DoctorContext,
    compatibility_rows,
    run_check,
)
from rayito.cli._console import echo, emit_json, status_label, table
from rayito.cli._session import Clients, clients_of, json_mode

logger = logging.getLogger("rayito.cli")

BUCKET_ENV_VAR = "RAYITO_BUCKET"
NAME_WIDTH = 15
NOT_CHECKED = "no comprobado"
VERBOSE_DETAIL_KEYS = frozenset({"results", "quotas"})
COMPATIBILITY_COLUMNS = ("SDK", "rayd mínimo", "Estado", "Nota")


def run_doctor(clients: Clients, context: DoctorContext) -> list[CheckResult]:
    """Las siete comprobaciones de sólo lectura y, dentro de un `finally`
    que mata el sandbox de `--launch` pase lo que pase, las tres del agente."""
    results = [run_check(spec, clients, context) for spec in PRE_LAUNCH_CHECKS]
    try:
        results.extend(run_check(spec, clients, context) for spec in LAUNCH_CHECKS)
    finally:
        kill_launched(context)
    return results


def kill_launched(context: DoctorContext) -> None:
    if context.launched is None:
        return
    try:
        context.launched.kill()
    except Exception:
        logger.warning(
            "no se pudo terminar el sandbox %s", context.launched.sandbox_id, exc_info=True
        )


def exit_code_for(results: list[CheckResult]) -> int:
    return 1 if any(result.status == "FAIL" for result in results) else 0


def compatibility_status(results: list[CheckResult]) -> str:
    for result in results:
        if result.name == "compatibility":
            return NOT_CHECKED if result.status == "SKIP" else result.status
    return NOT_CHECKED


def compatibility_table_rows(results: list[CheckResult]) -> list[tuple[str, ...]]:
    sdk_series = ".".join(__version__.split(".")[:2])
    status = compatibility_status(results)
    return [
        (
            row["sdk_series"],
            row["min_agent_version"],
            status if row["sdk_series"] == sdk_series else "",
            row["note"],
        )
        for row in compatibility_rows()
    ]


def render_human(results: list[CheckResult]) -> None:
    """Una línea por comprobación; los detalles de `WARN`/`FAIL` indentados
    salvo las tablas crudas (`results`, `quotas`), que sólo van en `--json`."""
    for result in results:
        echo(f"{status_label(result.status)} {result.name:<{NAME_WIDTH}} {result.summary}")
        if result.status in {"WARN", "FAIL"}:
            for key, value in result.details.items():
                if key not in VERBOSE_DETAIL_KEYS:
                    echo(f"       {key}: {value}")
    echo()
    table(COMPATIBILITY_COLUMNS, compatibility_table_rows(results))
    counts = {
        status: sum(1 for r in results if r.status == status)
        for status in ("OK", "WARN", "FAIL", "SKIP")
    }
    echo()
    echo(
        f"rayito doctor: {counts['OK']} OK, {counts['WARN']} WARN, "
        f"{counts['FAIL']} FAIL, {counts['SKIP']} SKIP"
    )


def render_json(
    clients: Clients, context: DoctorContext, results: list[CheckResult], code: int
) -> None:
    document: dict[str, Any] = {
        "rayito": __version__,
        "region": clients.region,
        "account": context.account,
        "principal_kind": context.principal_kind,
        "checks": [dataclasses.asdict(result) for result in results],
        "compatibility": compatibility_rows(),
        "launched_sandbox_id": None if context.launched is None else context.launched.sandbox_id,
        "exit_code": code,
    }
    emit_json(document)


def doctor(
    ctx: typer.Context,
    template: Annotated[
        str, typer.Option("--template", help="Nombre o ARN de la imagen a comprobar.")
    ] = DEFAULT_TEMPLATE,
    template_version: Annotated[
        str | None,
        typer.Option(
            "--template-version", help="Versión de la imagen; por defecto la última activa."
        ),
    ] = None,
    bucket: Annotated[
        str | None,
        typer.Option("--bucket", help=f"Bucket de artefactos (o {BUCKET_ENV_VAR})."),
    ] = None,
    launch: Annotated[
        bool,
        typer.Option(
            "--launch",
            help=(
                "Crea un sandbox de 300 s (≈ $0,002) para las comprobaciones 8-10 "
                "y lo mata al final."
            ),
        ),
    ] = False,
) -> None:
    """Diagnostica la cuenta: credenciales, imágenes gestionadas, cuotas,
    IAM, bucket, gate de la imagen, MicroVMs vivos, token, agente y
    compatibilidad SDK ↔ rayd ↔ imagen."""
    clients = clients_of(ctx)
    context = DoctorContext(
        template=template,
        template_version=template_version,
        bucket=bucket or os.environ.get(BUCKET_ENV_VAR) or None,
        launch=launch,
        transport=clients.transport,
    )
    results = run_doctor(clients, context)
    code = exit_code_for(results)
    if json_mode(ctx):
        render_json(clients, context, results, code)
    else:
        render_human(results)
        if context.launched is not None:
            echo(f"sandbox de --launch terminado: {context.launched.sandbox_id}")
    if code:
        raise typer.Exit(code)
