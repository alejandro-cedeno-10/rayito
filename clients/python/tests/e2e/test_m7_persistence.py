"""Aceptación de `m7-s3-persistence` contra AWS real (diseño D15).

Requiere, además de `RAYITO_E2E=1` y `RAYITO_TEMPLATE`, `RAYITO_TEMPLATE_CAPS`
(la imagen `rayito-base-caps`), `RAYITO_EXECUTION_ROLE_ARN` (con la política
`persistence` de `infra/iam.yaml`) y `RAYITO_PERSIST_BUCKET`;
`RAYITO_PERSIST_PREFIX` es `rayito-e2e` por defecto; `RAYITO_TEMPLATE_DEFAULT`
(y `RAYITO_TEMPLATE_DEFAULT_VERSION`) nombran la imagen sin caps del test 4
cuando `RAYITO_TEMPLATE` apunta a la caps (así el sweeper de la sesión sólo
barre la imagen de este track). Todo objeto creado bajo el
prefijo se borra en teardown con el cliente boto3 del desarrollador
(`s3:DeleteObject` no forma parte del execution role). El test 5 (credenciales
tras 55 min, ≈ 62 min y $0,13) sólo corre con `RAYITO_E2E_SLOW=1`.
"""

from __future__ import annotations

import contextlib
import os
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

import boto3
import pytest

from rayito import CheckpointResult, S3Prefix, Sandbox
from rayito._aws import LambdaMicrovmsControlPlane
from rayito.exceptions import (
    CommandExitException,
    NotFoundException,
    PersistenceException,
    SandboxNotFoundException,
)

from .conftest import BootTimings, E2ESettings, create_test_sandbox
from .test_m6_hardening import IMDS_PROBE, wait_until

pytestmark = pytest.mark.e2e

CAPS_TEMPLATE_VAR = "RAYITO_TEMPLATE_CAPS"
DEFAULT_TEMPLATE_VAR = "RAYITO_TEMPLATE_DEFAULT"
DEFAULT_TEMPLATE_VERSION_VAR = "RAYITO_TEMPLATE_DEFAULT_VERSION"
EXECUTION_ROLE_VAR = "RAYITO_EXECUTION_ROLE_ARN"
BUCKET_VAR = "RAYITO_PERSIST_BUCKET"
PREFIX_VAR = "RAYITO_PERSIST_PREFIX"
SLOW_VAR = "RAYITO_E2E_SLOW"
DEFAULT_PREFIX = "rayito-e2e"
BLOB_BYTES = 50 * 1024 * 1024
PERSIST_BUDGET_SECONDS = 300.0
NOT_FOUND_BUDGET_SECONDS = 5.0
NO_ROLE_BUDGET_SECONDS = 5.0
IMDS_BUDGET_SECONDS = 10.0
SANDBOX_TIMEOUT_SECONDS = 1800
CREDENTIAL_PROBE_UPTIME_SECONDS = 3600
SEED_SCRIPT = (
    "mkdir -p /home/user/data /home/user/proj/src /home/user/.cache/pip /home/user/skipme "
    f"&& head -c {BLOB_BYTES} /dev/urandom > /home/user/data/blob.bin "
    "&& printf 'notas de la vida 1\\n' > /home/user/notes.txt "
    "&& printf 'print(1)\\n' > /home/user/proj/src/a.py "
    "&& ln -sf data/blob.bin /home/user/link "
    "&& printf '#!/bin/sh\\necho hola\\n' > /home/user/run.sh && chmod 0755 /home/user/run.sh "
    "&& printf 'cache\\n' > /home/user/.cache/pip/c "
    "&& printf 'skip\\n' > /home/user/skipme/s"
)
TRACKED_FILES = ("data/blob.bin", "notes.txt", "proj/src/a.py", "run.sh")


def report(label: str, value: object) -> None:
    print(f"\n[m7-persist] {label}: {value}", flush=True)


@dataclass(frozen=True)
class PersistSettings:
    caps_template: str
    execution_role_arn: str
    bucket: str
    prefix: str


