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

``--os-capabilities ALL`` adds ``additionalOsCapabilities: ["ALL"]`` (the only
value the service model accepts) and nothing else to the image configuration:
with it ``rayd`` installs the IMDS block for uid 1000 at boot. Publish it under
its own name (``make image-publish-caps`` uses ``rayito-base-caps``) so
``rayito-base``'s version history stays homogeneous; a configuration that
differs by this key never reuses a default version.

``--base-image-version`` is required: every publish names the managed base
image version explicitly (``baseImageVersion`` of create/update-microvm-image,
the ``imageVersion`` that ``list-managed-microvm-image-versions`` returns), so a
silent roll of the managed image cannot change what a build runs on. The API
accepts ``1`` but ``list-microvm-image-versions`` echoes the normalised ``1.0``
(AWS_API_NOTES.md Q52), so the reuse check compares ``baseImageVersion``
numerically and every other key exactly.

There is no default bucket: ``--bucket`` or ``RAYITO_BUCKET`` name the
artifact bucket of the account that publishes.

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
included — the minimal, behavior-preserving seam this change needs
(``PublishSettings.environment_variables``), not the full
``ImageBuildGateway``/``_images.py`` extraction the M15 architecture names
and a future change may still do (tasks.md §11); it is never an activation
switch (ADR-014 rule 4): it only configures the image's own guest
environment, nothing about what the SDK does at ``Sandbox.create()``. A
``--env``/``--sizes`` publish only reuses a version whose
``environmentVariables`` match exactly, read with
``get-microvm-image-version`` because ``list-microvm-image-versions`` never
echoes them (Q118, ``echoed_environment_variables``). Without ``--env`` the
reuse check is 0.5.x's, list-only and with no extra call: a version built
earlier *with* ``--env`` under the same name and artifact is then reused as
is; pass ``--force`` to rebuild it without the variables.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol

from botocore.exceptions import ClientError

from rayito._sizing import BASELINE_MEMORY_MIB, NAME_TO_MEMORY_MIB
from rayito.cli._artifact import VARIANTS, marker_variant
from rayito.cli._console import client_error_code, echo, emit_json, fail

DEFAULT_IMAGE_NAME = "rayito-base"
DEFAULT_IMAGE_NAMES = {
    "full": DEFAULT_IMAGE_NAME,
    "slim": f"{DEFAULT_IMAGE_NAME}-slim",
    "poly": f"{DEFAULT_IMAGE_NAME}-poly",
}
DEFAULT_STACK_NAME = "rayito-m0-iam"
# El mismo catálogo cerrado que `_sizing.BASELINE_MEMORY_MIB`: la imagen sin
# sufijo de `--sizes` siempre es esta, verificado por
# `test_default_memory_mib_matches_the_sizing_baseline`.
DEFAULT_MEMORY_MIB = BASELINE_MEMORY_MIB
OS_CAPABILITY_CHOICES = ("ALL",)
BUILD_ROLE_OUTPUT_KEY = "BuildRoleArn"
S3_KEY_PREFIX = "rayito/images"
LOG_GROUP_PREFIX = "/rayito"
HOOKS_PORT = 9000
POLL_INTERVAL_SECONDS = 10.0
DEFAULT_BUILD_TIMEOUT_SECONDS = 1800.0
LAUNCHABLE_IMAGE_STATES = frozenset({"CREATED", "UPDATED"})
FAILED_IMAGE_STATES = frozenset({"CREATE_FAILED", "UPDATE_FAILED"})
SETTLED_VERSION_STATES = frozenset({"SUCCESSFUL", "FAILED"})
MISSING_OBJECT_CODES = frozenset({"404", "NoSuchKey", "NotFound", "403"})
BASE_IMAGE_VERSION_KEY = "baseImageVersion"
MANAGED_BASE_IMAGE_NAME = "al2023-1"
# Nombre de la variable de imagen que `--sizes` hornea: información
# declarada para que el código del usuario sepa el baseline de la imagen
# (Q88) sin llamar a AWS; `rayd` no la lee hoy (0.6), ver el docstring del
# módulo y `sized_settings`.
BASELINE_MEMORY_ENV_VAR = "RAYITO_BASELINE_MEMORY_MIB"
# Oleadas de construcción simultánea de `--sizes`: al menos 10 builds en
# paralelo no degradan el servicio (Q83, docs/research/2026-10-e2b-out-of-
# scope.md; compartido con el límite de concurrencia de m15-templates, que
# mide lo mismo del lado de `CreateMicrovmImage`). El catálogo cerrado de
# tamaños tiene como mucho 4 sufijos además del baseline, así que hoy esto
# nunca forma una segunda oleada; se implementa igual porque el límite es
# del servicio, no de este catálogo.
MAX_CONCURRENT_IMAGE_BUILDS_Q83 = 10

