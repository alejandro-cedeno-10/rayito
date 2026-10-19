"""Adaptadores de `rayito setup` sobre AWS: las pilas de IAM y del bucket (a
través de `OptionalStacks`), la sonda del bucket (`HeadBucket`) y la observación y
espera de `rayito-base` (`_images`). Construirlos no llama a AWS: los
clientes son los perezosos del llamante y `OptionalStacks` no hace nada en
su constructor.

Cada parámetro de AWS usado aquí aparece en `AWS_API_NOTES.md` (§21
CloudFormation, §4 y §27 imágenes, `HeadBucket`) o ya lo usa el SDK
(`BucketRegion` en `rayito doctor`).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any, Final, Protocol

from botocore.exceptions import ClientError

from rayito._account_setup._artifact_stack import ARTIFACT_STACK_COMPONENT
from rayito._account_setup._domain import (
    ARTIFACT_BUCKET_PARAMETER,
    ARTIFACT_STACK_NAME,
    BUCKET_NAME_PARAMETER,
    IAM_STACK_NAME,
    BucketProbe,
    ImageGateResult,
    ImageObservation,
    ImageVersionItem,
)
from rayito._account_setup._iam_stack import IAM_STACK_COMPONENT
from rayito._account_setup._ports import Progress
from rayito._images import (
    DEFAULT_BUILD_TIMEOUT_SECONDS,
    ImageBuildClients,
    Sleeper,
    VersionGate,
    account_image_arn,
    client_error_code,
    gate_progress_line,
    wait_for_gate,
)
from rayito._stacks._condition import StackObservation
from rayito._stacks._model import StackComponent, StackStatus
from rayito._stacks._port import StackProvisioner
from rayito._stacks._service import DEFAULT_WAIT_TIMEOUT_SECONDS, OptionalStacks
from rayito.exceptions import StackException

IMAGE_NOT_FOUND_CODE = "ResourceNotFoundException"
#: Códigos de `HeadBucket` para un bucket que no existe (fila `HeadBucket` de
#: `AWS_API_NOTES.md`).
BUCKET_NOT_FOUND_CODES: Final = frozenset({"404", "NoSuchBucket", "NotFound"})
#: `HeadBucket` contesta 403 si el bucket existe pero las credenciales no lo
#: ven, normalmente porque es de otra cuenta (`AWS_API_NOTES.md`, `HeadBucket`).
BUCKET_FORBIDDEN_CODES: Final = frozenset({"403"})


class CloudFormationSetupStack:
    """Una pila de `rayito setup` (`iam` o `artifact-bucket`) sobre
    `OptionalStacks.status/deploy`: crear y actualizar son el mismo `deploy`,
    que decide por el estado actual y, al actualizar, conserva con
    `UsePreviousValue` todo parámetro que no se manda (sólo se manda el del
    bucket: `ArtifactBucket` o `BucketName`). `tags` (`--tag`) se manda como
    etiquetas de la pila al crearla o actualizarla; CloudFormation las propaga
    a sus recursos."""

    def __init__(
        self,
        provisioner: StackProvisioner,
        *,
        component: StackComponent,
        stack_name: str,
        bucket_parameter: str,
        tags: Mapping[str, str] | None = None,
        wait_timeout_seconds: float = DEFAULT_WAIT_TIMEOUT_SECONDS,
    ) -> None:
        self._provisioner = provisioner
        self._stacks = OptionalStacks(provisioner=provisioner)
        self._component = component
        self._stack_name = stack_name
        self._bucket_parameter = bucket_parameter
        self._tags = dict(tags or {})
        self._wait_timeout = wait_timeout_seconds

    def observe(self) -> StackObservation:
        return _observation(
            self._stacks.status(self._component, stack_name=self._stack_name), self._stack_name
        )

    def create(self, bucket: str) -> StackObservation:
        return self._deploy(bucket)

    def update_bucket(self, bucket: str) -> StackObservation:
        return self._deploy(bucket)

    def wait(self) -> StackObservation:
        self._provisioner.wait(self._stack_name, "deployed", self._wait_timeout)
        observed = self.observe()
        if observed.status is None:
            raise StackException(
                f"la pila {self._stack_name!r} desapareció mientras se esperaba", code="failed"
            )
        return observed

    def _deploy(self, bucket: str) -> StackObservation:
        return _observation(
            self._stacks.deploy(
                self._component,
                stack_name=self._stack_name,
                parameters={self._bucket_parameter: bucket},
                tags=self._tags,
                wait_timeout=self._wait_timeout,
            ),
            self._stack_name,
        )


def iam_stack_gateway(
    provisioner: StackProvisioner,
    *,
    tags: Mapping[str, str] | None = None,
    wait_timeout_seconds: float = DEFAULT_WAIT_TIMEOUT_SECONDS,
) -> CloudFormationSetupStack:
    """La pila `rayito-m0-iam`, que recibe el bucket como `ArtifactBucket`."""
    return CloudFormationSetupStack(
        provisioner,
        component=IAM_STACK_COMPONENT,
        stack_name=IAM_STACK_NAME,
        bucket_parameter=ARTIFACT_BUCKET_PARAMETER,
        tags=tags,
        wait_timeout_seconds=wait_timeout_seconds,
    )


def artifact_stack_gateway(
    provisioner: StackProvisioner,
    *,
    tags: Mapping[str, str] | None = None,
    wait_timeout_seconds: float = DEFAULT_WAIT_TIMEOUT_SECONDS,
) -> CloudFormationSetupStack:
    """La pila `rayito-m0-artifacts`, que recibe el bucket como `BucketName`."""
    return CloudFormationSetupStack(
        provisioner,
        component=ARTIFACT_STACK_COMPONENT,
        stack_name=ARTIFACT_STACK_NAME,
        bucket_parameter=BUCKET_NAME_PARAMETER,
        tags=tags,
        wait_timeout_seconds=wait_timeout_seconds,
    )


def _observation(status: StackStatus | None, stack_name: str) -> StackObservation:
    if status is None:
        return StackObservation(stack_name, None, {}, {})
    return StackObservation(
        name=stack_name,
        status=status.state,
        outputs=dict(status.outputs),
        parameters=dict(status.parameters),
    )


class _BucketClients(Protocol):
    @property
    def s3(self) -> Any: ...

    @property
    def account_id(self) -> str: ...


class S3ArtifactBuckets:
    """`ArtifactBucketGateway`: la cuenta (`GetCallerIdentity`) y una única
    `HeadBucket` por candidato, con sus tres resultados."""

    def __init__(self, clients: _BucketClients) -> None:
        self._clients = clients

    def account_id(self) -> str:
        return self._clients.account_id

    def probe(self, bucket: str) -> BucketProbe:
        try:
            response = self._clients.s3.head_bucket(Bucket=bucket)
        except ClientError as exc:
            code = client_error_code(exc)
            if code in BUCKET_NOT_FOUND_CODES:
                return BucketProbe("missing", None)
            if code in BUCKET_FORBIDDEN_CODES:
                return BucketProbe("forbidden", None)
            raise
        return BucketProbe("found", response.get("BucketRegion") or None)


class MicrovmBaseImages:
    """`BaseImageGateway` sobre `_images`: `observe` sólo lee y `wait`
    sondea la puerta de tres estados de una versión en construcción."""

    def __init__(
        self,
        clients: ImageBuildClients,
        region: str,
        account_id: Callable[[], str],
        *,
        sleep: Sleeper = time.sleep,
    ) -> None:
        self._clients = clients
        self._region = region
        self._account_id = account_id
        self._sleep = sleep

    def observe(self, name: str) -> ImageObservation:
        arn = account_image_arn(self._region, self._account_id(), name)
        microvms = self._clients.microvms
        try:
            image = microvms.get_microvm_image(imageIdentifier=arn)
        except ClientError as exc:
            if client_error_code(exc) == IMAGE_NOT_FOUND_CODE:
                return ImageObservation(name=name, image_arn=arn, state=None, versions=())
            raise
        paginator = microvms.get_paginator("list_microvm_image_versions")
        versions = tuple(
            ImageVersionItem(
                image_version=str(item["imageVersion"]),
                state=str(item["state"]),
                status=str(item["status"]),
                created_at=item["createdAt"],
            )
            for page in paginator.paginate(imageIdentifier=arn)
            for item in page["items"]
        )
        return ImageObservation(
            name=name, image_arn=arn, state=str(image["state"]), versions=versions
        )

    def wait(self, image_arn: str, image_version: str, on_progress: Progress) -> ImageGateResult:
        def report(gate: VersionGate, elapsed: float) -> None:
            on_progress(gate_progress_line(gate, image_version, elapsed))

        gate, _timed_out = wait_for_gate(
            self._clients,
            image_arn,
            image_version,
            DEFAULT_BUILD_TIMEOUT_SECONDS,
            self._sleep,
            on_tick=report,
        )
        return ImageGateResult(launchable=gate.launchable, version_state=gate.version_state)
