"""`rayito setup`: prepara una cuenta y región de AWS para Rayito en una
orden idempotente (ADR-030). Delgada: lee las opciones, arma los puertos
(`gateways_for`), imprime el plan, pide confirmación y termina con `rayito
doctor`; las decisiones son del dominio (`_account_setup._domain`) y la
ejecución de `AccountSetup`.

Códigos de salida: 0 éxito, `--dry-run` o nada que hacer con el doctor
limpio; 1 fallo (pila bloqueada o incompleta, descarga o checksum, build no
lanzable, un "no" en la confirmación, un `FAIL` del doctor); 2 uso o entorno
(región, bucket, artefacto, falta de confirmación, sin credenciales).
"""

from __future__ import annotations

import os
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Annotated

import typer

from rayito._account_setup._aws import CloudFormationIamStack, MicrovmBaseImages, S3ArtifactBuckets
from rayito._account_setup._domain import (
    BASE_IMAGE_NAME,
    BASE_IMAGE_VARIANT,
    BUCKET_ENV_VAR,
    AccountSetupException,
    SetupPlan,
    SetupRequest,
    SetupResult,
)
from rayito._account_setup._ports import AccountSetupGateways
from rayito._account_setup._release import GithubReleaseSource
from rayito._account_setup._service import AccountSetup
from rayito._stacks._cloudformation import CloudFormationProvisioner
from rayito._version import __version__
from rayito.cli._artifact import marker_has_efs, marker_variant
from rayito.cli._checks import DoctorContext
from rayito.cli._console import EXIT_FAILURE, echo, emit_json
from rayito.cli._publish import progress_emitter
from rayito.cli._session import Clients, clients_of, json_mode
from rayito.cli._setup_output import (
    CONFIRM_QUESTION,
    DOCTOR_FAILED,
    DRY_RUN_FOOTER,
    ERROR_PREFIX,
    final_lines,
    plan_lines,
    setup_document,
)
from rayito.cli._setup_publisher import PipelineImagePublisher
from rayito.cli.doctor import doctor_document, exit_code_for, render_human, run_doctor


def gateways_for(clients: Clients) -> AccountSetupGateways:
    """Los puertos de `AccountSetup`: los dobles de los tests si
    `Clients.account_setup_override` los trae; si no, los adaptadores de AWS,
    que no llaman a nada al construirse."""
    if clients.account_setup_override is not None:
        return clients.account_setup_override
    return AccountSetupGateways(
        iam_stack=CloudFormationIamStack(
            CloudFormationProvisioner(region=clients.region, session=clients.session)
        ),
        buckets=S3ArtifactBuckets(clients),
        images=MicrovmBaseImages(clients, clients.region, lambda: clients.account_id),
        publisher=PipelineImagePublisher(clients),
        releases=GithubReleaseSource(),
    )


def require_usable_artifact(artifact: Path | None) -> None:
    """`--artifact` debe existir y ser un zip `full` sin EFS; se comprueba
    antes de cualquier llamada a AWS."""
    if artifact is None:
        return
    if not artifact.is_file():
        raise AccountSetupException("artifact_missing", artifact=str(artifact))
    try:
        usable = marker_variant(artifact) == BASE_IMAGE_VARIANT and not marker_has_efs(artifact)
    except zipfile.BadZipFile:
        usable = False
    if not usable:
        raise AccountSetupException("artifact_variant", artifact=str(artifact))


def confirm_plan(plan: SetupPlan, *, yes: bool, json_output: bool) -> None:
    """Sin nada que escribir o con `--yes` sigue; en una terminal pregunta
    (un "no" sale con 1); sin terminal o con `--json` y sin `--yes` es
    `needs_confirmation`."""
    if not plan.needs_confirmation or yes:
        return
    if json_output or not sys.stdin.isatty():
        raise AccountSetupException("needs_confirmation")
    if not typer.confirm(CONFIRM_QUESTION):
        raise typer.Exit(EXIT_FAILURE)


def report_error(
    exc: AccountSetupException,
    clients: Clients,
    plan: SetupPlan | None,
    *,
    dry_run: bool,
    json_output: bool,
) -> None:
    message = str(exc)
    echo(f"{ERROR_PREFIX}{message}", err=True)
    if json_output:
        emit_json(
            setup_document(
                sdk_version=__version__,
                region=clients.region,
                account=None if plan is None else plan.account,
                dry_run=dry_run,
                ready=False,
                plan=plan,
                result=None,
                doctor=None,
                error={"code": exc.code, "message": message},
                exit_code=exc.exit_code,
            )
        )


