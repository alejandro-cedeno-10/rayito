"""`m15-s3-mounts` contra AWS real (`RAYITO_E2E=1`): S3M-1..S3M-4 del plan de
aceptación de M15 (ADR-017; cap de gasto $0.30).

Necesita, además de las variables habituales de `conftest.py`:
- `RAYITO_TEMPLATE_CAPS`: imagen `rayito-base-caps` publicada con
  `rayito image publish ... --image-name rayito-base-caps --os-capabilities
  ALL --env RAYITO_ALLOWED_MOUNT_BUCKETS=<bucket>` (desde el repositorio,
  `make image-publish-caps MOUNT_BUCKETS=<bucket>`). Sin el bucket en ese
  allowlist, cada montaje es `MountException(code="not_allowed")` y su
  mensaje lo dice.
- `RAYITO_EXECUTION_ROLE_ARN`: el rol debe llevar la política
  `RayitoS3MountAccess` de `infra/s3-mounts.yaml` sobre el bucket de abajo,
  con `Prefixes` que cubra `rayito-e2e-s3-mounts/*` y `ReadOnly=false`.
- `RAYITO_S3_MOUNT_BUCKET`: un bucket ya existente, vacío o con un prefijo
  de test dedicado; este fichero sólo escribe y borra bajo
  `rayito-e2e-s3-mounts/<uuid>/`.

Coste: un sandbox `rayito-base-caps` por sesión (~$0.05) + un puñado de
peticiones S3 sobre un único objeto pequeño (~$0).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import boto3
import pytest

from rayito import S3Mount, Sandbox
from rayito._aws import LambdaMicrovmsControlPlane
from rayito.exceptions import CommandExitException

from .conftest import E2ESettings

pytestmark = pytest.mark.e2e

CAPS_TEMPLATE_VAR = "RAYITO_TEMPLATE_CAPS"
BUCKET_VAR = "RAYITO_S3_MOUNT_BUCKET"
TEST_PREFIX_ROOT = "rayito-e2e-s3-mounts"


def report(label: str, value: object) -> None:
    print(f"[s3-mounts e2e] {label}: {value}")


def _caps_template() -> str | None:
    return os.environ.get(CAPS_TEMPLATE_VAR) or None


def _test_bucket() -> str | None:
    return os.environ.get(BUCKET_VAR) or None


@pytest.fixture
def test_prefix() -> Iterator[str]:
    """A fresh `rayito-e2e-s3-mounts/<uuid>/` prefix, deleted (every object
    under it) at teardown regardless of what the test did."""
    prefix = f"{TEST_PREFIX_ROOT}/{uuid.uuid4().hex}/"
    yield prefix
    bucket = _test_bucket()
    if not bucket:
        return
    s3 = boto3.client("s3")
    paginator = s3.get_paginator("list_objects_v2")
    keys = [
        obj["Key"]
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix)
        for obj in page.get("Contents", [])
    ]
    if keys:
        s3.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": key} for key in keys]})
        report("cleanup: objects deleted", len(keys))


@pytest.fixture
def s3_mount_sandbox(
    e2e_settings: E2ESettings,
    control_plane: LambdaMicrovmsControlPlane,
    test_prefix: str,
) -> Iterator[Sandbox]:
    template = _caps_template()
    bucket = _test_bucket()
    if not template or not bucket or not e2e_settings.execution_role_arn:
        pytest.skip(
            f"exporta {CAPS_TEMPLATE_VAR}, {BUCKET_VAR} y RAYITO_EXECUTION_ROLE_ARN "
            "(con la política RayitoS3MountAccess) para medir m15-s3-mounts"
        )
    created = Sandbox.create(
        control_plane.resolve_template_arn(template),
        timeout=600,
        idle=None,
        execution_role_arn=e2e_settings.execution_role_arn,
        logging=e2e_settings.logging,
        control_plane=control_plane,
        mounts={
            "/mnt/ro": S3Mount(bucket=bucket, prefix=test_prefix, read_only=True),
            "/mnt/rw": S3Mount(
                bucket=bucket, prefix=test_prefix, read_only=False, allow_overwrite=True
            ),
        },
    )
    try:
        yield created
    finally:
        created.kill()


def test_s3m_1_read_write_mount_round_trips_through_s3(
    s3_mount_sandbox: Sandbox, test_prefix: str
) -> None:
    """S3M-1: un objeto escrito bajo `/mnt/rw` aparece en S3 bajo el mismo
    prefijo, y un objeto puesto en S3 de antemano se lee bajo `/mnt/ro`."""
    bucket = _test_bucket()
    assert bucket is not None
    s3 = boto3.client("s3")
    s3.put_object(Bucket=bucket, Key=f"{test_prefix}seed.txt", Body=b"hola desde S3\n")

    mounts = s3_mount_sandbox.mounts
    assert mounts["/mnt/ro"].state == "mounted"
    assert mounts["/mnt/rw"].state == "mounted"

    read = s3_mount_sandbox.commands.run("cat /mnt/ro/seed.txt")
    assert read.stdout.strip() == "hola desde S3"

    write = s3_mount_sandbox.commands.run("sh -c 'echo escrito-por-rayd > /mnt/rw/salida.txt'")
    assert write.exit_code == 0
    body = s3.get_object(Bucket=bucket, Key=f"{test_prefix}salida.txt")["Body"].read()
    assert body.decode().strip() == "escrito-por-rayd"
    report("S3M-1", "round trip ok")


def test_s3m_2_uid_1000_cannot_read_the_daemon_environ_or_argv(
    s3_mount_sandbox: Sandbox,
) -> None:
    """S3M-2 (SEC-3): como uid 1000, ni `/proc/<pid>/environ` ni
    `/proc/<pid>/cmdline` del daemon `mount-s3` exponen una credencial
    (el bucket y el prefijo sí aparecen: están declarados no secretos)."""
    find_pid = s3_mount_sandbox.commands.run("pgrep -f mount-s3 | head -1")
    pid = find_pid.stdout.strip()
    assert pid, "mount-s3 debería estar corriendo"

    cmdline = s3_mount_sandbox.commands.run(f"tr '\\0' ' ' < /proc/{pid}/cmdline")
    stdout = cmdline.stdout
    for forbidden in ("AKIA", "aws_secret", "aws_session_token"):
        assert forbidden.lower() not in stdout.lower()

    # Un exit distinto de cero es `CommandExitException` en `commands.run`
    # (contrato de E2B), no un resultado con `exit_code`.
    with pytest.raises(CommandExitException) as kill_attempt:
        s3_mount_sandbox.commands.run(f"kill -0 {pid}")
    assert kill_attempt.value.exit_code != 0, "uid 1000 no debería poder señalar al daemon"
    report("S3M-2", "no credential in argv; daemon not killable by uid 1000")


def test_s3m_3_suspend_with_s3_unreachable_still_answers_within_budget(
    s3_mount_sandbox: Sandbox,
) -> None:
    """S3M-3: `/suspend` responde 200 dentro de su presupuesto aunque S3 no
    sea alcanzable (ADR-017: s3-mounts no reclama ningún share de
    `/suspend`, así que esto nunca debería depender de la llamada S3)."""
    import time

    started = time.perf_counter()
    s3_mount_sandbox.pause()
    elapsed = time.perf_counter() - started
    report("S3M-3: pause() elapsed (s)", f"{elapsed:.2f}")
    assert elapsed < 30
    s3_mount_sandbox.resume()


def test_s3m_4_mount_works_with_allow_internet_access_false(
    e2e_settings: E2ESettings,
    control_plane: LambdaMicrovmsControlPlane,
    test_prefix: str,
) -> None:
    """S3M-4: el montaje sigue funcionando con `allow_internet_access=False`
    (el acceso a S3 va por el VPC endpoint/execution role, no por el
    egress a Internet que esa opción cierra)."""
    template = _caps_template()
    bucket = _test_bucket()
    if not template or not bucket or not e2e_settings.execution_role_arn:
        pytest.skip(f"exporta {CAPS_TEMPLATE_VAR}, {BUCKET_VAR} y RAYITO_EXECUTION_ROLE_ARN")

    sbx = Sandbox.create(
        control_plane.resolve_template_arn(template),
        timeout=300,
        idle=None,
        execution_role_arn=e2e_settings.execution_role_arn,
        allow_internet_access=False,
        logging=e2e_settings.logging,
        control_plane=control_plane,
        mounts={"/mnt/ro": S3Mount(bucket=bucket, prefix=test_prefix, read_only=True)},
    )
    try:
        result = sbx.commands.run("ls /mnt/ro")
        assert result.exit_code == 0
        report("S3M-4", "mount works with allow_internet_access=False")
    finally:
        sbx.kill()