@pytest.fixture(scope="module")
def persist_settings(e2e_settings: E2ESettings) -> PersistSettings:
    caps = os.environ.get(CAPS_TEMPLATE_VAR) or None
    bucket = os.environ.get(BUCKET_VAR) or None
    if not caps or not e2e_settings.execution_role_arn or not bucket:
        pytest.skip(
            f"exporta {CAPS_TEMPLATE_VAR}, {EXECUTION_ROLE_VAR} y {BUCKET_VAR} para la persistencia"
        )
    return PersistSettings(
        caps_template=caps,
        execution_role_arn=e2e_settings.execution_role_arn,
        bucket=bucket,
        prefix=os.environ.get(PREFIX_VAR) or DEFAULT_PREFIX,
    )


@dataclass
class S3Cleanup:
    """Borra en teardown, con el cliente del desarrollador, todo lo que los
    tests crearon bajo `prefix/<name>/` (el execution role no puede borrar)."""

    bucket: str
    prefix: str
    names: set[str] = field(default_factory=set)

    def track(self, target: S3Prefix) -> S3Prefix:
        assert target.name is not None
        self.names.add(target.name)
        return target

    def fresh(self, settings: PersistSettings) -> S3Prefix:
        return self.track(
            S3Prefix(settings.bucket, prefix=settings.prefix, name=f"e2e-{uuid.uuid4().hex}")
        )

    def sweep(self, region: str | None) -> int:
        client = boto3.session.Session(region_name=region).client("s3")
        deleted = 0
        for name in sorted(self.names):
            paginator = client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self.bucket, Prefix=f"{self.prefix}/{name}/"):
                for obj in page.get("Contents", []):
                    client.delete_object(Bucket=self.bucket, Key=obj["Key"])
                    deleted += 1
            for upload in client.list_multipart_uploads(
                Bucket=self.bucket, Prefix=f"{self.prefix}/{name}/"
            ).get("Uploads", []):
                client.abort_multipart_upload(
                    Bucket=self.bucket, Key=upload["Key"], UploadId=upload["UploadId"]
                )
        return deleted


@pytest.fixture(scope="module")
def s3_cleanup(persist_settings: PersistSettings, e2e_settings: E2ESettings) -> Iterator[S3Cleanup]:
    cleanup = S3Cleanup(bucket=persist_settings.bucket, prefix=persist_settings.prefix)
    yield cleanup
    deleted = cleanup.sweep(e2e_settings.region)
    report("s3 objects deleted in teardown", deleted)


def caps_sandbox(
    persist_settings: PersistSettings,
    e2e_settings: E2ESettings,
    control_plane: LambdaMicrovmsControlPlane,
    *,
    persist: S3Prefix,
) -> tuple[Sandbox, float]:
    started = time.perf_counter()
    created = Sandbox.create(
        control_plane.resolve_template_arn(persist_settings.caps_template),
        timeout=SANDBOX_TIMEOUT_SECONDS,
        idle=None,
        execution_role_arn=persist_settings.execution_role_arn,
        ingress=["ALL_INGRESS"],
        logging=e2e_settings.logging,
        control_plane=control_plane,
        persist=persist,
        persist_timeout=PERSIST_BUDGET_SECONDS,
    )
    return created, time.perf_counter() - started


def kill_quietly(sandbox: Sandbox) -> None:
    with contextlib.suppress(SandboxNotFoundException):
        sandbox.kill()


def sha256_of(sandbox: Sandbox, paths: tuple[str, ...]) -> dict[str, str]:
    output = sandbox.commands.run(
        "cd /home/user && sha256sum " + " ".join(paths), timeout=120
    ).stdout
    return {line.split()[1]: line.split()[0] for line in output.strip().splitlines()}


def stat_of(sandbox: Sandbox, path: str) -> str:
    return sandbox.commands.run(f"stat -c '%a %U:%G' /home/user/{path}", timeout=30).stdout.strip()


def mb_per_second(bytes_count: int, seconds: float) -> str:
    return f"{bytes_count / 1e6 / seconds:.2f} MB/s" if seconds > 0 else "n/a"