IMAGE_HOOKS: dict[str, Any] = {
    "port": HOOKS_PORT,
    "microvmImageHooks": {
        "ready": "ENABLED",
        "readyTimeoutInSeconds": 600,
        "validate": "ENABLED",
        "validateTimeoutInSeconds": 600,
    },
    "microvmHooks": {
        "run": "ENABLED",
        "runTimeoutInSeconds": 30,
        "resume": "ENABLED",
        "resumeTimeoutInSeconds": 30,
        "suspend": "ENABLED",
        "suspendTimeoutInSeconds": 30,
        "terminate": "ENABLED",
        "terminateTimeoutInSeconds": 10,
    },
}

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

    @property
    def log_group(self) -> str:
        return f"{LOG_GROUP_PREFIX}/{self.image_name}"


@dataclass(frozen=True)
class VersionGate:
    """The three independent states that must all pass before ``run-microvm``."""

    image_state: str
    version_state: str
    version_status: str
    state_reason: str | None

    @property
    def launchable(self) -> bool:
        return (
            self.image_state in LAUNCHABLE_IMAGE_STATES
            and self.version_state == "SUCCESSFUL"
            and self.version_status == "ACTIVE"
        )

    @property
    def settled(self) -> bool:
        version_done = self.version_state in SETTLED_VERSION_STATES
        image_done = self.image_state in LAUNCHABLE_IMAGE_STATES | FAILED_IMAGE_STATES
        return (version_done and image_done) or self.version_state == "FAILED"

    def as_dict(self) -> dict[str, Any]:
        return {
            "imageState": self.image_state,
            "versionState": self.version_state,
            "versionStatus": self.version_status,
            "stateReason": self.state_reason,
            "launchable": self.launchable,
        }


def default_image_name(variant: str) -> str:
    if variant not in VARIANTS:
        raise ValueError(f"variant desconocida: {variant!r} (admitidas: {', '.join(VARIANTS)})")
    return DEFAULT_IMAGE_NAMES[variant]


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def without_metadata(response: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in response.items() if key != "ResponseMetadata"}


def artifact_key(payload: bytes) -> str:
    return f"{S3_KEY_PREFIX}/rayd-{hashlib.sha256(payload).hexdigest()[:12]}.zip"


def object_exists(clients: PublishClients, bucket: str, key: str) -> bool:
    """A 403 also counts as absent: without ``s3:ListBucket`` S3 answers 403
    instead of 404 to a HEAD of a missing key, and a permission that is really
    missing surfaces on the ``put_object`` that follows."""
    try:
        clients.s3.head_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        if client_error_code(exc) in MISSING_OBJECT_CODES:
            return False
        raise
    return True


def upload_artifact(clients: PublishClients, settings: PublishSettings, emit: Emitter) -> str:
    payload = settings.artifact.read_bytes()
    key = artifact_key(payload)
    uri = f"s3://{settings.bucket}/{key}"
    if object_exists(clients, settings.bucket, key):
        emit(f"artifact already in S3, skipping upload: {uri}")
    else:
        clients.s3.put_object(Bucket=settings.bucket, Key=key, Body=payload)
        emit(f"uploaded {uri} ({len(payload)} bytes)")
    return uri


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
    return f"arn:aws:lambda:{clients.region}:{clients.account_id}:microvm-image:{image_name}"


