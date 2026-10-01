"""`BuildClients` falso en memoria (m15-templates): sin boto3, sin red.
Cada método hace lo mínimo necesario para que `_build.py` avance y registra
sus llamadas en `calls`, para que los tests comprueben qué se pidió sin
tocar AWS."""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass, field
from typing import Any


def make_base_zip(dockerfile: str, extra: dict[str, bytes] | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("Dockerfile", dockerfile)
        for name, content in (extra or {}).items():
            archive.writestr(name, content)
    return buffer.getvalue()


@dataclass
class FakeBuildClients:
    """Satisface el `Protocol` `BuildClients`. `images`/`versions`/`objects`
    son los "servidores" falsos que cada operación boto3 consulta."""

    region_value: str = "us-east-1"
    account_id_value: str = "123456789012"
    #: `arn -> {"state": ..., "codeArtifact": {...}}` (GetMicrovmImage).
    images: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: `(arn, version) -> detail dict` (GetMicrovmImageVersion).
    versions: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    #: `(bucket, key) -> bytes` (S3).
    objects: dict[tuple[str, str], bytes] = field(default_factory=dict)
    #: Líneas que devuelve `GetLogEvents` para el único log group simulado.
    log_lines: list[str] = field(default_factory=list)
    calls: list[tuple[str, ...]] = field(default_factory=list)
    #: Qué devuelve `submit_build` (create/update-microvm-image).
    next_build_version: str = "1"

    @property
    def region(self) -> str:
        return self.region_value

    @property
    def account_id(self) -> str:
        return self.account_id_value

    @property
    def microvms(self) -> _FakeMicrovms:
        return _FakeMicrovms(self)

    @property
    def s3(self) -> _FakeS3:
        return _FakeS3(self)

    @property
    def logs(self) -> _FakeLogs:
        return _FakeLogs(self)


class _FakeMicrovms:
    def __init__(self, parent: FakeBuildClients) -> None:
        self._parent = parent

    def get_microvm_image(self, *, imageIdentifier: str) -> dict[str, Any]:
        self._parent.calls.append(("get_microvm_image", imageIdentifier))
        image = self._parent.images.get(imageIdentifier)
        if image is None:
            raise _client_error("ResourceNotFoundException")
        return image

    def get_microvm_image_version(
        self, *, imageIdentifier: str, imageVersion: str
    ) -> dict[str, Any]:
        self._parent.calls.append(("get_microvm_image_version", imageIdentifier, imageVersion))
        detail = self._parent.versions.get((imageIdentifier, imageVersion))
        if detail is None:
            raise _client_error("ResourceNotFoundException")
        return detail

    def get_paginator(self, operation: str) -> _FakeVersionPaginator:
        assert operation == "list_microvm_image_versions"
        return _FakeVersionPaginator(self._parent)

    def create_microvm_image(self, *, name: str, **request: Any) -> dict[str, Any]:
        self._parent.calls.append(("create_microvm_image", name))
        arn = f"arn:aws:lambda:{self._parent.region}:{self._parent.account_id}:microvm-image:{name}"
        version = self._parent.next_build_version
        self._parent.images[arn] = {"state": "CREATED", **request}
        self._parent.versions[(arn, version)] = {
            "state": "SUCCESSFUL",
            "status": "ACTIVE",
            "imageVersion": version,
            "createdAt": 1,
            **request,
        }
        return {"imageArn": arn, "imageVersion": version}

    def update_microvm_image(self, *, imageIdentifier: str, **request: Any) -> dict[str, Any]:
        self._parent.calls.append(("update_microvm_image", imageIdentifier))
        version = self._parent.next_build_version
        self._parent.versions[(imageIdentifier, version)] = {
            "state": "SUCCESSFUL",
            "status": "ACTIVE",
            "imageVersion": version,
            "createdAt": 2,
            **request,
        }
        return {"imageArn": imageIdentifier, "imageVersion": version}


class _FakeVersionPaginator:
    def __init__(self, parent: FakeBuildClients) -> None:
        self._parent = parent

    def paginate(self, *, imageIdentifier: str) -> list[dict[str, list[dict[str, Any]]]]:
        items = [
            {**detail, "imageVersion": version}
            for (arn, version), detail in self._parent.versions.items()
            if arn == imageIdentifier
        ]
        return [{"items": items}]


class _FakeS3:
    def __init__(self, parent: FakeBuildClients) -> None:
        self._parent = parent

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        self._parent.calls.append(("get_object", Bucket, Key))
        content = self._parent.objects.get((Bucket, Key))
        if content is None:
            raise _client_error("NoSuchKey")
        return {"Body": _Readable(content)}

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        self._parent.calls.append(("head_object", Bucket, Key))
        if (Bucket, Key) not in self._parent.objects:
            raise _client_error("404")
        return {}

    def put_object(self, *, Bucket: str, Key: str, Body: bytes) -> dict[str, Any]:
        self._parent.calls.append(("put_object", Bucket, Key))
        self._parent.objects[(Bucket, Key)] = Body
        return {}


class _FakeLogs:
    def __init__(self, parent: FakeBuildClients) -> None:
        self._parent = parent

    def describe_log_streams(self, **_kwargs: Any) -> dict[str, Any]:
        self._parent.calls.append(("describe_log_streams",))
        if not self._parent.log_lines:
            return {"logStreams": []}
        return {"logStreams": [{"logStreamName": "only-stream"}]}

    def get_log_events(self, **_kwargs: Any) -> dict[str, Any]:
        self._parent.calls.append(("get_log_events",))
        return {
            "events": [
                {"message": line, "timestamp": i} for i, line in enumerate(self._parent.log_lines)
            ]
        }


class _Readable:
    def __init__(self, content: bytes) -> None:
        self._content = content

    def read(self) -> bytes:
        return self._content


def _client_error(code: str) -> Exception:
    from botocore.exceptions import ClientError

    error: Exception = ClientError({"Error": {"Code": code, "Message": code}}, "FakeOperation")
    return error
