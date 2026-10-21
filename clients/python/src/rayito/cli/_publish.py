"""Publish the rayd image to Lambda MicroVMs from a prebuilt artifact zip
(`rayito image publish`; `scripts/publish_image.py` is a shim over it).

Pipeline (MILESTONES.md, M1 ``image-publish``): content-addressed upload of the
zip to S3 (skipped when the key already exists) -> ``create-microvm-image`` the
first time, ``update-microvm-image`` afterwards (PUT semantics: every required
field is sent again) -> poll the three independent states until the version is
launchable (image ``CREATED|UPDATED``, version ``SUCCESSFUL``, status
``ACTIVE``) -> print the ``snapshotBuild`` sizes and the image ARN.

A version whose artifact and configuration already match is reused instead of
rebuilt (every version costs a week of snapshot storage); ``--force`` builds
anyway. On a failed build the version/build ``stateReason`` and the newest
CloudWatch events of the image log group are printed.

Every AWS parameter name used here appears literally in ``AWS_API_NOTES.md``
§4 or ``docs/aws-api/model_summary.md``.

``--variant slim`` publishes the measurement image ``rayito-base-slim`` from a
zip built with ``image_zip.py --variant slim`` (kernel warm-up disabled through
the ``warmup_variant`` marker); ``--variant poly`` publishes ``rayito-base-poly``
from a zip built with ``image_zip.py --variant poly`` (the ``kernels_variant``
marker makes the Dockerfile's conditional layer install the bash kernel pins of
``kernel-sidecar/requirements-poly.txt``); ``--variant full`` (default)
publishes ``rayito-base``. The artifact's marker must match the flag, checked
before any AWS call; ``--image-name`` still overrides the default name. Hooks,
memory, ``/validate`` and the three-state gate are identical for the variants.

``--with-efs`` (``m15-efs-volumes``) publishes a zip built with
``image_zip.py --with-efs`` (the ``efs_variant`` marker makes the
Dockerfile's conditional layer install ``amazon-efs-utils``); it requires
``--os-capabilities ALL`` (mounting needs ``CAP_SYS_ADMIN``) and the ``full``
variant, and its default name is ``rayito-base-caps-efs``. The marker and the
flag must agree, checked before any AWS call (``require_matching_variant``):
an efs artifact never publishes under a plain name, nor the reverse.

``--os-capabilities ALL`` adds ``additionalOsCapabilities: ["ALL"]`` (the only
value the service model accepts) and nothing else to the image configuration:
with it ``rayd`` installs the IMDS block for uid 1000 at boot. Publish it under
its own name (``make image-publish-caps`` uses ``rayito-base-caps``) so
``rayito-base``'s version history stays homogeneous; a configuration that
differs by this key never reuses a default version.

``--base-image-version`` is required: every publish names the managed base
image version (``baseImageVersion`` of create/update-microvm-image, the
``imageVersion`` that ``list-managed-microvm-image-versions`` returns), so a
silent roll of the managed image cannot change what a build runs on. The value
``latest`` asks for the newest listed version and prints the one it chose
(``resolve_base_image_version``): the roll is then explicit and visible, and an
explicit number is what a reproducible build pins. The API
accepts ``1`` but ``list-microvm-image-versions`` echoes the normalised ``1.0``
(AWS_API_NOTES.md Q52), so the reuse check compares ``baseImageVersion``
numerically and every other key exactly.

There is no default bucket: ``--bucket`` or ``RAYITO_BUCKET`` name the
artifact bucket of the account that publishes.

``--artifact-run-id RUN_ID`` scopes the artifact key to one run:
``rayito/images/runs/<RUN_ID>/rayd-<sha>.zip`` instead of the shared
content-addressed ``rayito/images/rayd-<sha>.zip``. Two acceptance runs that
publish the same commit would otherwise share one object, and the first run
to clean up would delete the artifact the other run's image still points at.
The summary always says whether this invocation uploaded the object
(``artifactUploaded``), so a cleanup deletes only what its own run uploaded.
Without the flag the key, and everything else, is unchanged.

``--sizes 512mb,4gb`` (m15-sizes-catalog) publishes one extra image per
named size from the *same* artifact, named ``<image_name>-<size>``
(``apply_size_suffix``/``_sizing.SIZE_NAMES``), each with its own
``resources[0].minimumMemoryInMiB`` and an ``environmentVariables``
entry (``RAYITO_BASELINE_MEMORY_MIB``) baked in as declared image
information, readable with ``get-microvm-image-version``; it does not
reach ``commands.run``'s environment (Q119). With ``--sizes``
every image this invocation publishes (baseline and sizes alike) is
reported in a single output block: one JSON document, or one
``RAYITO_TEMPLATE=`` line for the baseline plus one
``RAYITO_TEMPLATE_<SIZE>=`` line per size (``publish_with_sizes``), never
one document/line per image. Without ``--sizes`` only the unsuffixed
baseline publishes, exactly as before sizes-catalog existed (``publish()``,
unaggregated). ``--env KEY=VALUE`` (repeatable) adds arbitrary
``environmentVariables`` to every image this invocation publishes, baseline
included (``PublishSettings.environment_variables``, sent through the
shared build core ``rayito._images`` that ``Template.build()`` also uses);
it is never an activation
switch (ADR-014 rule 4): it only configures the image's own guest
environment, nothing about what the SDK does at ``Sandbox.create()``. A
``--env``/``--sizes`` publish only reuses a version whose
``environmentVariables`` match exactly, read with
``get-microvm-image-version`` because ``list-microvm-image-versions`` never
echoes them (Q118, ``_images.echoed_environment_variables``). Without ``--env`` the
reuse check is 0.5.x's, list-only and with no extra call: a version built
earlier *with* ``--env`` under the same name and artifact is then reused as
is; pass ``--force`` to rebuild it without the variables.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from botocore.exceptions import ClientError

from rayito._images import (
    BUILD_ROLE_OUTPUT_KEY as BUILD_ROLE_OUTPUT_KEY,
)
from rayito._images import (
    DEFAULT_IAM_STACK_NAME,
    VersionGate,
    account_image_arn,
    existing_image,
    find_reusable_version,
    gate_progress_line,
    submit_image_build,
    upload_if_absent,
)
from rayito._images import wait_for_gate as wait_for_image_gate
from rayito._images_publish_domain import (
    ALL_OS_CAPABILITIES as ALL_OS_CAPABILITIES,
)
from rayito._images_publish_domain import (
    BASELINE_MEMORY_ENV_VAR as BASELINE_MEMORY_ENV_VAR,
)
from rayito._images_publish_domain import (
    DEFAULT_IMAGE_NAME as DEFAULT_IMAGE_NAME,
)
from rayito._images_publish_domain import (
    DEFAULT_IMAGE_NAMES as DEFAULT_IMAGE_NAMES,
)
from rayito._images_publish_domain import (
    EFS_BASE_VARIANT as EFS_BASE_VARIANT,
)
from rayito._images_publish_domain import (
    EFS_IMAGE_NAME as EFS_IMAGE_NAME,
)
from rayito._images_publish_domain import (
    HOOKS_PORT as HOOKS_PORT,
)
from rayito._images_publish_domain import (
    IMAGE_HOOKS as IMAGE_HOOKS,
)
from rayito._images_publish_domain import (
    MANAGED_BASE_IMAGE_NAME as MANAGED_BASE_IMAGE_NAME,
)
from rayito._images_publish_domain import (
    base_image_arn as base_image_arn,
)
from rayito._images_publish_domain import (
    default_image_name as default_image_name,
)
from rayito._images_publish_domain import desired_configuration as desired_build_configuration
from rayito._images_publish_domain import log_group_for
from rayito._images_publish_domain import (
    require_efs_combination as require_efs_combination,
)
from rayito._images_publish_domain import (
    sized_settings as sized_settings,
)
from rayito._limits import CDK_MANAGED_BY_TAG_VALUE, MAX_CONCURRENT_IMAGE_BUILDS
from rayito.cli._artifact import marker_has_efs, marker_variant
from rayito.cli._console import client_error_code, echo, emit_json, fail

DEFAULT_STACK_NAME = DEFAULT_IAM_STACK_NAME
OS_CAPABILITY_CHOICES = (ALL_OS_CAPABILITIES,)
S3_KEY_PREFIX = "rayito/images"
#: Segmento de las claves de `--artifact-run-id`: queda bajo `S3_KEY_PREFIX`,
#: así que los permisos `rayito/images/*` de `infra/iam.yaml` y
#: `infra/templates.yaml` las cubren sin cambios.
RUN_SCOPED_KEY_SEGMENT = "runs"
#: Un id de ejecución es un único segmento de clave S3: letras, dígitos y
#: guiones, sin `/` ni `..`, de 1 a 64 caracteres.
RUN_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,63}")
#: ``--base-image-version latest`` publishes on the newest version of the
#: managed base image instead of a pinned one.
LATEST_BASE_IMAGE_VERSION = "latest"
NO_MANAGED_VERSION_MESSAGE = (
    "la base gestionada no lista ninguna versión: pasa --base-image-version con una versión "
    "de list-managed-microvm-image-versions"
)
Sleeper = Callable[[float], None]
Emitter = Callable[[str], None]


class PublishClients(Protocol):
    """What the pipeline needs from `rayito.cli._session.Clients`."""

    @property
    def region(self) -> str: ...

    @property
    def account_id(self) -> str: ...

    @property
    def microvms(self) -> Any: ...

    @property
    def s3(self) -> Any: ...

    @property
    def cloudformation(self) -> Any: ...

    @property
    def logs(self) -> Any: ...


@dataclass(frozen=True)
class PublishSettings:
    artifact: Path
    image_name: str
    variant: str
    bucket: str
    stack_name: str
    build_role_arn: str | None
    base_image_version: str
    memory_mib: int
    force: bool
    timeout_seconds: float
    os_capabilities: str | None = None
    # m15-sizes-catalog: `--env KEY=VALUE` (genérico) más, para una imagen de
    # `--sizes`, `BASELINE_MEMORY_ENV_VAR` ya horneada por `sized_settings`.
    # Vacío (igual que antes de sizes-catalog) no añade `environmentVariables`
    # a la configuración: ver `desired_configuration`.
    environment_variables: Mapping[str, str] = field(default_factory=dict)
    # m15-efs-volumes: `--with-efs`, checked against the artifact's marker
    # by `require_matching_variant`.
    with_efs: bool = False
    # `--artifact-run-id`: `None` keeps the shared content-addressed key.
    artifact_run_id: str | None = None

    @property
    def log_group(self) -> str:
        return log_group_for(self.image_name)


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def without_metadata(response: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in response.items() if key != "ResponseMetadata"}


def validate_run_id(run_id: str) -> str:
    """`run_id` tal cual si es un segmento de clave válido
    (`RUN_ID_PATTERN`); si no, `ValueError`."""
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise ValueError(
            f"--artifact-run-id inválido: {run_id!r} (letras, dígitos y guiones, "
            "de 1 a 64, empezando por letra o dígito)"
        )
    return run_id


def artifact_key(payload: bytes, run_id: str | None = None) -> str:
    """La clave S3 del zip: por contenido (`rayd-<12 hex del sha256>.zip`)
    bajo `S3_KEY_PREFIX`, y con `run_id` además bajo
    `runs/<run_id>/`, para que el objeto sea sólo de esa ejecución."""
    prefix = S3_KEY_PREFIX
    if run_id is not None:
        prefix = f"{prefix}/{RUN_SCOPED_KEY_SEGMENT}/{validate_run_id(run_id)}"
    return f"{prefix}/rayd-{hashlib.sha256(payload).hexdigest()[:12]}.zip"


@dataclass(frozen=True)
class UploadedArtifact:
    """Dónde quedó el zip y si esta invocación lo subió (`False`: ya
    existía y se reutilizó), para que una limpieza borre sólo lo suyo."""

    uri: str
    uploaded: bool


def upload_artifact(
    clients: PublishClients, settings: PublishSettings, emit: Emitter
) -> UploadedArtifact:
    payload = settings.artifact.read_bytes()
    key = artifact_key(payload, settings.artifact_run_id)
    uri = f"s3://{settings.bucket}/{key}"
    uploaded = upload_if_absent(clients, settings.bucket, key, payload, clients.account_id)
    if uploaded:
        emit(f"uploaded {uri} ({len(payload)} bytes)")
    else:
        emit(f"artifact already in S3, skipping upload: {uri}")
    return UploadedArtifact(uri, uploaded)


def build_role_arn(clients: PublishClients, settings: PublishSettings) -> str:
    if settings.build_role_arn:
        return settings.build_role_arn
    stacks = clients.cloudformation.describe_stacks(StackName=settings.stack_name)["Stacks"]
    outputs = stacks[0].get("Outputs", []) if stacks else []
    for output in outputs:
        if output["OutputKey"] == BUILD_ROLE_OUTPUT_KEY:
            return str(output["OutputValue"])
    fail(
        f"stack {settings.stack_name} has no {BUILD_ROLE_OUTPUT_KEY} output; pass --build-role-arn"
    )


def image_arn(clients: PublishClients, image_name: str) -> str:
    return account_image_arn(clients.region, clients.account_id, image_name)


def newest_managed_version(microvms: Any, arn: str) -> tuple[str, datetime] | None:
    """The newest version of the managed base image `arn` (`imageVersion` and
    `createdAt` of ``list-managed-microvm-image-versions``), or ``None`` when
    it lists none."""
    paginator = microvms.get_paginator("list_managed_microvm_image_versions")
    versions = [
        item for page in paginator.paginate(imageIdentifier=arn) for item in page.get("items", [])
    ]
    if not versions:
        return None
    newest = max(versions, key=lambda item: item["createdAt"])
    return str(newest["imageVersion"]), newest["createdAt"]


def resolve_base_image_version(microvms: Any, region: str, requested: str) -> str:
    """The ``baseImageVersion`` to publish on: ``requested`` as given, or, for
    ``latest``, the newest the managed base image lists. The caller prints the
    resolved value so the roll of the managed image is never silent."""
    if requested != LATEST_BASE_IMAGE_VERSION:
        return requested
    newest = newest_managed_version(microvms, base_image_arn(region))
    if newest is None:
        fail(NO_MANAGED_VERSION_MESSAGE)
    return newest[0]


def desired_configuration(
    clients: PublishClients, settings: PublishSettings, artifact_uri: str
) -> dict[str, Any]:
    """The PUT body shared by create and update, minus name and description
    (`_images_publish_domain.desired_configuration` with the account's build
    role)."""
    return desired_build_configuration(
        region=clients.region,
        settings=settings,
        artifact_uri=artifact_uri,
        build_role_arn=build_role_arn(clients, settings),
    )


def submit_build(
    clients: PublishClients,
    settings: PublishSettings,
    arn: str,
    desired: dict[str, Any],
    emit: Emitter,
) -> tuple[str, str]:
    request = {**desired, "description": f"rayd {utc_now()}"}
    submitted = submit_image_build(clients, settings.image_name, arn, request)
    operation = "create" if submitted.created else "update"
    emit(f"{operation}-microvm-image accepted: version {submitted.version}")
    return submitted.arn, submitted.version


def wait_for_gate(
    clients: PublishClients,
    arn: str,
    version: str,
    timeout_seconds: float,
    emit: Emitter,
    sleep: Sleeper,
) -> VersionGate:
    def report(gate: VersionGate, elapsed: float) -> None:
        emit(gate_progress_line(gate, version, elapsed))

    gate, _timed_out = wait_for_image_gate(
        clients, arn, version, timeout_seconds, sleep, on_tick=report
    )
    return gate


def latest_build(clients: PublishClients, arn: str, version: str) -> dict[str, Any]:
    builds = clients.microvms.list_microvm_image_builds(imageIdentifier=arn, imageVersion=version)
    items = builds["items"]
    if not items:
        return {}
    build = clients.microvms.get_microvm_image_build(
        imageIdentifier=arn, imageVersion=version, buildId=items[0]["buildId"]
    )
    return without_metadata(build)


def print_recent_logs(
    clients: PublishClients, group: str, emit: Emitter, streams: int = 3, events: int = 200
) -> None:
    """Build logs are Dockerfile output, by construction never sandbox content."""
    try:
        described = clients.logs.describe_log_streams(
            logGroupName=group, orderBy="LastEventTime", descending=True, limit=streams
        )["logStreams"]
    except ClientError as exc:
        emit(f"no build logs in {group}: {client_error_code(exc)}")
        return
    for stream in described:
        name = stream["logStreamName"]
        page = clients.logs.get_log_events(logGroupName=group, logStreamName=name, limit=events)
        emit(f"===== {group} / {name} ({len(page['events'])} events)")
        for event in page["events"]:
            emit(f"{event['timestamp']} {event['message'].rstrip()}")


def publish_summary(
    arn: str,
    version: str,
    artifact: UploadedArtifact,
    build_seconds: float | None,
    build: dict[str, Any],
    gate: VersionGate | None = None,
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "imageArn": arn,
        "imageVersion": version,
        "artifact": artifact.uri,
        "artifactUploaded": artifact.uploaded,
        "buildSeconds": None if build_seconds is None else round(build_seconds, 1),
        "buildState": build.get("buildState"),
        "chipsetGeneration": build.get("chipsetGeneration"),
        "snapshotBuild": build.get("snapshotBuild", {}),
        "launchable": gate.launchable if gate is not None else True,
    }
    if gate is not None:
        summary["gate"] = gate.as_dict()
        summary["buildStateReason"] = build.get("stateReason")
    return summary


def report_success(summary: dict[str, Any], *, json_output: bool) -> None:
    emit_json(summary)
    if not json_output:
        echo(f"RAYITO_TEMPLATE={summary['imageArn']}")


def size_env_var(size_name: str) -> str:
    """`RAYITO_TEMPLATE_<SIZE>` (p.ej. `RAYITO_TEMPLATE_4GB`): la línea de
    salida en texto plano que `publish_with_sizes` imprime para un tamaño
    adicional. Nunca sustituye a `RAYITO_TEMPLATE`, que siempre nombra el
    baseline (un script que lea la última línea de ese nombre debe seguir
    viendo el baseline, nunca el último tamaño publicado)."""
    return f"RAYITO_TEMPLATE_{size_name.upper()}"


def aggregate_summary(
    baseline: dict[str, Any], sized: Mapping[str, dict[str, Any]]
) -> dict[str, Any]:
    """Un único documento para `--json` (`publish_with_sizes`): los campos
    del baseline en la raíz, igual que sin `--sizes` (quien ya hace
    `summary['imageArn']` sigue funcionando), y cada tamaño adicional bajo
    `sizes[<size>]`. Sin tamaños, exactamente `baseline` (ni siquiera una
    clave `sizes` vacía, para no cambiar la forma del documento de
    `publish()` sin `--sizes`)."""
    return {**baseline, "sizes": dict(sized)} if sized else baseline


def require_matching_variant(settings: PublishSettings) -> None:
    """The marker inside the zip decides what the image is; the flag only
    names it. A mismatch would publish a full build under the slim or poly
    name (or the reverse), so it stops before the first AWS call."""
    if not settings.artifact.is_file():
        fail(f"{settings.artifact} does not exist; run `make image-zip` first")
    found = marker_variant(settings.artifact)
    if found != settings.variant:
        fail(
            f"{settings.artifact} is a {found} artifact but --variant {settings.variant} "
            f"was given; build it with `image_zip.py --variant {settings.variant}`"
        )
    has_efs = marker_has_efs(settings.artifact)
    if has_efs and not settings.with_efs:
        fail(
            f"{settings.artifact} carries the amazon-efs-utils marker; publish it with "
            "--with-efs (or rebuild it without `image_zip.py --with-efs`)"
        )
    if settings.with_efs and not has_efs:
        fail(
            f"--with-efs was given but {settings.artifact} has no amazon-efs-utils marker; "
            "build it with `image_zip.py --with-efs`"
        )
    if settings.with_efs:
        try:
            require_efs_combination(settings.variant, settings.os_capabilities)
        except ValueError as exc:
            fail(str(exc))


def progress_emitter(json_output: bool) -> Emitter:
    """With `--json` stdout carries only the summary document."""
    return lambda message: echo(message, err=json_output)


@dataclass(frozen=True)
class PreparedBuild:
    """Lo común a reusar-o-construir: `arn` y `artifact` no cambian
    entre comprobar si hay una versión que reutilizar y, si no la hay,
    pedir una nueva con la misma `desired`. `existing` es la respuesta de
    `GetMicrovmImage` ya leída (o `None`), para no volver a pedirla."""

    arn: str
    artifact: UploadedArtifact
    desired: dict[str, Any]
    existing: dict[str, Any] | None


def prepare_build(
    clients: PublishClients, settings: PublishSettings, emit: Emitter
) -> PreparedBuild:
    """Rechaza una imagen gestionada por CDK *antes* de cualquier escritura
    (ni siquiera sube el artefacto: spec `agentcore-cdk`, "SHALL fail without
    any write"), sube el artefacto (si hace falta) y arma la configuración
    deseada; no decide si hay que construir, eso es `reusable_version`."""
    arn = image_arn(clients, settings.image_name)
    existing = existing_image(clients, arn)
    refuse_cdk_managed(settings.image_name, existing)
    artifact = upload_artifact(clients, settings, emit)
    desired = desired_configuration(clients, settings, artifact.uri)
    return PreparedBuild(arn, artifact, desired, existing)


def reusable_version(
    clients: PublishClients, settings: PublishSettings, prepared: PreparedBuild
) -> str | None:
    """La versión lanzable más nueva que ya coincide, o `None` si no hay
    ninguna o `--force` la ignora. `settings` sólo aporta `force`: el resto
    ya está en `prepared`."""
    if settings.force or prepared.existing is None:
        return None
    return find_reusable_version(clients, prepared.arn, prepared.desired)


#: Una imagen que gestiona CloudFormation (`RayitoMicrovmImage`, ADR-032 D6),
#: tal cual `testdata/agentcore-cdk/contract.json`
#: (`cliRefusals.imagePublish.message`).
CDK_MANAGED_IMAGE_MESSAGE = (
    "la imagen {name} la gestiona CloudFormation (CDK): cámbiala con cdk deploy"
)
MANAGED_BY_TAG_KEY = "rayito:managed-by"


def refuse_cdk_managed(image_name: str, existing: Mapping[str, Any] | None) -> None:
    """`GetMicrovmImage.tags["rayito:managed-by"] == "rayito-cdk"`: la imagen es
    de un stack de CDK y publicar encima crearía deriva; se para antes de
    `UpdateMicrovmImage`. `rayito image prune` sigue funcionando sobre ella."""
    tags = (existing or {}).get("tags") or {}
    if tags.get(MANAGED_BY_TAG_KEY) == CDK_MANAGED_BY_TAG_VALUE:
        fail(CDK_MANAGED_IMAGE_MESSAGE.format(name=image_name))


def reusable_summary(
    clients: PublishClients, settings: PublishSettings, prepared: PreparedBuild, emit: Emitter
) -> dict[str, Any] | None:
    """El resumen de la versión reutilizable más nueva, o `None` si hay que
    construir una nueva (ver `reusable_version`)."""
    existing = reusable_version(clients, settings, prepared)
    if existing is None:
        return None
    emit(
        f"{settings.image_name}: version {existing} already built from this artifact "
        "and config; reusing"
    )
    build = latest_build(clients, prepared.arn, existing)
    return publish_summary(prepared.arn, existing, prepared.artifact, None, build)


def finalize_build(
    clients: PublishClients,
    settings: PublishSettings,
    arn: str,
    version: str,
    artifact: UploadedArtifact,
    started: float,
    sleep: Sleeper,
    emit: Emitter,
) -> dict[str, Any]:
    """Espera el gate de tres estados de una construcción ya sometida
    (`submit_build`) y arma su resumen; si no quedó lanzable, imprime el
    motivo y la cola de logs (nunca el documento JSON: eso lo decide quien
    agrega resúmenes, `publish`/`publish_with_sizes`)."""
    gate = wait_for_gate(clients, arn, version, settings.timeout_seconds, emit, sleep)
    build_seconds = time.monotonic() - started
    build = latest_build(clients, arn, version)
    summary = publish_summary(arn, version, artifact, build_seconds, build, gate)
    if not gate.launchable:
        emit(
            f"{settings.image_name}: build did not become launchable: "
            f"image={gate.image_state} version={gate.version_state}/{gate.version_status} "
            f"stateReason={gate.state_reason!r} buildStateReason={build.get('stateReason')!r}"
        )
        print_recent_logs(clients, settings.log_group, emit)
    return summary


def build_or_reuse(
    clients: PublishClients, settings: PublishSettings, *, sleep: Sleeper, emit: Emitter
) -> dict[str, Any]:
    """Construye o reutiliza una única versión de `settings.image_name`, sin
    imprimir nunca el documento final: `summary['launchable']` dice si
    quedó lanzable, y `publish`/`publish_with_sizes` deciden cómo informarlo
    (uno a la vez o agregado con otros)."""
    prepared = prepare_build(clients, settings, emit)
    reused = reusable_summary(clients, settings, prepared, emit)
    if reused is not None:
        return reused
    started = time.monotonic()
    arn, version = submit_build(clients, settings, prepared.arn, prepared.desired, emit)
    return finalize_build(clients, settings, arn, version, prepared.artifact, started, sleep, emit)


def publish(
    clients: PublishClients,
    settings: PublishSettings,
    *,
    sleep: Sleeper = time.sleep,
    json_output: bool = False,
) -> int:
    emit = progress_emitter(json_output)
    require_matching_variant(settings)
    summary = build_or_reuse(clients, settings, sleep=sleep, emit=emit)
    if not summary["launchable"]:
        if json_output:
            emit_json(summary)
        return 1
    report_success(summary, json_output=json_output)
    return 0


def publish_sizes(
    clients: PublishClients,
    base_settings: PublishSettings,
    size_names: Sequence[str],
    *,
    sleep: Sleeper = time.sleep,
    json_output: bool = False,
) -> dict[str, dict[str, Any]]:
    """Construye o reutiliza, desde el mismo artefacto que `base_settings`,
    una imagen por cada nombre de `size_names` (nunca el baseline: eso es
    `build_or_reuse(base_settings, ...)`, que `publish`/
    `publish_with_sizes` ya cubren por su cuenta). Cada oleada de a lo sumo
    `MAX_CONCURRENT_IMAGE_BUILDS` construcciones se somete entera antes
    de esperar a que ninguna se asiente, así ``create``/``update-microvm-
    image`` las construye en paralelo. Nunca imprime el documento final:
    devuelve el resumen de cada tamaño (su `['launchable']` dice si quedó
    lanzable), para que `publish_with_sizes` los agregue junto con el del
    baseline en un único documento."""
    emit = progress_emitter(json_output)
    require_matching_variant(base_settings)
    summaries: dict[str, dict[str, Any]] = {}
    for wave_start in range(0, len(size_names), MAX_CONCURRENT_IMAGE_BUILDS):
        wave = size_names[wave_start : wave_start + MAX_CONCURRENT_IMAGE_BUILDS]
        pending: list[tuple[str, PublishSettings, str, str, UploadedArtifact, float]] = []
        for size_name in wave:
            settings = sized_settings(base_settings, size_name)
            prepared = prepare_build(clients, settings, emit)
            reused = reusable_summary(clients, settings, prepared, emit)
            if reused is not None:
                summaries[size_name] = reused
                continue
            started = time.monotonic()
            arn, version = submit_build(clients, settings, prepared.arn, prepared.desired, emit)
            pending.append((size_name, settings, arn, version, prepared.artifact, started))
        for size_name, settings, arn, version, artifact, started in pending:
            summaries[size_name] = finalize_build(
                clients, settings, arn, version, artifact, started, sleep, emit
            )
    return summaries


def publish_with_sizes(
    clients: PublishClients,
    settings: PublishSettings,
    size_names: Sequence[str],
    *,
    sleep: Sleeper = time.sleep,
    json_output: bool = False,
) -> int:
    """`rayito image publish --sizes`: construye o reutiliza el baseline y,
    si quedó lanzable, una imagen adicional por cada tamaño de `size_names`
    (`publish_sizes`, en oleadas paralelas) — pero informa todo en un único
    bloque de salida, nunca uno por imagen (a diferencia de informar cada
    `build_or_reuse` por separado, que dejaría varios documentos/líneas
    `RAYITO_TEMPLATE` en `stdout` y rompería a un script que sólo lee la
    última). Con `--json`, un sólo objeto (ver `aggregate_summary`); en
    texto plano, un único `RAYITO_TEMPLATE=<ARN del baseline>` seguido de un
    `RAYITO_TEMPLATE_<SIZE>=<ARN>` por tamaño adicional (`size_env_var`), y
    sólo si *todas* las imágenes quedaron lanzables — igual que `publish()`
    no imprime `RAYITO_TEMPLATE` en un build fallido, para que un script que
    evalúe o lea la última línea de `stdout` nunca recoja el ARN de una
    imagen no lanzable (code review de PR #76). Sin `size_names` esto nunca
    se llama: `publish()` ya es exactamente este camino sin agregación."""
    emit = progress_emitter(json_output)
    require_matching_variant(settings)
    baseline = build_or_reuse(clients, settings, sleep=sleep, emit=emit)
    sized: dict[str, dict[str, Any]] = {}
    if baseline["launchable"]:
        sized = publish_sizes(clients, settings, size_names, sleep=sleep, json_output=json_output)
    failed = not baseline["launchable"] or any(not s["launchable"] for s in sized.values())
    document = aggregate_summary(baseline, sized)
    if json_output:
        emit_json(document)
    elif not failed:
        echo(f"RAYITO_TEMPLATE={baseline['imageArn']}")
        for size_name, summary in sized.items():
            echo(f"{size_env_var(size_name)}={summary['imageArn']}")
    return 1 if failed else 0