def base_image_arn(region: str) -> str:
    return f"arn:aws:lambda:{region}:aws:microvm-image:{MANAGED_BASE_IMAGE_NAME}"


def desired_configuration(
    clients: PublishClients, settings: PublishSettings, artifact_uri: str
) -> dict[str, Any]:
    """The PUT body shared by create and update, minus name and description."""
    configuration: dict[str, Any] = {
        "baseImageArn": base_image_arn(clients.region),
        "baseImageVersion": settings.base_image_version,
        "buildRoleArn": build_role_arn(clients, settings),
        "codeArtifact": {"uri": artifact_uri},
        "resources": [{"minimumMemoryInMiB": settings.memory_mib}],
        "cpuConfigurations": [{"architecture": "ARM_64"}],
        "hooks": IMAGE_HOOKS,
        "logging": {"cloudWatch": {"logGroup": settings.log_group}},
    }
    if settings.os_capabilities:
        configuration["additionalOsCapabilities"] = [settings.os_capabilities]
    if settings.environment_variables:
        configuration["environmentVariables"] = dict(settings.environment_variables)
    return configuration


def image_exists(clients: PublishClients, arn: str) -> bool:
    try:
        clients.microvms.get_microvm_image(imageIdentifier=arn)
    except ClientError as exc:
        if client_error_code(exc) == "ResourceNotFoundException":
            return False
        raise
    return True


def base_image_version_matches(echoed: Any, desired: Any) -> bool:
    """``1`` and ``1.0`` name the same managed version: the API accepts the
    ``list-managed-microvm-image-versions`` spelling and echoes the
    normalised one (AWS_API_NOTES.md Q52). Non-numeric spellings only match
    exactly."""
    if not isinstance(echoed, str) or not isinstance(desired, str):
        return bool(echoed == desired)
    if echoed == desired:
        return True
    try:
        return Decimal(echoed) == Decimal(desired)
    except InvalidOperation:
        return False


def value_matches(key: str, echoed: Any, desired: Any) -> bool:
    if key == BASE_IMAGE_VERSION_KEY:
        return base_image_version_matches(echoed, desired)
    return bool(echoed == desired)


ENVIRONMENT_VARIABLES_KEY = "environmentVariables"


def environment_variables_match(echoed: Any, desired: Mapping[str, str]) -> bool:
    """`environmentVariables` se compara siempre contra lo que de verdad se
    quiere ahora (`desired`, que es `settings.environment_variables`, nunca
    el dict `desired_configuration` arma para la petición: ese sólo lleva la
    clave cuando no está vacío, para que la petición a AWS siga siendo byte
    a byte la de antes de sizes-catalog). Una versión sin la clave (nunca
    tuvo `--env`, o viene de `list-microvm-image-versions`, que nunca la
    devuelve: Q118) cuenta como `{}`, igual que un `desired` vacío. Quién
    lee de verdad `echoed`: `echoed_environment_variables`."""
    return dict(echoed or {}) == dict(desired)


def settings_match(version: dict[str, Any], desired: dict[str, Any]) -> bool:
    """Every key of `desired` except `environmentVariables`, which
    `configuration_matches`/`published_version` compare on their own."""
    return all(
        value_matches(key, version.get(key), value)
        for key, value in desired.items()
        if key != ENVIRONMENT_VARIABLES_KEY
    )


def configuration_matches(
    version: dict[str, Any],
    desired: dict[str, Any],
    environment_variables: Mapping[str, str] | None = None,
) -> bool:
    if not environment_variables_match(
        version.get(ENVIRONMENT_VARIABLES_KEY), environment_variables or {}
    ):
        return False
    return settings_match(version, desired)


