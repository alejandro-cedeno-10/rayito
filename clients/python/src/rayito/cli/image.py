"""`rayito image publish | list | prune | zip`."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Annotated, Any

import typer

from rayito.cli._artifact import VARIANTS, artifact_sha256, copy_sidecar, write_zip
from rayito.cli._console import echo, emit_json, table
from rayito.cli._prune import (
    DEFAULT_IMAGE_NAME,
    DEFAULT_KEEP,
    DEFAULT_WAIT_TIMEOUT_SECONDS,
    PruneSettings,
)
from rayito.cli._prune import run as run_prune
from rayito.cli._publish import (
    DEFAULT_BUILD_TIMEOUT_SECONDS,
    DEFAULT_MEMORY_MIB,
    DEFAULT_STACK_NAME,
    OS_CAPABILITY_CHOICES,
    PublishSettings,
    default_image_name,
    publish,
)
from rayito.cli._session import Clients, clients_of, json_mode

image_app = typer.Typer(
    no_args_is_help=True, help="Imágenes de MicroVM: publish, list, prune, zip."
)

BUCKET_ENV_VAR = "RAYITO_BUCKET"
SIDECAR_DIRECTORY = "kernel-sidecar"
IMAGE_COLUMNS = (
    "name",
    "state",
    "latestActiveImageVersion",
    "latestFailedImageVersion",
    "createdAt",
)
VERSION_COLUMNS = ("imageVersion", "state", "status", "baseImageVersion", "artifact", "createdAt")


def resolve_bucket(bucket: str | None) -> str:
    resolved = bucket or os.environ.get(BUCKET_ENV_VAR)
    if not resolved:
        raise typer.BadParameter(f"pasa --bucket o exporta {BUCKET_ENV_VAR}", param_hint="--bucket")
    return resolved


def validate_variant(variant: str) -> str:
    if variant not in VARIANTS:
        raise typer.BadParameter(f"admitidas: {', '.join(VARIANTS)}", param_hint="--variant")
    return variant


def validate_os_capabilities(value: str | None) -> str | None:
    if value is not None and value not in OS_CAPABILITY_CHOICES:
        raise typer.BadParameter(
            f"admitidas: {', '.join(OS_CAPABILITY_CHOICES)}", param_hint="--os-capabilities"
        )
    return value


@image_app.command("publish")
def publish_command(
    ctx: typer.Context,
    artifact: Annotated[Path, typer.Option("--artifact", help="Zip con el Dockerfile en la raíz.")],
    base_image_version: Annotated[
        str,
        typer.Option(
            "--base-image-version",
            help=(
                "baseImageVersion de la base gestionada "
                "(imageVersion de list-managed-microvm-image-versions)."
            ),
        ),
    ],
    bucket: Annotated[
        str | None,
        typer.Option("--bucket", help=f"Bucket de artefactos; por defecto {BUCKET_ENV_VAR}."),
    ] = None,
    image_name: Annotated[
        str | None,
        typer.Option("--image-name", help="Sustituye el nombre por defecto de la variante."),
    ] = None,
    variant: Annotated[
        str,
        typer.Option(
            "--variant",
            help="full (rayito-base), slim (rayito-base-slim) o poly (rayito-base-poly).",
        ),
    ] = "full",
    os_capabilities: Annotated[
        str | None,
        typer.Option("--os-capabilities", help="additionalOsCapabilities de la imagen (ALL)."),
    ] = None,
    build_role_arn: Annotated[
        str | None, typer.Option("--build-role-arn", help="Sustituye la salida del stack.")
    ] = None,
    stack_name: Annotated[
        str,
        typer.Option("--stack-name", help="Stack de CloudFormation con la salida BuildRoleArn."),
    ] = DEFAULT_STACK_NAME,
    memory_mib: Annotated[int, typer.Option("--memory-mib")] = DEFAULT_MEMORY_MIB,
    timeout_seconds: Annotated[
        float, typer.Option("--timeout-seconds")
    ] = DEFAULT_BUILD_TIMEOUT_SECONDS,
    force: Annotated[
        bool, typer.Option("--force", help="Construye una versión nueva aunque una coincida.")
    ] = False,
) -> None:
    """Sube el zip a S3 (clave por sha256), crea o actualiza la imagen y
    espera al gate de tres estados; reutiliza una versión igual."""
    settings = PublishSettings(
        artifact=artifact,
        image_name=image_name or default_image_name(validate_variant(variant)),
        variant=variant,
        bucket=resolve_bucket(bucket),
        stack_name=stack_name,
        build_role_arn=build_role_arn,
        base_image_version=base_image_version,
        memory_mib=memory_mib,
        force=force,
        timeout_seconds=timeout_seconds,
        os_capabilities=validate_os_capabilities(os_capabilities),
    )
    code = publish(clients_of(ctx), settings, json_output=json_mode(ctx))
    if code:
        raise typer.Exit(code)


def listed_images(clients: Clients, name_filter: str | None) -> list[dict[str, Any]]:
    params: dict[str, Any] = {}
    if name_filter:
        params["nameFilter"] = name_filter
    paginator = clients.microvms.get_paginator("list_microvm_images")
    return [
        {
            "name": str(item["name"]),
            "imageArn": str(item["imageArn"]),
            "state": str(item["state"]),
            "latestActiveImageVersion": item.get("latestActiveImageVersion"),
            "latestFailedImageVersion": item.get("latestFailedImageVersion"),
            "createdAt": item["createdAt"],
        }
        for page in paginator.paginate(**params)
        for item in page.get("items", [])
    ]


def artifact_basename(item: dict[str, Any]) -> str | None:
    uri = item.get("codeArtifact", {}).get("uri")
    return None if uri is None else str(uri).rsplit("/", 1)[-1]


def listed_versions(clients: Clients, arn: str) -> list[dict[str, Any]]:
    paginator = clients.microvms.get_paginator("list_microvm_image_versions")
    versions = [
        {
            "imageVersion": str(item["imageVersion"]),
            "state": str(item["state"]),
            "status": str(item["status"]),
            "baseImageVersion": item.get("baseImageVersion"),
            "artifact": artifact_basename(item),
            "createdAt": item["createdAt"],
        }
        for page in paginator.paginate(imageIdentifier=arn)
        for item in page.get("items", [])
    ]
    versions.sort(key=lambda version: version["createdAt"], reverse=True)
    return versions


@image_app.command("list")
def list_command(
    ctx: typer.Context,
    name: Annotated[
        str | None, typer.Argument(help="Nombre o ARN de una imagen: lista sus versiones.")
    ] = None,
    name_filter: Annotated[
        str | None, typer.Option("--name-filter", help="Filtro de nombre de list-microvm-images.")
    ] = None,
) -> None:
    """Sin NAME, las imágenes de la cuenta; con NAME, sus versiones (la más nueva primero)."""
    if name and name_filter:
        raise typer.BadParameter("--name-filter sólo sin NAME", param_hint="--name-filter")
    clients = clients_of(ctx)
    columns: tuple[str, ...] = IMAGE_COLUMNS
    if name:
        rows = listed_versions(clients, clients.control_plane.resolve_template_arn(name))
        columns = VERSION_COLUMNS
    else:
        rows = listed_images(clients, name_filter)
    if json_mode(ctx):
        emit_json(rows)
        return
    table(columns, [[row[column] for column in columns] for row in rows])


@image_app.command("prune")
def prune_command(
    ctx: typer.Context,
    image_name: Annotated[str, typer.Option("--image-name")] = DEFAULT_IMAGE_NAME,
    keep: Annotated[
        int, typer.Option("--keep", help="Versiones lanzables más nuevas que se conservan.")
    ] = DEFAULT_KEEP,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Imprime el plan sin borrar nada.")
    ] = False,
    wait_timeout: Annotated[
        float, typer.Option("--wait-timeout", help="Segundos de espera a que la imagen se asiente.")
    ] = DEFAULT_WAIT_TIMEOUT_SECONDS,
) -> None:
    """Borra versiones antiguas de una en una; primero con --dry-run."""
    try:
        settings = PruneSettings(
            image_name=image_name, keep=keep, dry_run=dry_run, wait_timeout=wait_timeout
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--keep") from exc
    code = run_prune(clients_of(ctx), settings, json_output=json_mode(ctx))
    if code:
        raise typer.Exit(code)


@image_app.command("zip")
def zip_command(
    ctx: typer.Context,
    image_dir: Annotated[Path, typer.Argument(help="Directorio con el Dockerfile en la raíz.")],
    destination: Annotated[Path, typer.Argument(help="Zip de salida.")],
    variant: Annotated[str, typer.Option("--variant", help="full, slim o poly.")] = "full",
    sidecar: Annotated[
        Path | None,
        typer.Option(
            "--sidecar", help="Copia primero este kernel-sidecar a IMAGE_DIR/kernel-sidecar."
        ),
    ] = None,
) -> None:
    """Zip determinista del directorio de la imagen (sin tests, cachés ni locks)."""
    validate_variant(variant)
    copied = None
    started = time.monotonic()
    if sidecar is not None:
        copied = copy_sidecar(sidecar, image_dir / SIDECAR_DIRECTORY)
    count = write_zip(image_dir, destination, variant)
    summary = {
        "destination": str(destination),
        "files": count,
        "bytes": destination.stat().st_size,
        "variant": variant,
        "sha256": artifact_sha256(destination),
        "sidecarFiles": copied,
        "seconds": round(time.monotonic() - started, 2),
    }
    if json_mode(ctx):
        emit_json(summary)
        return
    if copied is not None:
        echo(f"{image_dir / SIDECAR_DIRECTORY}: {copied} files")
    echo(
        f"{destination}: {count} files, {summary['bytes']} bytes (variant {variant}) "
        f"sha256={summary['sha256']}"
    )
