"""Los seis puertos de `AccountSetup` en memoria (ADR-030): registran cada
llamada y se construyen desde el `input` de `testdata/account-setup/*.json`
(el mismo formato en `plan-vectors.json` y `cli-cases.json`). Sin AWS, sin
red y sin disco fuera del directorio que se les da."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from rayito._account_setup._domain import (
    CAPS_IMAGE_NAME,
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
    default_artifact_bucket_name,
)
from rayito._account_setup._iam_stack import IAM_STACK_COMPONENT
from rayito._account_setup._ports import AccountSetupGateways, Progress
from rayito._images import account_image_arn
from rayito._stacks._packaging import template_fingerprint

TESTDATA: Final = Path(__file__).resolve().parents[4] / "testdata" / "account-setup"
ACCOUNT: Final = "123456789012"
REGION: Final = "us-east-1"
BUILD_ROLE_ARN: Final = f"arn:aws:iam::{ACCOUNT}:role/rayito-m0-build-{REGION}"
IMAGE_VERSION_PUBLISHED: Final = "3.0"
ARTIFACT_BYTES: Final = b"image zip"
ARTIFACT_SHA256: Final = hashlib.sha256(ARTIFACT_BYTES).hexdigest()
#: Sufijo de las llamadas que tocan `rayito-base-caps` en el registro.
CAPS_CALL_SUFFIX: Final = ":caps"
#: El sha256 del `infra/iam.yaml` empaquetado: `{iamTemplateFingerprint}` en
#: los vectores.
IAM_TEMPLATE_FINGERPRINT: Final = template_fingerprint(IAM_STACK_COMPONENT)

Call = tuple[str, ...]


def load_contract() -> dict[str, Any]:
    return dict(json.loads((TESTDATA / "contract.json").read_text(encoding="utf-8")))


def load_cases(name: str) -> list[dict[str, Any]]:
    document = json.loads((TESTDATA / name).read_text(encoding="utf-8"))
    return list(document["cases"])


def fill_placeholders(value: Any, *, account: str, region: str, sdk_version: str) -> Any:
    """Sustituye `{sdkVersion}`, `{cdkBucket}`, `{defaultBucket}` e
    `{iamTemplateFingerprint}` en cadenas, claves y listas."""
    if isinstance(value, str):
        return (
            value.replace("{sdkVersion}", sdk_version)
            .replace("{iamTemplateFingerprint}", IAM_TEMPLATE_FINGERPRINT)
            .replace("{cdkBucket}", cdk_bucket_name(account, region))
            .replace("{defaultBucket}", default_artifact_bucket_name(account, region))
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


def bucket_reads(calls: Sequence[Call]) -> list[str]:
    """Las lecturas que resuelven el bucket, como `bucketReads` de
    `plan-vectors.json`: `head:<bucket>` y `artifactStack`."""
    reads: list[str] = []
    for call in calls:
        if call[0] == "head_bucket":
            reads.append(f"head:{call[1]}")
        elif call[0] == "describe_artifact_stack":
            reads.append("artifactStack")
    return reads


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

    def template_fingerprint(self) -> str:
        return IAM_TEMPLATE_FINGERPRINT

    def create(self, bucket: str) -> StackObservation:
        self.recorder.record("create", bucket)
        return self._complete(bucket, "CREATE_COMPLETE")

    def update_bucket(self, bucket: str) -> StackObservation:
        self.recorder.record("update", bucket)
        return self._complete(bucket, "UPDATE_COMPLETE")

    def wait(self) -> StackObservation:
        """Termina la operación en curso sin tocar los parámetros: la pila
        queda con la plantilla con la que se desplegaba."""
        self.recorder.record("wait")
        self.stack = StackObservation(
            self.stack.name, "CREATE_COMPLETE", dict(self.created_outputs), self.stack.parameters
        )
        return self.stack

    def _complete(self, bucket: str, status: str) -> StackObservation:
        self.stack = StackObservation(
            self.stack.name,
            status,
            dict(self.created_outputs),
            {
                **self.stack.parameters,
                "ArtifactBucket": bucket,
                "TemplateFingerprint": IAM_TEMPLATE_FINGERPRINT,
            },
        )
        return self.stack


@dataclass
class FakeArtifactStack:
    """La pila `rayito-m0-artifacts` en memoria: `create` y `wait` la dejan
    completa con la salida `BucketName`."""

    recorder: Recorder
    stack: StackObservation
    created_outputs_key: str = "BucketName"

    def observe(self) -> StackObservation:
        self.recorder.record("describe_artifact_stack")
        return self.stack

    def create(self, bucket: str) -> StackObservation:
        self.recorder.record("create_artifact_stack", bucket)
        return self._complete(bucket)

    def wait(self) -> StackObservation:
        self.recorder.record("wait_artifact_stack")
        return self._complete(self.stack.parameters.get("BucketName", ""))

    def _complete(self, bucket: str) -> StackObservation:
        self.stack = StackObservation(
            self.stack.name,
            "CREATE_COMPLETE",
            {self.created_outputs_key: bucket},
            {"BucketName": bucket},
        )
        return self.stack


@dataclass
class FakeBuckets:
    recorder: Recorder
    buckets: Mapping[str, str]
    forbidden: Sequence[str] = ()
    account: str = ACCOUNT

    def account_id(self) -> str:
        self.recorder.record("account")
        return self.account

    def probe(self, bucket: str) -> BucketProbe:
        self.recorder.record("head_bucket", bucket)
        if bucket in self.forbidden:
            return BucketProbe("forbidden", None)
        if bucket not in self.buckets:
            return BucketProbe("missing", None)
        return BucketProbe("found", self.buckets[bucket])


@dataclass
class FakeImages:
    """`rayito-base` (`image`) y `rayito-base-caps` (`caps`) en memoria. Las
    llamadas de la base se registran como `get_image`/`list_versions`, igual
    que antes de `capsImage`; las de la imagen con capacidades, con el
    sufijo `:caps`."""

    recorder: Recorder
    image: ImageObservation
    caps: ImageObservation | None = None
    gate: ImageGateResult = field(default_factory=lambda: ImageGateResult(True, "SUCCESSFUL"))

    def observe(self, name: str) -> ImageObservation:
        observed = self.caps if name == CAPS_IMAGE_NAME else self.image
        suffix = CAPS_CALL_SUFFIX if name == CAPS_IMAGE_NAME else ""
        assert observed is not None
        self.recorder.record(f"get_image{suffix}")
        if observed.state is not None:
            self.recorder.record(f"list_versions{suffix}")
        return observed

    def wait(self, image_arn: str, image_version: str, on_progress: Progress) -> ImageGateResult:
        self.recorder.record("gate", image_version)
        return self.gate


@dataclass
class FakePublisher:
    """Registra `("publish", <zip>)` para `rayito-base` y
    `("publish:caps", <zip>, <os_capabilities>)` para `rayito-base-caps`."""

    recorder: Recorder
    image_arn: str
    caps_image_arn: str = ""
    artifact_uploaded: bool = True
    published_with: tuple[str, str] = ("", "")

    def publish(
        self,
        artifact: Path,
        *,
        name: str,
        os_capabilities: str | None,
        bucket: str,
        build_role_arn: str,
        on_progress: Progress,
    ) -> PublishedImage:
        self.published_with = (bucket, build_role_arn)
        if name == CAPS_IMAGE_NAME:
            self.recorder.record(f"publish{CAPS_CALL_SUFFIX}", str(artifact), str(os_capabilities))
            return PublishedImage(
                self.caps_image_arn, IMAGE_VERSION_PUBLISHED, self.artifact_uploaded
            )
        assert os_capabilities is None
        self.recorder.record("publish", str(artifact))
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
    """Los seis dobles de un `AccountSetupGateways` con su registro común."""

    recorder: Recorder
    iam_stack: FakeIamStack
    artifact_stack: FakeArtifactStack
    buckets: FakeBuckets
    images: FakeImages
    publisher: FakePublisher
    releases: FakeReleases

    @property
    def gateways(self) -> AccountSetupGateways:
        return AccountSetupGateways(
            iam_stack=self.iam_stack,
            artifact_stack=self.artifact_stack,
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
    caps_arn = account_image_arn(region, account, CAPS_IMAGE_NAME)
    return FakeAccount(
        recorder=recorder,
        iam_stack=FakeIamStack(recorder, stack_from_vector(data["stack"], "rayito-m0-iam")),
        artifact_stack=FakeArtifactStack(
            recorder, stack_from_vector(data["artifactStack"], "rayito-m0-artifacts")
        ),
        buckets=FakeBuckets(recorder, data["buckets"], data["forbiddenBuckets"], account),
        images=FakeImages(
            recorder,
            image_from_vector(data["image"], "rayito-base", arn),
            image_from_vector(data.get("capsImage"), CAPS_IMAGE_NAME, caps_arn),
        ),
        publisher=FakePublisher(recorder, arn, caps_arn),
        releases=FakeReleases(recorder),
    )
