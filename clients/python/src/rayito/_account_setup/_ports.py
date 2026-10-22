"""Puertos de `rayito setup` (ADR-030): lo que el servicio `AccountSetup`
necesita del mundo exterior, como `Protocol`s. Los adaptadores viven en
`_aws.py`, `_release.py` y `cli/_setup_publisher.py`; los tests usan fakes
en memoria (`tests/unit/fake_account_setup.py`)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from rayito._account_setup._domain import (
    BucketProbe,
    DownloadedRelease,
    ImageGateResult,
    ImageObservation,
    PublishedImage,
)
from rayito._stacks._condition import StackObservation

Progress = Callable[[str], None]


class IamStackGateway(Protocol):
    def observe(self) -> StackObservation: ...

    def create(self, bucket: str) -> StackObservation: ...

    def update_bucket(self, bucket: str) -> StackObservation: ...

    def wait(self) -> StackObservation: ...


class ArtifactStackGateway(Protocol):
    """La pila `rayito-m0-artifacts`. Sin `update`: el nombre del bucket de la
    pila no cambia nunca."""

    def observe(self) -> StackObservation: ...

    def create(self, bucket: str) -> StackObservation: ...

    def wait(self) -> StackObservation: ...


class ArtifactBucketGateway(Protocol):
    def account_id(self) -> str: ...

    def probe(self, bucket: str) -> BucketProbe: ...


class BaseImageGateway(Protocol):
    def observe(self, name: str) -> ImageObservation: ...

    def wait(
        self, image_arn: str, image_version: str, on_progress: Progress
    ) -> ImageGateResult: ...


class BaseImagePublisher(Protocol):
    """Publica la imagen `name` desde el zip `artifact`; `os_capabilities`
    es el valor de `additionalOsCapabilities` (`"ALL"` para
    `rayito-base-caps`) o `None`."""

    def publish(
        self,
        artifact: Path,
        *,
        name: str,
        os_capabilities: str | None,
        bucket: str,
        build_role_arn: str,
        on_progress: Progress,
    ) -> PublishedImage: ...


class ReleaseArtifactSource(Protocol):
    def fetch(self, tag: str, directory: Path, on_progress: Progress) -> DownloadedRelease: ...


@dataclass(frozen=True)
class AccountSetupGateways:
    iam_stack: IamStackGateway
    artifact_stack: ArtifactStackGateway
    buckets: ArtifactBucketGateway
    images: BaseImageGateway
    publisher: BaseImagePublisher
    releases: ReleaseArtifactSource