def report_dry_run(clients: Clients, plan: SetupPlan, *, json_output: bool) -> None:
    if json_output:
        emit_json(
            setup_document(
                sdk_version=__version__,
                region=clients.region,
                account=plan.account,
                dry_run=True,
                ready=plan.ready,
                plan=plan,
                result=None,
                doctor=None,
                error=None,
                exit_code=0,
            )
        )
    else:
        echo(DRY_RUN_FOOTER)


def report_outcome(
    clients: Clients, plan: SetupPlan, result: SetupResult, *, json_output: bool
) -> int:
    """Cierra con `rayito doctor` (sin `--launch`) sobre la imagen y el
    bucket recién preparados; el código de salida es el del doctor."""
    context = DoctorContext(
        template=BASE_IMAGE_NAME, bucket=result.bucket, transport=clients.transport
    )
    results = run_doctor(clients, context)
    code = exit_code_for(results)
    if json_output:
        emit_json(
            setup_document(
                sdk_version=__version__,
                region=clients.region,
                account=plan.account,
                dry_run=False,
                ready=code == 0,
                plan=plan,
                result=result,
                doctor=doctor_document(clients, context, results, code),
                error=None,
                exit_code=code,
            )
        )
        return code
    render_human(results)
    for line in final_lines(result, clients.region) if code == 0 else [DOCTOR_FAILED]:
        echo(line)
    return code


def run_setup(
    clients: Clients,
    service: AccountSetup,
    plan: SetupPlan,
    *,
    yes: bool,
    json_output: bool,
) -> int:
    confirm_plan(plan, yes=yes, json_output=json_output)
    with tempfile.TemporaryDirectory() as workdir:
        result = service.apply(
            plan, workdir=Path(workdir), on_progress=progress_emitter(json_output)
        )
    return report_outcome(clients, plan, result, json_output=json_output)


def setup(
    ctx: typer.Context,
    bucket: Annotated[
        str | None,
        typer.Option(
            "--bucket",
            help=(
                "Bucket de artefactos en la misma región; por defecto RAYITO_BUCKET, el de la "
                "pila de IAM o el del bootstrap de CDK. Rayito no crea buckets."
            ),
        ),
    ] = None,
    artifact: Annotated[
        Path | None,
        typer.Option(
            "--artifact",
            help=(
                "Zip de la imagen ya verificado (p. ej. con cosign); sin él se descarga "
                "rayito-image.zip de la release rayd-v<versión del SDK> y se comprueba contra "
                "su SHA256SUMS."
            ),
        ),
    ] = None,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            help=(
                "No pedir confirmación (hace falta sin terminal o con --json si hay algo "
                "que crear)."
            ),
        ),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run", help="Sólo lee la cuenta y muestra el plan; no crea ni cambia nada."
        ),
    ] = False,
) -> None:
    """Prepara la cuenta y la región para Rayito en una orden idempotente, como
    `cdk bootstrap`: la pila de IAM rayito-m0-iam, el bucket de artefactos (el
    tuyo, el de la pila o el del bootstrap de CDK; Rayito no crea buckets) y la
    imagen rayito-base desde el rayito-image.zip de la release de tu versión,
    comprobado contra su SHA256SUMS. Muestra el plan y el coste, pide
    confirmación y termina con rayito doctor. Coste: cada versión publicada de
    la imagen ≈ $0,04 por semana de almacenamiento (mínimo una semana); la pila
    de IAM es gratis; con --dry-run sólo lee."""
    clients = clients_of(ctx)
    json_output = json_mode(ctx)
    plan: SetupPlan | None = None
    try:
        require_usable_artifact(artifact)
        service = AccountSetup(gateways_for(clients))
        plan = service.plan(
            SetupRequest(
                region=clients.region,
                bucket=bucket,
                environment_bucket=os.environ.get(BUCKET_ENV_VAR) or None,
                artifact=artifact,
                sdk_version=__version__,
            )
        )
        if not json_output:
            for line in plan_lines(plan):
                echo(line)
        if dry_run:
            report_dry_run(clients, plan, json_output=json_output)
            return
        code = run_setup(clients, service, plan, yes=yes, json_output=json_output)
    except AccountSetupException as exc:
        report_error(exc, clients, plan, dry_run=dry_run, json_output=json_output)
        raise typer.Exit(exc.exit_code) from None
    if code:
        raise typer.Exit(code)