def timed(label: str, action: Callable[[], CheckpointResult]) -> CheckpointResult:
    started = time.perf_counter()
    result = action()
    elapsed = time.perf_counter() - started
    report(
        label,
        f"{result.files} files, {result.bytes_read} bytes read, "
        f"{result.archive_bytes} archive bytes, "
        f"{result.skipped} skipped, {elapsed:.2f} s wall ({result.duration:.2f} s agent), "
        f"{mb_per_second(result.archive_bytes, elapsed)} archive / "
        f"{mb_per_second(result.bytes_read, elapsed)} home",
    )
    return result


def test_checkpoint_kill_restore_roundtrip(
    persist_settings: PersistSettings,
    e2e_settings: E2ESettings,
    control_plane: LambdaMicrovmsControlPlane,
    s3_cleanup: S3Cleanup,
) -> None:
    target = s3_cleanup.fresh(persist_settings)
    sandbox, boot = caps_sandbox(persist_settings, e2e_settings, control_plane, persist=target)
    try:
        report("caps sandbox kernel_ready_s (first life)", f"{boot:.2f}")
        assert sandbox.persist == target
        assert sandbox.last_restore is None, "first life of the name: nothing to restore"
        sandbox.commands.run(SEED_SCRIPT, timeout=180)
        before = sha256_of(sandbox, TRACKED_FILES)
        run_stat = stat_of(sandbox, "run.sh")
        blocked_after = wait_until(
            lambda: sandbox.get_health().imds_blocked, IMDS_BUDGET_SECONDS, "imds_blocked"
        )
        report("imds_blocked after readiness (s)", f"{blocked_after:.2f}")
        with pytest.raises(CommandExitException):
            sandbox.commands.run(IMDS_PROBE, timeout=30)
        first = timed(
            "checkpoint #1 (lazy page-in)", lambda: sandbox.checkpoint_files(exclude=["skipme"])
        )
        assert first.files >= 8
        assert first.bytes_read >= BLOB_BYTES
        assert first.sha256 and len(first.sha256) == 64
        second = timed("checkpoint #2 (warm)", lambda: sandbox.checkpoint_files(exclude=["skipme"]))
        assert second.files == first.files
    finally:
        kill_quietly(sandbox)

    started = time.perf_counter()
    successor, boot2 = caps_sandbox(persist_settings, e2e_settings, control_plane, persist=target)
    try:
        restored = successor.last_restore
        assert restored is not None, "the second life must restore the checkpoint"
        elapsed = time.perf_counter() - started
        report(
            "create(persist=) second life",
            f"{boot2:.2f} s total incl. restore; restore {restored.duration:.2f} s agent, "
            f"{restored.files} files, {restored.bytes_written} bytes written, "
            f"{restored.archive_bytes} archive bytes, "
            f"{mb_per_second(restored.archive_bytes, restored.duration)} archive / "
            f"{mb_per_second(restored.bytes_written, restored.duration)} home; "
            f"wall {elapsed:.2f} s",
        )
        assert restored.sha256 == second.sha256
        after = sha256_of(successor, TRACKED_FILES)
        assert after == before, "every tracked file must hash the same after the restore"
        assert stat_of(successor, "run.sh") == run_stat == "755 user:user"
        link = successor.commands.run("readlink /home/user/link", timeout=30).stdout.strip()
        assert link == "data/blob.bin"
        absent = successor.commands.run(
            "test ! -e /home/user/.cache/pip/c && test ! -e /home/user/skipme/s && echo absent",
            timeout=30,
        ).stdout.strip()
        assert absent == "absent"
        assert (
            wait_until(
                lambda: successor.get_health().imds_blocked, IMDS_BUDGET_SECONDS, "imds_blocked"
            )
            >= 0
        )
        with pytest.raises(CommandExitException):
            successor.commands.run(IMDS_PROBE, timeout=30)
        successor.commands.run("printf 'marker\\n' > /home/user/marker.txt", timeout=30)
        started = time.perf_counter()
        reborn = successor.reincarnate(exclude=["skipme"])
        try:
            report("reincarnate() wall (s)", f"{time.perf_counter() - started:.2f}")
            assert reborn.sandbox_id != successor.sandbox_id
            assert reborn.persist == successor.persist
            assert reborn.last_restore is not None
            marker = reborn.commands.run("cat /home/user/marker.txt", timeout=30).stdout.strip()
            assert marker == "marker"
            assert sha256_of(reborn, ("data/blob.bin",)) == {
                "data/blob.bin": before["data/blob.bin"]
            }
            old_state = control_plane.get_microvm(successor.sandbox_id).state
            report("old sandbox state after reincarnate", old_state)
            assert old_state in ("TERMINATING", "TERMINATED")
        finally:
            kill_quietly(reborn)
    finally:
        kill_quietly(successor)


