"""Los cinco puertos de `AccountSetup` en memoria (ADR-030): registran cada
llamada y se construyen desde el `input` de `testdata/account-setup/*.json`
(el mismo formato en `plan-vectors.json` y `cli-cases.json`). Sin AWS, sin
red y sin disco fuera del directorio que se les da."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from rayito._account_setup._domain import (
    CHECKSUMS_ASSET_NAME,
    IMAGE_ASSET_NAME,
    BucketProbe,
    DownloadedRelease,
    ImageGateResult,
    ImageObservation,
    ImageVersionItem,
    PublishedImage,
    StackObservation,
    cdk_bucket_name,
)
from rayito._account_setup._ports import AccountSetupGateways, Progress
from rayito._images import account_image_arn

TESTDATA: Final = Path(__file__).resolve().parents[4] / "testdata" / "account-setup"
ACCOUNT: Final = "123456789012"
REGION: Final = "us-east-1"
BUILD_ROLE_ARN: Final = f"arn:aws:iam::{ACCOUNT}:role/rayito-m0-build-{REGION}"
IMAGE_VERSION_PUBLISHED: Final = "3.0"
ARTIFACT_BYTES: Final = b"image zip"
ARTIFACT_SHA256: Final = hashlib.sha256(ARTIFACT_BYTES).hexdigest()

Call = tuple[str, ...]


def load_contract() -> dict[str, Any]:
    return dict(json.loads((TESTDATA / "contract.json").read_text(encoding="utf-8")))


def load_cases(name: str) -> list[dict[str, Any]]:
    document = json.loads((TESTDATA / name).read_text(encoding="utf-8"))
    return list(document["cases"])


def fill_placeholders(value: Any, *, account: str, region: str, sdk_version: str) -> Any:
    """Sustituye `{sdkVersion}` y `{cdkBucket}` en cadenas, claves y listas."""
    if isinstance(value, str):
        return value.replace("{sdkVersion}", sdk_version).replace(
            "{cdkBucket}", cdk_bucket_name(account, region)
        )
    if isinstance(value, list):
        return [
            fill_placeholders(item, account=account, region=region, sdk_version=sdk_version)
            for item in value
        ]
    if isinstance(value, dict):
        return {
            fill_placeholders(
                key, account=account, region=region, sdk_version=sdk_version
            ): fill_placeholders(item, account=account, region=region, sdk_version=sdk_version)
            for key, item in value.items()
        }
    return value


def stack_from_vector(stack: Mapping[str, Any] | None, name: str) -> StackObservation:
    if stack is None:
        return StackObservation(name, None, {}, {})
    return StackObservation(
        name, stack["status"], dict(stack["outputs"]), dict(stack["parameters"])
    )


def image_from_vector(image: Mapping[str, Any] | None, name: str, arn: str) -> ImageObservation:
    if image is None:
        return ImageObservation(name, arn, None, ())
    versions = tuple(
        ImageVersionItem(
            image_version=item["imageVersion"],
            state=item["state"],
            status=item["status"],
            created_at=datetime.fromisoformat(item["createdAt"]),
        )
        for item in image["versions"]
    )
    return ImageObservation(name, arn, image["state"], versions)


@dataclass
class Recorder:
    calls: list[Call] = field(default_factory=list)

    def record(self, *call: str) -> None:
        self.calls.append(call)

    def names(self) -> list[str]:
        return [call[0] for call in self.calls]


@dataclass
class FakeIamStack:
    recorder: Recorder
    stack: StackObservation
    created_outputs: dict[str, str] = field(
        default_factory=lambda: {"BuildRoleArn": BUILD_ROLE_ARN}
    )

    def observe(self) -> StackObservation:
        self.recorder.record("describe")
        return self.stack

    def create(self, bucket: str) -> StackObservation:
        self.recorder.record("create", bucket)
        return self._complete(bucket, "CREATE_COMPLETE")

    def update_bucket(self, bucket: str) -> StackObservation:
        self.recorder.record("update", bucket)
        return self._complete(bucket, "UPDATE_COMPLETE")

    def wait(self) -> StackObservation:
        self.recorder.record("wait")
        bucket = self.stack.parameters.get("ArtifactBucket", "")
        return self._complete(bucket, "CREATE_COMPLETE")

    def _complete(self, bucket: str, status: str) -> StackObservation:
        self.stack = StackObservation(
            self.stack.name,
            status,
            dict(self.created_outputs),
            {**self.stack.parameters, "ArtifactBucket": bucket},
        )
        return self.stack


@dataclass
class FakeBuckets:
    recorder: Recorder
    buckets: Mapping[str, str]
    account: str = ACCOUNT

    def account_id(self) -> str:
        self.recorder.record("account")
        return self.account

    def probe(self, bucket: str) -> BucketProbe:
        self.recorder.record("head_bucket", bucket)
        if bucket not in self.buckets:
            return BucketProbe(exists=False, region=None)
        return BucketProbe(exists=True, region=self.buckets[bucket])


@dataclass
class FakeImages:
    recorder: Recorder
    image: ImageObservation
    gate: ImageGateResult = field(default_factory=lambda: ImageGateResult(True, "SUCCESSFUL"))

    def observe(self, name: str) -> ImageObservation:
        self.recorder.record("get_image")
        if self.image.state is not None:
            self.recorder.record("list_versions")
        return self.image

    def wait(self, image_arn: str, image_version: str, on_progress: Progress) -> ImageGateResult:
        self.recorder.record("gate", image_version)
        return self.gate


@dataclass
class FakePublisher:
    recorder: Recorder
    image_arn: str
    artifact_uploaded: bool = True
    published_with: tuple[str, str] = ("", "")

    def publish(
        self, artifact: Path, *, bucket: str, build_role_arn: str, on_progress: Progress
    ) -> PublishedImage:
        self.recorder.record("publish", str(artifact))
        self.published_with = (bucket, build_role_arn)
        return PublishedImage(self.image_arn, IMAGE_VERSION_PUBLISHED, self.artifact_uploaded)


@dataclass
class FakeReleases:
    recorder: Recorder
    sha256_in_zip: str = ARTIFACT_SHA256
    sha256_listed: str | None = ARTIFACT_SHA256

    def fetch(self, tag: str, directory: Path, on_progress: Progress) -> DownloadedRelease:
        self.recorder.record("download", CHECKSUMS_ASSET_NAME)
        self.recorder.record("download", IMAGE_ASSET_NAME)
        artifact = directory / IMAGE_ASSET_NAME
        artifact.write_bytes(ARTIFACT_BYTES)
        listed = "" if self.sha256_listed is None else f"{self.sha256_listed}  {IMAGE_ASSET_NAME}\n"
        return DownloadedRelease(listed, artifact, self.sha256_in_zip)


@dataclass
class FakeAccount:
    """Los cinco dobles de un `AccountSetupGateways` con su registro común."""

    recorder: Recorder
    iam_stack: FakeIamStack
    buckets: FakeBuckets
    images: FakeImages
    publisher: FakePublisher
    releases: FakeReleases

    @property
    def gateways(self) -> AccountSetupGateways:
        return AccountSetupGateways(
            iam_stack=self.iam_stack,
            buckets=self.buckets,
            images=self.images,
            publisher=self.publisher,
            releases=self.releases,
        )

    @property
    def calls(self) -> list[Call]:
        return self.recorder.calls


def from_vector_input(vector: Mapping[str, Any], sdk_version: str) -> FakeAccount:
    """Los dobles de una cuenta descrita como en `plan-vectors.json`: los
    marcadores ya se sustituyen aquí."""
    region, account = vector["region"], vector["account"]
    data = fill_placeholders(vector, account=account, region=region, sdk_version=sdk_version)
    recorder = Recorder()
    arn = account_image_arn(region, account, "rayito-base")
    return FakeAccount(
        recorder=recorder,
        iam_stack=FakeIamStack(recorder, stack_from_vector(data["stack"], "rayito-m0-iam")),
        buckets=FakeBuckets(recorder, data["buckets"], account),
        images=FakeImages(recorder, image_from_vector(data["image"], "rayito-base", arn)),
        publisher=FakePublisher(recorder, arn),
        releases=FakeReleases(recorder),
    )
