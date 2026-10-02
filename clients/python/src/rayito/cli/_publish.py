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
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from botocore.exceptions import ClientError

from rayito._images import (
    DEFAULT_BASE_IMAGE_NAME,
    LOG_GROUP_PREFIX,
    VersionGate,
    account_image_arn,
    find_reusable_version,
    image_exists,
    submit_image_build,
    upload_if_absent,
)
from rayito._images import wait_for_gate as wait_for_image_gate
from rayito.cli._artifact import VARIANTS, marker_variant
from rayito.cli._console import client_error_code, echo, emit_json, fail

DEFAULT_IMAGE_NAME = DEFAULT_BASE_IMAGE_NAME
DEFAULT_IMAGE_NAMES = {
    "full": DEFAULT_IMAGE_NAME,
    "slim": f"{DEFAULT_IMAGE_NAME}-slim",
    "poly": f"{DEFAULT_IMAGE_NAME}-poly",
}
DEFAULT_STACK_NAME = "rayito-m0-iam"
OS_CAPABILITY_CHOICES = ("ALL",)
BUILD_ROLE_OUTPUT_KEY = "BuildRoleArn"
S3_KEY_PREFIX = "rayito/images"
HOOKS_PORT = 9000
MANAGED_BASE_IMAGE_NAME = "al2023-1"

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

    @property
    def log_group(self) -> str:
        return f"{LOG_GROUP_PREFIX}/{self.image_name}"


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


def upload_artifact(clients: PublishClients, settings: PublishSettings, emit: Emitter) -> str:
    payload = settings.artifact.read_bytes()
    key = artifact_key(payload)
    uri = f"s3://{settings.bucket}/{key}"
    if upload_if_absent(clients, settings.bucket, key, payload):
        emit(f"uploaded {uri} ({len(payload)} bytes)")
    else:
        emit(f"artifact already in S3, skipping upload: {uri}")
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
    return account_image_arn(clients.region, clients.account_id, image_name)


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
    return configuration


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
        emit(
            f"  image={gate.image_state} version={version} state={gate.version_state} "
            f"status={gate.version_status} ({elapsed:.0f}s)"
        )

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


def fail_build(
    clients: PublishClients,
    settings: PublishSettings,
    summary: dict[str, Any],
    gate: VersionGate,
    build: dict[str, Any],
    emit: Emitter,
    *,
    json_output: bool,
) -> int:
    emit(
        f"build did not become launchable: image={gate.image_state} "
        f"version={gate.version_state}/{gate.version_status} "
        f"stateReason={gate.state_reason!r} buildStateReason={build.get('stateReason')!r}"
    )
    print_recent_logs(clients, settings.log_group, emit)
    if json_output:
        emit_json(summary)
    return 1


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


def publish(
    clients: PublishClients,
    settings: PublishSettings,
    *,
    sleep: Sleeper = time.sleep,
    json_output: bool = False,
) -> int:
    emit = progress_emitter(json_output)
    require_matching_variant(settings)
    arn = image_arn(clients, settings.image_name)
    artifact_uri = upload_artifact(clients, settings, emit)
    desired = desired_configuration(clients, settings, artifact_uri)
    if not settings.force and image_exists(clients, arn):
        existing = find_reusable_version(clients, arn, desired)
        if existing:
            emit(f"version {existing} already built from this artifact and config; reusing")
            build = latest_build(clients, arn, existing)
            report_success(
                publish_summary(arn, existing, artifact_uri, None, build), json_output=json_output
            )
            return 0
    started = time.monotonic()
    arn, version = submit_build(clients, settings, arn, desired, emit)
    gate = wait_for_gate(clients, arn, version, settings.timeout_seconds, emit, sleep)
    build_seconds = time.monotonic() - started
    build = latest_build(clients, arn, version)
    summary = publish_summary(arn, version, artifact_uri, build_seconds, build, gate)
    if not gate.launchable:
        return fail_build(clients, settings, summary, gate, build, emit, json_output=json_output)
    report_success(summary, json_output=json_output)
    return 0
