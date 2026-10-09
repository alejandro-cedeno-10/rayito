"""`rayito image publish | list | sizes | prune | zip`."""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Annotated, Any

import typer

from rayito._images import DEFAULT_BUILD_TIMEOUT_SECONDS, DEFAULT_MEMORY_MIB
from rayito._sizing import (
    BASELINE_MEMORY_MIB,
    NAME_TO_MEMORY_MIB,
    SIZE_NAMES,
    apply_size_suffix,
    resolve_size,
)
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
    DEFAULT_STACK_NAME,
    OS_CAPABILITY_CHOICES,
    PublishSettings,
    default_image_name,
    publish,
    publish_with_sizes,
    require_efs_combination,
    validate_run_id,
)
from rayito.cli._session import Clients, clients_of, json_mode

image_app = typer.Typer(
    no_args_is_help=True, help="Imágenes de MicroVM: publish, list, sizes, prune, zip."
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


#: Ayuda de `--with-efs`, compartida por `zip` y `publish`.
WITH_EFS_HELP = (
    "Añade amazon-efs-utils (volumes=, m15-efs-volumes): +~198 MB de imagen; "
    "publish exige --os-capabilities ALL y publica rayito-base-caps-efs."
)


def validate_efs_combination(with_efs: bool, variant: str, os_capabilities: str | None) -> None:
    if not with_efs:
        return
    try:
        require_efs_combination(variant, os_capabilities)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--with-efs") from exc


ARTIFACT_RUN_ID_HELP = (
    "Sube el zip a rayito/images/runs/<RUN_ID>/ en vez de la clave compartida, "
    "para que la limpieza de una ejecución (aceptación, e2e) no borre el artefacto "
    "de otra; el resumen JSON dice si se subió (artifactUploaded)."
)


def validate_artifact_run_id(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return validate_run_id(value)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--artifact-run-id") from exc


def validate_os_capabilities(value: str | None) -> str | None:
    if value is not None and value not in OS_CAPABILITY_CHOICES:
        raise typer.BadParameter(
            f"admitidas: {', '.join(OS_CAPABILITY_CHOICES)}", param_hint="--os-capabilities"
        )
    return value


BASELINE_SIZE_NAME = next(
    name for name, mib in NAME_TO_MEMORY_MIB.items() if mib == BASELINE_MEMORY_MIB
)
# `--sizes` nunca admite el baseline (ya lo publica `publish_command` sin
# sufijo, ver `validate_sizes`): la ayuda de `--sizes` lista sólo lo que de
# verdad se puede pasar, en vez de repetir `SIZE_NAMES` a mano y dejar que
# se desalinee (code review de PR #76).
EXTRA_SIZE_NAMES = tuple(name for name in SIZE_NAMES if name != BASELINE_SIZE_NAME)


def validate_sizes(raw: str | None) -> tuple[str, ...]:
    """`--sizes 512mb,4gb` (m15-sizes-catalog): nombres separados por comas,
    cada uno del catálogo cerrado (`_sizing.SIZE_NAMES`), sin duplicados ni
    el baseline (ya lo publica `publish_command` sin sufijo)."""
    if raw is None:
        return ()
    names = [piece.strip() for piece in raw.split(",") if piece.strip()]
    for name in names:
        if name not in SIZE_NAMES:
            raise typer.BadParameter(
                f"{name!r}: admitidos {', '.join(SIZE_NAMES)}", param_hint="--sizes"
            )
        if name == BASELINE_SIZE_NAME:
            raise typer.BadParameter(
                f"{BASELINE_SIZE_NAME!r} ya es el baseline sin sufijo; quítalo de --sizes",
                param_hint="--sizes",
            )
    if len(set(names)) != len(names):
        raise typer.BadParameter("tamaños repetidos", param_hint="--sizes")
    return tuple(names)


def validate_baseline_memory_mib(memory_mib: int, size_names: tuple[str, ...]) -> None:
    """Con `--sizes`, `--memory-mib` sólo admite `DEFAULT_MEMORY_MIB`
    (m15-sizes-catalog, code review de PR #76): la imagen sin sufijo que
    `--sizes` publica es siempre el baseline (`BASELINE_SIZE_NAME`/
    `validate_sizes`), así que un `--memory-mib` distinto rompería esa
    regla en silencio — `Sandbox.create(size="2gb")` seguiría asumiendo
    2048 MiB mientras la imagen sin sufijo fuera otra cosa. Sin `--sizes`
    no hay regla que proteger: `--memory-mib` sigue siendo libre, como
    antes de sizes-catalog."""
    if size_names and memory_mib != DEFAULT_MEMORY_MIB:
        raise typer.BadParameter(
            f"--sizes publica el baseline sin sufijo a {DEFAULT_MEMORY_MIB} MiB "
            "(la regla de sizes-catalog: la imagen sin sufijo siempre es el baseline); "
            "quita --memory-mib o publica sin --sizes para elegir otro valor",
            param_hint="--memory-mib",
        )


# `--env KEY=VALUE`: el mismo patrón de nombre de variable de entorno que
# POSIX exige (letra o `_` inicial, luego alfanuméricos o `_`); rechaza
# `=valor` (clave vacía) y claves con espacios o símbolos que
# `environmentVariables` aceptaría pero que `rayd`/el guest no podrían
# exportar como variable de entorno de verdad.
ENV_KEY_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def parse_environment_assignments(raw: list[str]) -> dict[str, str]:
    """`--env KEY=VALUE` (repetible, m15-sizes-catalog): nunca un
    interruptor de activación (ADR-014 regla 4), sólo configuración
    horneada en la imagen (`environmentVariables` de `create`/`update-
    microvm-image`; `PublishSettings.environment_variables`, el seam mínimo
    que esta función necesita en vez de la extracción completa de
    `_images.py`/`ImageBuildGateway` que nombra la arquitectura M15 —
    `cli/_publish.py` tasks.md §11). Nunca secretos: cualquiera con
    `GetMicrovmImageVersion` y todo proceso del guest la leen en claro; usa
    `SecretStore`/`secrets=` para eso (code review de PR #76)."""
    result: dict[str, str] = {}
    for item in raw:
        if "=" not in item:
            raise typer.BadParameter(f"formato K=V esperado: {item!r}", param_hint="--env")
        key, _, value = item.partition("=")
        if not ENV_KEY_PATTERN.fullmatch(key):
            raise typer.BadParameter(
                f"{key!r}: nombre de variable inválido (letras, dígitos y `_`, "
                "sin empezar por un dígito)",
                param_hint="--env",
            )
        result[key] = value
    return result


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
    sizes: Annotated[
        str | None,
        typer.Option(
            "--sizes",
            help=(
                f"Tamaños extra separados por comas ({','.join(EXTRA_SIZE_NAMES)}), "
                "publicados además del baseline desde el mismo artefacto."
            ),
        ),
    ] = None,
    env: Annotated[
        list[str],
        typer.Option(
            "--env",
            help=(
                "KEY=VALUE horneado en environmentVariables (repetible). Nunca secretos: "
                "legible por GetMicrovmImageVersion y por todo proceso del guest; usa "
                "SecretStore/secrets= para eso."
            ),
        ),
    ] = [],  # noqa: B006
    with_efs: Annotated[bool, typer.Option("--with-efs", help=WITH_EFS_HELP)] = False,
    artifact_run_id: Annotated[
        str | None, typer.Option("--artifact-run-id", help=ARTIFACT_RUN_ID_HELP)
    ] = None,
) -> None:
    """Publica la imagen: sube el zip y espera a que la versión sea lanzable.

    Sube el zip a S3 (clave por sha256), crea o actualiza la imagen y
    espera al gate de tres estados; reutiliza una versión igual. Con
    `--sizes` publica, además, una imagen por tamaño desde el mismo
    artefacto y lo informa todo en un único bloque de salida: un documento
    JSON, o un `RAYITO_TEMPLATE=` del baseline seguido de un
    `RAYITO_TEMPLATE_<SIZE>=` por tamaño. Sin `--sizes`, sólo el baseline.
    Con `--artifact-run-id`, la clave del zip es de esa ejecución."""
    run_id = validate_artifact_run_id(artifact_run_id)
    size_names = validate_sizes(sizes)
    validate_baseline_memory_mib(memory_mib, size_names)
    environment_variables = parse_environment_assignments(env)
    capabilities = validate_os_capabilities(os_capabilities)
    validate_efs_combination(with_efs, validate_variant(variant), capabilities)
    settings = PublishSettings(
        artifact=artifact,
        image_name=image_name or default_image_name(variant, with_efs=with_efs),
        variant=variant,
        bucket=resolve_bucket(bucket),
        stack_name=stack_name,
        build_role_arn=build_role_arn,
        base_image_version=base_image_version,
        memory_mib=memory_mib,
        force=force,
        timeout_seconds=timeout_seconds,
        os_capabilities=capabilities,
        environment_variables=environment_variables,
        with_efs=with_efs,
        artifact_run_id=run_id,
    )
    code = (
        publish_with_sizes(clients_of(ctx), settings, size_names, json_output=json_mode(ctx))
        if size_names
        else publish(clients_of(ctx), settings, json_output=json_mode(ctx))
    )
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


SIZES_COLUMNS = ("size", "name", "published", "imageArn", "state", "sameArtifact", "createdAt")


def size_image_names(base_name: str) -> dict[str, str]:
    """El nombre de imagen que cada tamaño del catálogo cerrado tendría para
    `base_name` (m15-sizes-catalog): la misma convención de sufijo que
    `Sandbox.create(size=...)` resuelve en el SDK, vía `apply_size_suffix`
    (nunca reimplementada aquí: una sola fuente para el sufijo, code review
    de PR #76). No dice si de verdad se publicó cada una; eso lo cruza
    `sizes_command` con `list-microvm-images`."""
    return {size: apply_size_suffix(base_name, resolve_size(size)) for size in SIZE_NAMES}


def active_code_artifact(clients: Clients, image: dict[str, Any] | None) -> str | None:
    """El `codeArtifact.uri` (la clave sha256 de S3) de la versión activa
    más reciente de una imagen ya publicada, o `None` si la imagen no se
    publicó o no tiene ninguna versión activa. `GetMicrovmImageVersion` no
    tiene cuota propia en `apiTps` (AWS_API_NOTES.md §24): una llamada por
    imagen publicada no cambia el presupuesto de coste de la CLI, y nunca
    lanza ningún sandbox (`sizes_command` la usa para `sameArtifact`, code
    review de PR #76: el reemplazo gratuito, sin booteo, del parity check
    de `agent_version` que `doctor` no hace — tasks.md §11)."""
    if image is None or image.get("latestActiveImageVersion") is None:
        return None
    response = clients.microvms.get_microvm_image_version(
        imageIdentifier=image["imageArn"], imageVersion=str(image["latestActiveImageVersion"])
    )
    return artifact_basename(response)


@image_app.command("sizes")
def sizes_command(
    ctx: typer.Context,
    variant: Annotated[str, typer.Option("--variant", help="full, slim o poly.")] = "full",
    image_name: Annotated[
        str | None,
        typer.Option(
            "--image-name",
            help="Nombre base de `image publish --image-name`; sustituye al de la variante.",
        ),
    ] = None,
) -> None:
    """Qué tamaño del catálogo cerrado ya está publicado para esta variante.

    Por cada tamaño (`512mb` a `8gb`), qué imagen publicó `rayito image
    publish --sizes` o si ninguna. Hace una `list-microvm-images` filtrada por
    el nombre base y, sólo si hay algún tamaño adicional publicado, una
    `GetMicrovmImageVersion` (gratuita) por imagen publicada. Nunca
    construye, publica ni lanza nada. `sameArtifact` compara el artefacto
    de la versión activa de cada tamaño con el del baseline: `false` delata
    un tamaño publicado desde un zip distinto. `--image-name` lista la
    familia publicada con `rayito image publish --image-name X --sizes`."""
    base_name = image_name or default_image_name(validate_variant(variant))
    clients = clients_of(ctx)
    published = {image["name"]: image for image in listed_images(clients, base_name)}
    names = size_image_names(base_name)
    other_sizes_published = any(
        size != BASELINE_SIZE_NAME and name in published for size, name in names.items()
    )
    # Sólo pide el `codeArtifact.uri` del baseline si hay con qué
    # compararlo: con el catálogo entero sin tamaños extra, ni esta
    # llamada gratuita hace falta (sigue "ninguna llamada adicional" en el
    # caso común, `test_sizes_command_human_table`).
    baseline_artifact = (
        active_code_artifact(clients, published.get(names[BASELINE_SIZE_NAME]))
        if other_sizes_published
        else None
    )
    rows = []
    for size, name in names.items():
        image = published.get(name)
        same_artifact = None
        if size != BASELINE_SIZE_NAME and other_sizes_published:
            artifact = active_code_artifact(clients, image)
            if baseline_artifact is not None and artifact is not None:
                same_artifact = artifact == baseline_artifact
        rows.append(
            {
                "size": size,
                "name": name,
                "published": image is not None,
                "imageArn": None if image is None else image["imageArn"],
                "state": None if image is None else image["state"],
                "sameArtifact": same_artifact,
                "createdAt": None if image is None else image["createdAt"],
            }
        )
    if json_mode(ctx):
        emit_json(rows)
        return
    table(SIZES_COLUMNS, [[row[column] for column in SIZES_COLUMNS] for row in rows])


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
    with_efs: Annotated[bool, typer.Option("--with-efs", help=WITH_EFS_HELP)] = False,
    artifact_run_id: Annotated[
        str | None, typer.Option("--artifact-run-id", help=ARTIFACT_RUN_ID_HELP)
    ] = None,
) -> None:
    """Zip determinista del directorio de la imagen (sin tests, cachés ni locks)."""
    validate_variant(variant)
    copied = None
    started = time.monotonic()
    if sidecar is not None:
        copied = copy_sidecar(sidecar, image_dir / SIDECAR_DIRECTORY)
    count = write_zip(image_dir, destination, variant, with_efs=with_efs)
    summary = {
        "destination": str(destination),
        "files": count,
        "bytes": destination.stat().st_size,
        "variant": variant,
        "withEfs": with_efs,
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
        f"{destination}: {count} files, {summary['bytes']} bytes (variant {variant}"
        f"{', with efs' if with_efs else ''}) "
        f"sha256={summary['sha256']}"
    )