def test_restore_missing_is_not_found(
    persist_settings: PersistSettings,
    e2e_settings: E2ESettings,
    control_plane: LambdaMicrovmsControlPlane,
    s3_cleanup: S3Cleanup,
) -> None:
    fresh = s3_cleanup.fresh(persist_settings)
    sandbox, _ = caps_sandbox(persist_settings, e2e_settings, control_plane, persist=fresh)
    try:
        assert sandbox.last_restore is None
        started = time.perf_counter()
        with pytest.raises(NotFoundException):
            sandbox.restore_files(source=s3_cleanup.fresh(persist_settings))
        elapsed = time.perf_counter() - started
        report("explicit restore of a missing name -> NotFoundException (s)", f"{elapsed:.2f}")
        assert elapsed < NOT_FOUND_BUDGET_SECONDS
    finally:
        kill_quietly(sandbox)


def test_no_role_is_permission_denied(
    persist_settings: PersistSettings,
    e2e_settings: E2ESettings,
    control_plane: LambdaMicrovmsControlPlane,
    boot_timings: BootTimings,
    s3_cleanup: S3Cleanup,
) -> None:
    without_role = E2ESettings(
        template=os.environ.get(DEFAULT_TEMPLATE_VAR) or e2e_settings.template,
        region=e2e_settings.region,
        execution_role_arn=None,
        template_version=os.environ.get(DEFAULT_TEMPLATE_VERSION_VAR)
        or e2e_settings.template_version,
    )
    default_arn = control_plane.resolve_template_arn(without_role.template)
    sandbox = create_test_sandbox(without_role, control_plane, default_arn, boot_timings)
    try:
        target = s3_cleanup.fresh(persist_settings)
        started = time.perf_counter()
        with pytest.raises(PersistenceException) as excinfo:
            sandbox.checkpoint_files(target=target)
        elapsed = time.perf_counter() - started
        report(
            "default image without a role -> checkpoint_files",
            f"PersistenceException(code={excinfo.value.code}) in {elapsed:.2f} s: {excinfo.value}",
        )
        assert excinfo.value.code == "permission_denied"
        assert elapsed < NO_ROLE_BUDGET_SECONDS
        assert sandbox.get_health().imds_blocked is False
    finally:
        kill_quietly(sandbox)


@pytest.mark.slow
def test_credentials_after_55_minutes(
    persist_settings: PersistSettings,
    e2e_settings: E2ESettings,
    control_plane: LambdaMicrovmsControlPlane,
    s3_cleanup: S3Cleanup,
) -> None:
    if os.environ.get(SLOW_VAR) != "1":
        pytest.skip(f"exporta {SLOW_VAR}=1 para medir la rotación de credenciales (≈ 62 min)")
    target = s3_cleanup.fresh(persist_settings)
    sandbox, _ = caps_sandbox(persist_settings, e2e_settings, control_plane, persist=target)
    try:
        while sandbox.get_health().uptime_ms < CREDENTIAL_PROBE_UPTIME_SECONDS * 1000:
            time.sleep(60)
        try:
            result = sandbox.checkpoint_files()
            report(
                "checkpoint after 60 min of uptime",
                f"ok: {result.files} files (credentials rotated)",
            )
        except PersistenceException as exc:
            report(
                "checkpoint after 60 min of uptime",
                f"PersistenceException(code={exc.code}): {exc} (credentials NOT rotated)",
            )
            assert exc.code == "permission_denied"
    finally:
        kill_quietly(sandbox)
