"""Salida de `rayito setup`: las plantillas de texto del contrato
(`testdata/account-setup/contract.json`), las líneas del plan y del final y
el documento JSON de `--json`. Sin `typer`: cada función es pura."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, Final

from rayito._account_setup._domain import (
    ARTIFACT_STACK_NAME,
    BUCKET_SOURCE_LABELS,
    ResourceKind,
    SetupPlan,
    SetupResult,
    SetupStep,
    applied_steps,
)

__all__ = [
    "ACTION_TEMPLATES",
    "BUCKET_LINE_TEMPLATE",
    "BUCKET_SOURCE_LABELS",
    "CONFIRM_QUESTION",
    "COST_HEADER",
    "COST_LINE_TEMPLATE",
    "DOCTOR_FAILED",
    "DONE_TEMPLATE",
    "DRY_RUN_FOOTER",
    "ERROR_PREFIX",
    "HEADER_TEMPLATE",
    "NOTHING_TO_DO",
    "RESOURCE_LABELS",
    "STEP_TEMPLATE",
    "TEMPLATE_LINE_TEMPLATE",
    "final_lines",
    "plan_lines",
    "setup_document",
]

HEADER_TEMPLATE: Final = "rayito setup: cuenta {account}, región {region}"
STEP_TEMPLATE: Final = "  {label} {name}: {action}"
RESOURCE_LABELS: Final[Mapping[ResourceKind, str]] = MappingProxyType(
    {
        "bucket": "bucket",
        "iamStack": "pila de IAM",
        "baseImage": "imagen",
        "capsImage": "imagen con capacidades",
    }
)
ACTION_TEMPLATES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "bucketReuse": "reutilizar ({source})",
        "bucketCreate": "crear con la pila {stack} ({source})",
        "bucketWait": "pila {stack} en curso ({status}): esperar",
        "stackCreate": "crear",
        "stackUpdate": "actualizar ArtifactBucket {previousBucket} -> {bucket}",
        "stackTemplate": "actualizar a la plantilla infra/iam.yaml de este SDK",
        "stackReuse": "ya existe ({status})",
        "stackNewer": (
            "ya existe ({status}) con la plantilla de Rayito {stackSdkVersion}, más nuevo que "
            "este SDK: se conserva"
        ),
        "stackWait": "en curso ({status}): esperar",
        "imagePublishRelease": "publicar desde {release} (≈ 10 min)",
        "imagePublishArtifact": "publicar desde {artifact} (≈ 10 min)",
        "imageReuse": "ya existe (versión {imageVersion} lanzable)",
        "imageWait": "build en curso (versión {imageVersion}): esperar",
        "capsSkip": "omitida (sin --caps-image)",
    }
)
COST_HEADER: Final = "Coste aproximado:"
COST_LINE_TEMPLATE: Final = "  {text}"
NOTHING_TO_DO: Final = "La cuenta ya está preparada: no hay nada que crear."
DRY_RUN_FOOTER: Final = "no se creó ni cambió nada (--dry-run)"
CONFIRM_QUESTION: Final = "¿Preparar la cuenta?"
DONE_TEMPLATE: Final = "rayito setup: cuenta preparada en {region}"
TEMPLATE_LINE_TEMPLATE: Final = "RAYITO_TEMPLATE={imageArn}"
BUCKET_LINE_TEMPLATE: Final = "RAYITO_BUCKET={bucket}"
DOCTOR_FAILED: Final = "rayito setup: rayito doctor encontró algún FAIL; la cuenta no está lista"
ERROR_PREFIX: Final = "rayito: "

#: Clave de `ACTION_TEMPLATES` de cada (recurso, acción).
ACTION_KEYS: Final[Mapping[tuple[str, str], str]] = MappingProxyType(
    {
        ("bucket", "create"): "bucketCreate",
        ("bucket", "reuse"): "bucketReuse",
        ("bucket", "wait"): "bucketWait",
        ("iamStack", "create"): "stackCreate",
        ("iamStack", "update"): "stackUpdate",
        ("iamStack", "reuse"): "stackReuse",
        ("iamStack", "wait"): "stackWait",
        ("baseImage", "reuse"): "imageReuse",
        ("baseImage", "wait"): "imageWait",
        ("capsImage", "reuse"): "imageReuse",
        ("capsImage", "wait"): "imageWait",
        ("capsImage", "skip"): "capsSkip",
    }
)


def action_text(plan: SetupPlan, step: SetupStep) -> str:
    if step.action == "publish":
        key = "imagePublishArtifact" if step.source == "artifact" else "imagePublishRelease"
    elif step.resource == "iamStack" and step.action == "update" and step.previous_bucket is None:
        key = "stackTemplate"
    elif step.resource == "iamStack" and step.action == "reuse" and step.stack_sdk_version:
        key = "stackNewer"
    else:
        key = ACTION_KEYS[(step.resource, step.action)]
    return ACTION_TEMPLATES[key].format(
        stack=ARTIFACT_STACK_NAME,
        source=BUCKET_SOURCE_LABELS.get(step.source or "", ""),
        status=step.status,
        previousBucket=step.previous_bucket,
        bucket=plan.bucket,
        release=step.release,
        artifact=plan.artifact,
        imageVersion=step.image_version,
        stackSdkVersion=step.stack_sdk_version,
    )


def plan_lines(plan: SetupPlan) -> list[str]:
    """El encabezado, un renglón por paso, el coste de lo que cuesta y, si
    ya está todo, el aviso de que no hay nada que crear."""
    lines = [HEADER_TEMPLATE.format(account=plan.account, region=plan.region)]
    lines.extend(
        STEP_TEMPLATE.format(
            label=RESOURCE_LABELS[step.resource], name=step.name, action=action_text(plan, step)
        )
        for step in plan.steps
    )
    if plan.cost:
        lines.append(COST_HEADER)
        lines.extend(COST_LINE_TEMPLATE.format(text=text) for text in plan.cost)
    if plan.ready:
        lines.append(NOTHING_TO_DO)
    return lines


def final_lines(result: SetupResult, region: str) -> list[str]:
    return [
        DONE_TEMPLATE.format(region=region),
        TEMPLATE_LINE_TEMPLATE.format(imageArn=result.image_arn),
        BUCKET_LINE_TEMPLATE.format(bucket=result.bucket),
    ]


def setup_document(
    *,
    sdk_version: str,
    region: str,
    account: str | None,
    dry_run: bool,
    ready: bool,
    plan: SetupPlan | None,
    result: SetupResult | None,
    doctor: dict[str, Any] | None,
    error: dict[str, str] | None,
    exit_code: int,
) -> dict[str, Any]:
    """El documento único de `--json`, con las claves del contrato en su
    orden; `steps` y `cost` vacíos si el plan mismo falló. Tras `apply`, el
    paso de cada imagen publicada lleva su `imageVersion` (`applied_steps`)."""
    return {
        "rayito": sdk_version,
        "region": region,
        "account": account,
        "dryRun": dry_run,
        "ready": ready,
        "steps": [] if plan is None else [step.as_json() for step in applied_steps(plan, result)],
        "cost": [] if plan is None else list(plan.cost),
        "result": None if result is None else result.as_json(),
        "doctor": doctor,
        "error": error,
        "exitCode": exit_code,
    }
