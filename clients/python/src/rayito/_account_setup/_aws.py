"""Adaptadores de `rayito setup` sobre AWS: la pila de IAM (a través de
`OptionalStacks`), la sonda del bucket (`HeadBucket`) y la observación y
espera de `rayito-base` (`_images`). Construirlos no llama a AWS: los
clientes son los perezosos del llamante y `OptionalStacks` no hace nada en
su constructor.

Cada parámetro de AWS usado aquí aparece en `AWS_API_NOTES.md` (§21
CloudFormation, §4 y §27 imágenes) o ya lo usa el SDK (`HeadBucket` y su
`BucketRegion` en `rayito doctor`).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, Protocol

from botocore.exceptions import ClientError

from rayito._account_setup._domain import (
    ARTIFACT_BUCKET_PARAMETER,
    IAM_STACK_NAME,
    BucketProbe,
    ImageGateResult,
    ImageObservation,
    ImageVersionItem,
    StackObservation,
)
from rayito._account_setup._iam_stack import IAM_STACK_COMPONENT
from rayito._account_setup._ports import Progress
from rayito._agent._aws_resolve import BUCKET_MISSING_CODES
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
from rayito._stacks._model import StackStatus
from rayito._stacks._port import StackProvisioner
from rayito._stacks._service import DEFAULT_WAIT_TIMEOUT_SECONDS, OptionalStacks
from rayito.exceptions import StackException

IMAGE_NOT_FOUND_CODE = "ResourceNotFoundException"


class CloudFormationIamStack:
    """`IamStackGateway` sobre `OptionalStacks.status/deploy`: crear y
    actualizar son el mismo `deploy`, que decide por el estado actual y, al
    actualizar, conserva con `UsePreviousValue` todo parámetro que no se
    manda (sólo se manda `ArtifactBucket`)."""

    def __init__(
        self,
        provisioner: StackProvisioner,
        *,
        wait_timeout_seconds: float = DEFAULT_WAIT_TIMEOUT_SECONDS,
    ) -> None:
        self._provisioner = provisioner
        self._stacks = OptionalStacks(provisioner=provisioner)
        self._wait_timeout = wait_timeout_seconds

    def observe(self) -> StackObservation:
        return _observation(self._stacks.status(IAM_STACK_COMPONENT, stack_name=IAM_STACK_NAME))

    def create(self, bucket: str) -> StackObservation:
        return self._deploy(bucket)

    def update_bucket(self, bucket: str) -> StackObservation:
        return self._deploy(bucket)

    def wait(self) -> StackObservation:
        self._provisioner.wait(IAM_STACK_NAME, "deployed", self._wait_timeout)
        observed = self.observe()
        if observed.status is None:
            raise StackException(
                f"la pila {IAM_STACK_NAME!r} desapareció mientras se esperaba", code="failed"
            )
        return observed

    def _deploy(self, bucket: str) -> StackObservation:
        return _observation(
            self._stacks.deploy(
                IAM_STACK_COMPONENT,
                stack_name=IAM_STACK_NAME,
                parameters={ARTIFACT_BUCKET_PARAMETER: bucket},
                wait_timeout=self._wait_timeout,
            )
        )


def _observation(status: StackStatus | None) -> StackObservation:
    if status is None:
        return StackObservation(IAM_STACK_NAME, None, {}, {})
    return StackObservation(
        name=IAM_STACK_NAME,
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
    `HeadBucket` por candidato."""

    def __init__(self, clients: _BucketClients) -> None:
        self._clients = clients

    def account_id(self) -> str:
        return self._clients.account_id

    def probe(self, bucket: str) -> BucketProbe:
        try:
            response = self._clients.s3.head_bucket(Bucket=bucket)
        except ClientError as exc:
            if client_error_code(exc) in BUCKET_MISSING_CODES:
                return BucketProbe(exists=False, region=None)
            raise
        return BucketProbe(exists=True, region=response.get("BucketRegion") or None)


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
