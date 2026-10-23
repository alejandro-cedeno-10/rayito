"""`rayito setup`: prepara una cuenta y región de AWS para Rayito en una
orden idempotente (ADR-030). Delgada: lee las opciones, arma los puertos
(`gateways_for`), imprime el plan, pide confirmación y termina con `rayito
doctor`; las decisiones son del dominio (`_account_setup._domain`) y la
ejecución de `AccountSetup`.

Códigos de salida: 0 éxito, `--dry-run` o nada que hacer con el doctor
limpio; 1 fallo (pila bloqueada o incompleta, descarga o checksum, build no
lanzable, un "no" en la confirmación, un `FAIL` del doctor); 2 uso o entorno
(región, bucket, pila de artefactos en conflicto, artefacto, falta de
confirmación, sin credenciales).
"""

from __future__ import annotations

import os
import sys
import tempfile
import zipfile
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Final

import typer
from botocore.exceptions import ClientError

from rayito._account_setup._aws import (
    MicrovmBaseImages,
    S3ArtifactBuckets,
    artifact_stack_gateway,
    iam_stack_gateway,
)
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
from rayito.cli._console import (
    EXIT_FAILURE,
    EXIT_USAGE,
    echo,
    emit_json,
    translate_failure,
)
from rayito.cli._publish import progress_emitter
from rayito.cli._session import Clients, UsageError, clients_of, json_mode
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
from rayito.cli.stack import parse_pairs
from rayito.exceptions import SandboxException, UnimplementedError

#: El contrato de `rayito setup` no tiene código para un fallo ajeno a él
#: (AWS sin traducir, una pila, el entorno): con `--json` se elige el más
#: cercano por su código de salida, igual que la CLI de TypeScript.
UNTRANSLATED_FAILURE_CODE: Final = "stack_blocked"
UNTRANSLATED_USAGE_CODE: Final = "bucket_unavailable"


def gateways_for(clients: Clients, tags: Mapping[str, str] | None = None) -> AccountSetupGateways:
    """Los puertos de `AccountSetup`: los dobles de los tests si
    `Clients.account_setup_override` los trae; si no, los adaptadores de AWS,
    que no llaman a nada al construirse. `tags` son las de `--tag`, para las
    pilas que `setup` crea o actualiza."""
    if clients.account_setup_override is not None:
        return clients.account_setup_override
    provisioner = CloudFormationProvisioner(region=clients.region, session=clients.session)
    return AccountSetupGateways(
        iam_stack=iam_stack_gateway(provisioner, tags=tags),
        artifact_stack=artifact_stack_gateway(provisioner, tags=tags),
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
    error: dict[str, str],
    exit_code: int,
    clients: Clients,
    plan: SetupPlan | None,
    *,
    dry_run: bool,
    json_output: bool,
) -> None:
    echo(f"{ERROR_PREFIX}{error['message']}", err=True)
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
                error=error,
                exit_code=exit_code,
            )
        )


def untranslated_error(exc: Exception) -> tuple[dict[str, str], int] | None:
    """Un fallo que `setup` no traduce, con el mensaje y el código de salida
    del manejador global de la CLI (sin el prefijo `rayito: `). El contrato no
    tiene código para él: se elige por el código de salida, `stack_blocked`
    (1) o `bucket_unavailable` (2). `None` si el manejador global tampoco lo
    traduce."""
    translated = translate_failure(exc)
    if translated is None:
        return None
    message, exit_code = translated
    code = UNTRANSLATED_USAGE_CODE if exit_code == EXIT_USAGE else UNTRANSLATED_FAILURE_CODE
    return {"code": code, "message": message}, exit_code


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
                "Bucket de artefactos en la misma región; por defecto RAYITO_BUCKET, el "
                "de la pila de IAM, el del bootstrap de CDK o uno nuevo "
                "rayito-artifacts-<cuenta>-<región>. Si no existe, se crea con ese nombre "
                "(pila rayito-m0-artifacts)."
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
    tag: Annotated[
        list[str],
        typer.Option(
            "--tag",
            help=(
                "Etiqueta K=V; repetible. Se aplica a las pilas que crea setup "
                "(rayito-m0-artifacts y rayito-m0-iam) y CloudFormation la propaga a sus "
                "recursos; útil si la organización exige etiquetas."
            ),
        ),
    ] = [],  # noqa: B006
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
    caps_image: Annotated[
        bool,
        typer.Option(
            "--caps-image",
            help=(
                "Publica también rayito-base-caps, la base del agente en MicroVM (la misma imagen "
                "con additionalOsCapabilities ALL, desde el mismo zip); es otra versión de imagen "
                "que cuesta. Sin esta opción no se publica."
            ),
        ),
    ] = False,
) -> None:
    """Prepara la cuenta y la región para Rayito en una orden idempotente, como `cdk bootstrap`: el
    bucket de artefactos (el tuyo, el de la pila de IAM o el del bootstrap de CDK; si no hay
    ninguno, lo crea con la pila rayito-m0-artifacts: cifrado, privado, sólo TLS y con los zips
    caducando a los 7 días), la pila de IAM rayito-m0-iam y las imágenes rayito-base y
    rayito-base-caps (la base del agente en MicroVM, sólo con --caps-image) desde el
    rayito-image.zip de la release de tu versión, descargado una vez y comprobado contra su
    SHA256SUMS. Muestra el plan y el coste, pide confirmación y termina con rayito doctor.
    Coste: cada versión publicada de cada imagen ≈ $0,04 por semana de almacenamiento ($0,08
    por GB-mes, mínimo una semana); la pila de IAM es gratis; el zip en el bucket, céntimos; con
    --dry-run sólo lee."""
    clients = clients_of(ctx)
    json_output = json_mode(ctx)
    plan: SetupPlan | None = None
    stack_tags = parse_pairs(tag, option="--tag")
    try:
        require_usable_artifact(artifact)
        service = AccountSetup(gateways_for(clients, stack_tags))
        plan = service.plan(
            SetupRequest(
                region=clients.region,
                bucket=bucket,
                environment_bucket=os.environ.get(BUCKET_ENV_VAR) or None,
                artifact=artifact,
                sdk_version=__version__,
                caps_image=caps_image,
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
        error = {"code": exc.code, "message": str(exc)}
        report_error(error, exc.exit_code, clients, plan, dry_run=dry_run, json_output=json_output)
        raise typer.Exit(exc.exit_code) from None
    except (UsageError, ClientError, SandboxException, UnimplementedError) as exc:
        untranslated = untranslated_error(exc)
        if not json_output or untranslated is None:
            raise
        error, exit_code = untranslated
        report_error(error, exit_code, clients, plan, dry_run=dry_run, json_output=True)
        raise typer.Exit(exit_code) from None
    if code:
        raise typer.Exit(code)