def echoed_environment_variables(
    clients: PublishClients,
    arn: str,
    item: dict[str, Any],
    environment_variables: Mapping[str, str],
) -> Any:
    """The `environmentVariables` a listed version was built with.
    `list-microvm-image-versions` never echoes the key, only
    `get-microvm-image-version` does (Q118, AWS_API_NOTES.md §24): without
    this every `--env`/`--sizes` publish (sized images always bake
    `RAYITO_BASELINE_MEMORY_MIB`) rebuilt instead of reusing. The extra
    call (no `apiTps` quota of its own) only happens when this publish
    asks for variables; without `--env` the list item is trusted as is,
    the exact reuse check (and calls) of 0.5.x."""
    if not environment_variables:
        return item.get(ENVIRONMENT_VARIABLES_KEY)
    response = clients.microvms.get_microvm_image_version(
        imageIdentifier=arn, imageVersion=str(item["imageVersion"])
    )
    return response.get(ENVIRONMENT_VARIABLES_KEY)


def published_version(
    clients: PublishClients,
    arn: str,
    desired: dict[str, Any],
    environment_variables: Mapping[str, str] | None = None,
) -> str | None:
    """Newest launchable version already built from this artifact and config:
    candidates matching every other key are checked newest first, so a
    `--env` publish confirms its variables with as few
    `get-microvm-image-version` calls as possible (see
    `echoed_environment_variables`)."""
    wanted = environment_variables or {}
    paginator = clients.microvms.get_paginator("list_microvm_image_versions")
    candidates = sorted(
        (
            item
            for page in paginator.paginate(imageIdentifier=arn)
            for item in page["items"]
            if item["state"] == "SUCCESSFUL"
            and item["status"] == "ACTIVE"
            and settings_match(item, desired)
        ),
        key=lambda item: item["createdAt"],
        reverse=True,
    )
    for item in candidates:
        echoed = echoed_environment_variables(clients, arn, item, wanted)
        if environment_variables_match(echoed, wanted):
            return str(item["imageVersion"])
    return None


def submit_build(
    clients: PublishClients,
    settings: PublishSettings,
    arn: str,
    desired: dict[str, Any],
    emit: Emitter,
) -> tuple[str, str]:
    request = {**desired, "description": f"rayd {utc_now()}"}
    if image_exists(clients, arn):
        response = clients.microvms.update_microvm_image(imageIdentifier=arn, **request)
        emit(f"update-microvm-image accepted: version {response['imageVersion']}")
    else:
        response = clients.microvms.create_microvm_image(name=settings.image_name, **request)
        emit(f"create-microvm-image accepted: version {response['imageVersion']}")
    return str(response["imageArn"]), str(response["imageVersion"])


def read_gate(clients: PublishClients, arn: str, version: str) -> VersionGate:
    image = clients.microvms.get_microvm_image(imageIdentifier=arn)
    detail = clients.microvms.get_microvm_image_version(imageIdentifier=arn, imageVersion=version)
    return VersionGate(
        image_state=str(image["state"]),
        version_state=str(detail["state"]),
        version_status=str(detail["status"]),
        state_reason=detail.get("stateReason"),
    )


def wait_for_gate(
    clients: PublishClients,
    arn: str,
    version: str,
    timeout_seconds: float,
    emit: Emitter,
    sleep: Sleeper,
) -> VersionGate:
    started = time.monotonic()
    while True:
        gate = read_gate(clients, arn, version)
        elapsed = time.monotonic() - started
        emit(
            f"  image={gate.image_state} version={version} state={gate.version_state} "
            f"status={gate.version_status} ({elapsed:.0f}s)"
        )
        if gate.settled or elapsed >= timeout_seconds:
            return gate
        sleep(POLL_INTERVAL_SECONDS)


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
    artifact_uri: str,
    build_seconds: float | None,
    build: dict[str, Any],
    gate: VersionGate | None = None,
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "imageArn": arn,
        "imageVersion": version,
        "artifact": artifact_uri,
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


def progress_emitter(json_output: bool) -> Emitter:
    """With `--json` stdout carries only the summary document."""
    return lambda message: echo(message, err=json_output)


@dataclass(frozen=True)
class PreparedBuild:
    """Lo común a reusar-o-construir: `arn` y `artifact_uri` no cambian
    entre comprobar si hay una versión que reutilizar y, si no la hay,
    pedir una nueva con la misma `desired`."""

    arn: str
    artifact_uri: str
    desired: dict[str, Any]


