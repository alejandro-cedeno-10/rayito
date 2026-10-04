"""`AsyncTemplate`/`_build_async.py` (m15-templates): las mismas operaciones
que `_build.py`, sólo que detrás de `asyncio.to_thread` (como el resto del
SDK async)."""

from __future__ import annotations

from pathlib import Path

import pytest

from rayito import AsyncTemplate, Template
from rayito._templates import _build

from .fake_templates import FakeBuildClients, make_base_zip

BASE_ARN = "arn:aws:lambda:us-east-1:123456789012:microvm-image:rayito-base"
BUCKET = "my-artifact-bucket"
BASE_DOCKERFILE = 'FROM scratch\nCOPY rayd /usr/local/bin/rayd\nCMD ["/usr/local/bin/rayd"]\n'


def _clients_with_base_image() -> FakeBuildClients:
    clients = FakeBuildClients()
    clients.images[BASE_ARN] = {"state": "CREATED"}
    clients.versions[(BASE_ARN, "1")] = {
        "state": "SUCCESSFUL",
        "status": "ACTIVE",
        "imageVersion": "1",
        "createdAt": 1,
        "baseImageArn": "arn:aws:lambda:us-east-1:aws:microvm-image:al2023-1",
        "baseImageVersion": "1.0",
        "buildRoleArn": "arn:aws:iam::123456789012:role/rayito-build",
        "hooks": {"port": 9000},
        "codeArtifact": {"uri": f"s3://{BUCKET}/base.zip"},
    }
    clients.objects[(BUCKET, "base.zip")] = make_base_zip(BASE_DOCKERFILE)
    return clients


@pytest.mark.asyncio
async def test_async_template_build_delegates_to_the_sync_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clients = _clients_with_base_image()
    monkeypatch.setattr(_build, "_Clients", lambda **_kwargs: clients)

    info = await AsyncTemplate.build(
        Template().from_base_image().pip_install("pandas"),
        "mi-template",
        bucket=BUCKET,
        context_dir=Path(),
    )
    assert info.alias == "mi-template"


@pytest.mark.asyncio
async def test_async_build_in_background_and_status(monkeypatch: pytest.MonkeyPatch) -> None:
    clients = _clients_with_base_image()
    monkeypatch.setattr(_build, "_Clients", lambda **_kwargs: clients)

    handle = await AsyncTemplate.build_in_background(
        Template().from_base_image().pip_install("pandas"),
        "mi-template",
        bucket=BUCKET,
        context_dir=Path(),
    )
    status = await AsyncTemplate.get_build_status(handle)
    assert status.state == "SUCCESSFUL"


@pytest.mark.asyncio
async def test_async_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    clients = _clients_with_base_image()
    monkeypatch.setattr(_build, "_Clients", lambda **_kwargs: clients)
    assert await AsyncTemplate.exists("not-there") is False