def prepare_build(
    clients: PublishClients, settings: PublishSettings, emit: Emitter
) -> PreparedBuild:
    """Sube el artefacto (si hace falta) y arma la configuración deseada;
    no decide si hay que construir, eso es `reusable_version`."""
    arn = image_arn(clients, settings.image_name)
    artifact_uri = upload_artifact(clients, settings, emit)
    return PreparedBuild(arn, artifact_uri, desired_configuration(clients, settings, artifact_uri))


def reusable_version(
    clients: PublishClients, settings: PublishSettings, prepared: PreparedBuild
) -> str | None:
    """La versión lanzable más nueva que ya coincide, o `None` si no hay
    ninguna o `--force` la ignora. `settings` sólo aporta `force`: el resto
    ya está en `prepared`."""
    if settings.force or not image_exists(clients, prepared.arn):
        return None
    return published_version(
        clients, prepared.arn, prepared.desired, settings.environment_variables
    )


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
    return publish_summary(prepared.arn, existing, prepared.artifact_uri, None, build)


def finalize_build(
    clients: PublishClients,
    settings: PublishSettings,
    arn: str,
    version: str,
    artifact_uri: str,
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
    summary = publish_summary(arn, version, artifact_uri, build_seconds, build, gate)
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
    return finalize_build(
        clients, settings, arn, version, prepared.artifact_uri, started, sleep, emit
    )


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


def sized_settings(base: PublishSettings, size_name: str) -> PublishSettings:
    """`base` con el sufijo de tamaño aplicado (m15-sizes-catalog):
    `image_name` lleva `-<size_name>`, `memory_mib` es el valor del
    catálogo cerrado y `environment_variables` gana `BASELINE_MEMORY_ENV_VAR`
    horneado: información declarada para que el código del usuario (no
    `rayd`, que hoy no la lee) sepa el baseline de la imagen sin llamar a
    AWS (Q88: el guest ve hasta 4x más en `free -m`). `size_name` ya viene
    validado contra `_sizing.SIZE_NAMES` por la CLI."""
    memory_mib = NAME_TO_MEMORY_MIB[size_name]
    return replace(
        base,
        image_name=f"{base.image_name}-{size_name}",
        memory_mib=memory_mib,
        environment_variables={
            **base.environment_variables,
            BASELINE_MEMORY_ENV_VAR: str(memory_mib),
        },
    )


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
    `MAX_CONCURRENT_IMAGE_BUILDS_Q83` construcciones se somete entera antes
    de esperar a que ninguna se asiente, así ``create``/``update-microvm-
    image`` las construye en paralelo. Nunca imprime el documento final:
    devuelve el resumen de cada tamaño (su `['launchable']` dice si quedó
    lanzable), para que `publish_with_sizes` los agregue junto con el del
    baseline en un único documento."""
    emit = progress_emitter(json_output)
    require_matching_variant(base_settings)
    summaries: dict[str, dict[str, Any]] = {}
    for wave_start in range(0, len(size_names), MAX_CONCURRENT_IMAGE_BUILDS_Q83):
        wave = size_names[wave_start : wave_start + MAX_CONCURRENT_IMAGE_BUILDS_Q83]
        pending: list[tuple[str, PublishSettings, str, str, str, float]] = []
        for size_name in wave:
            settings = sized_settings(base_settings, size_name)
            prepared = prepare_build(clients, settings, emit)
            reused = reusable_summary(clients, settings, prepared, emit)
            if reused is not None:
                summaries[size_name] = reused
                continue
            started = time.monotonic()
            arn, version = submit_build(clients, settings, prepared.arn, prepared.desired, emit)
            pending.append((size_name, settings, arn, version, prepared.artifact_uri, started))
        for size_name, settings, arn, version, artifact_uri, started in pending:
            summaries[size_name] = finalize_build(
                clients, settings, arn, version, artifact_uri, started, sleep, emit
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
