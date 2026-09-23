"""Aceptación de `m9-file-transfer` contra AWS real
(`openspec/changes/m9-file-transfer/design.md` D24).

Requiere, además de `RAYITO_E2E=1` y `RAYITO_TEMPLATE` (la imagen
`rayito-base` M9), `RAYITO_E2E_TRANSFER_BUCKET`; `RAYITO_E2E_TRANSFER_PREFIX`
es `rayito-e2e-transfer` por defecto. Sin bucket, todo el módulo se salta.
Los sandboxes nacen **sin execution role** (salvo el test 13, que usa
`RAYITO_EXECUTION_ROLE_ARN`, el rol de sólo logs). En teardown se borra
cada objeto bajo `<prefix>/<sandbox_id>/` y se abortan las subidas
multiparte con el cliente boto3 de quien corre los tests. Las URLs se usan
con `urllib` (stdlib), como las usaría cualquier cliente HTTP sin
credenciales; nunca se imprimen.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import secrets
import time
import urllib.error
import urllib.request
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
import grpc
import pytest
from botocore.exceptions import ClientError

from rayito import AsyncSandbox, S3Staging, Sandbox
from rayito._aws import LambdaMicrovmsControlPlane
from rayito._s3 import S3Gateway
from rayito._transfer_base import new_staging_object, unix_ms
from rayito.exceptions import (
    InvalidArgumentException,
    SandboxNotFoundException,
    TimeoutException,
)
from rayito.v1 import filesystem_pb2

from .conftest import TEST_SANDBOX_TIMEOUT_SECONDS, E2ESettings
from .test_m9_egress import rayd_log_lines

pytestmark = pytest.mark.e2e

BUCKET_VAR = "RAYITO_E2E_TRANSFER_BUCKET"
PREFIX_VAR = "RAYITO_E2E_TRANSFER_PREFIX"
DEFAULT_PREFIX = "rayito-e2e-transfer"
MIB = 1_048_576
HOME = "/home/user"
HTTP_TIMEOUT_SECONDS = 300
WAIT_SECONDS = 300.0
GRPC_BASELINE_BYTES = 20 * 1_000_000
LARGE_BYTES = 200 * 1_000_000
ROUTED_SPEEDUP = 10.0
LINK_SHARE = 0.7
GZIP_SPEEDUP = 3.0


def report(label: str, value: object) -> None:
    print(f"\n[m9-transfer] {label}: {value}", flush=True)


def rate(size: int, seconds: float) -> float:
    return size / 1_000_000 / max(seconds, 1e-6)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class TransferSettings:
    bucket: str
    prefix: str

    def staging(self, **overrides: Any) -> S3Staging:
        return S3Staging(bucket=self.bucket, prefix=self.prefix, **overrides)


@pytest.fixture(scope="module")
def transfer_settings() -> TransferSettings:
    bucket = os.environ.get(BUCKET_VAR) or None
    if bucket is None:
        pytest.skip(f"exporta {BUCKET_VAR}=<bucket> para las transferencias de M9")
    return TransferSettings(bucket=bucket, prefix=os.environ.get(PREFIX_VAR) or DEFAULT_PREFIX)


@pytest.fixture(scope="module")
def s3(control_plane: LambdaMicrovmsControlPlane) -> Any:
    return boto3.client("s3", region_name=control_plane.region)


def clean_staging(s3: Any, settings: TransferSettings, sandbox_id: str) -> None:
    """Borra lo que quede bajo `<prefix>/<sandbox_id>/` y aborta las subidas
    multiparte abiertas: el teardown nunca deja objetos del test."""
    scope = f"{settings.prefix}/{sandbox_id}/"
    listing = s3.list_objects_v2(Bucket=settings.bucket, Prefix=scope)
    for item in listing.get("Contents", []):
        s3.delete_object(Bucket=settings.bucket, Key=item["Key"])
    uploads = s3.list_multipart_uploads(Bucket=settings.bucket, Prefix=scope)
    for upload in uploads.get("Uploads", []):
        s3.abort_multipart_upload(
            Bucket=settings.bucket, Key=upload["Key"], UploadId=upload["UploadId"]
        )


def open_uploads(s3: Any, settings: TransferSettings, sandbox_id: str) -> list[Any]:
    scope = f"{settings.prefix}/{sandbox_id}/"
    return list(s3.list_multipart_uploads(Bucket=settings.bucket, Prefix=scope).get("Uploads", []))


def staged_objects(s3: Any, settings: TransferSettings, sandbox_id: str) -> list[str]:
    scope = f"{settings.prefix}/{sandbox_id}/"
    listing = s3.list_objects_v2(Bucket=settings.bucket, Prefix=scope)
    return [str(item["Key"]) for item in listing.get("Contents", [])]


def create_staged(
    e2e_settings: E2ESettings,
    control_plane: LambdaMicrovmsControlPlane,
    template_arn: str,
    staging: S3Staging,
    *,
    execution_role_arn: str | None = None,
) -> Sandbox:
    created = Sandbox.create(
        template_arn,
        template_version=e2e_settings.template_version,
        timeout=TEST_SANDBOX_TIMEOUT_SECONDS,
        idle=None,
        execution_role_arn=execution_role_arn,
        ingress=["ALL_INGRESS"],
        logging="cloudwatch" if execution_role_arn else "disabled",
        control_plane=control_plane,
        transfer=staging,
    )
    report("sandbox", created.sandbox_id)
    return created


@contextlib.contextmanager
def plain_connection(
    staged: Sandbox, control_plane: LambdaMicrovmsControlPlane, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Sandbox]:
    """Un segundo handle al mismo sandbox sin bucket de transferencias: todo
    va por gRPC, la línea base de los tests de rendimiento."""
    for name in ("RAYITO_TRANSFER_BUCKET", "RAYITO_TRANSFER_PREFIX", "RAYITO_TRANSFER_REGION"):
        monkeypatch.delenv(name, raising=False)
    plain = Sandbox.connect(
        staged.sandbox_id,
        access_token=staged.access_token,
        control_plane=control_plane,
        transfer=None,
    )
    try:
        assert plain.transfer is None
        yield plain
    finally:
        plain.close()


@pytest.fixture
def staged(
    e2e_settings: E2ESettings,
    control_plane: LambdaMicrovmsControlPlane,
    template_arn: str,
    transfer_settings: TransferSettings,
    s3: Any,
) -> Iterator[Sandbox]:
    created = create_staged(e2e_settings, control_plane, template_arn, transfer_settings.staging())
    try:
        assert created.get_info().execution_role_arn is None
        yield created
    finally:
        with contextlib.suppress(SandboxNotFoundException):
            created.kill()
        clean_staging(s3, transfer_settings, created.sandbox_id)


# -------------------------------------------------------------------- http


def http(
    url: str,
    *,
    method: str = "GET",
    data: bytes | None = None,
    headers: Mapping[str, str] | None = None,
) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=data, method=method, headers=dict(headers or {}))
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            return int(response.status), response.read()
    except urllib.error.HTTPError as error:
        return int(error.code), error.read()


def post_form(url: str, fields: Mapping[str, str], data: bytes) -> tuple[int, bytes]:
    boundary = secrets.token_hex(16)
    parts = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        for name, value in fields.items()
    ]
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="f"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n".encode()
        + data
        + f"\r\n--{boundary}--\r\n".encode()
    )
    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    return http(url, method="POST", data=b"".join(parts), headers=headers)


def remote_sha256(sandbox: Sandbox, path: str) -> str:
    return sandbox.commands.run(f"sha256sum {path}").stdout.split()[0]


def connections(ss_output: str) -> set[tuple[str, str]]:
    """`(local, peer)` of every non-listening TCP socket in `ss -Htan`
    output. The platform keeps its own guest socket to the link-local
    metadata address and the ingress listener sits on local port 8443, so
    the SSRF check compares peers against a snapshot taken before the calls."""
    pairs: set[tuple[str, str]] = set()
    for line in ss_output.splitlines():
        fields = line.split()
        if len(fields) >= 5 and fields[0] != "LISTEN":
            pairs.add((fields[3], fields[4]))
    return pairs


def link_rates(s3: Any, settings: TransferSettings, sandbox_id: str) -> tuple[float, float]:
    """Direct boto3 `PutObject`/`GetObject` MB/s between this machine and
    S3 in the same run: the ceiling any routed transfer can reach."""
    key = f"{settings.prefix}/{sandbox_id}/link-probe"
    data = os.urandom(GRPC_BASELINE_BYTES)
    started = time.perf_counter()
    s3.put_object(Bucket=settings.bucket, Key=key, Body=data)
    put = rate(len(data), time.perf_counter() - started)
    started = time.perf_counter()
    s3.get_object(Bucket=settings.bucket, Key=key)["Body"].read()
    get = rate(len(data), time.perf_counter() - started)
    s3.delete_object(Bucket=settings.bucket, Key=key)
    return put, get


def routed_floor(grpc_rate: float, link_rate: float) -> float:
    """D24 asks for 10x the gRPC baseline; a developer link slower than that
    caps what S3 routing can show, so the floor is the lower of the two."""
    return min(ROUTED_SPEEDUP * grpc_rate, LINK_SHARE * link_rate)


def head_status(s3: Any, bucket: str, key: str) -> int:
    try:
        s3.head_object(Bucket=bucket, Key=key)
    except ClientError as error:
        return int(error.response["ResponseMetadata"]["HTTPStatusCode"])
    return 200


# ------------------------------------------------------------------- tests


def test_upload_url_put_and_wait(
    staged: Sandbox, s3: Any, transfer_settings: TransferSettings
) -> None:
    data = os.urandom(10 * MIB)
    ticket = staged.files.upload_url("m9/upload.bin")
    started = time.perf_counter()
    status, _ = http(ticket, method="PUT", data=data, headers=ticket.headers)
    assert status == 200
    put_seconds = time.perf_counter() - started
    entry = ticket.wait(timeout=WAIT_SECONDS)
    total = time.perf_counter() - started
    report("upload_url 10 MiB PUT MB/s (dev -> S3)", f"{rate(len(data), put_seconds):.2f}")
    report("upload_url 10 MiB PUT -> wait() s", f"{total:.2f}")
    assert entry.size == len(data)
    assert remote_sha256(staged, f"{HOME}/m9/upload.bin") == sha256(data)
    assert staged.commands.run(f"stat -c %u {HOME}/m9/upload.bin").stdout.strip() == "1000"
    assert staged_objects(s3, transfer_settings, staged.sandbox_id) == []


def test_barrier_without_wait(staged: Sandbox) -> None:
    first = os.urandom(MIB)
    ticket = staged.files.upload_url("m9/barrier-read.bin")
    assert http(ticket, method="PUT", data=first, headers=ticket.headers)[0] == 200
    started = time.perf_counter()
    assert staged.files.read("m9/barrier-read.bin", format="bytes") == first
    report("PUT -> files.read visible s (barrier)", f"{time.perf_counter() - started:.2f}")
    second = os.urandom(MIB)
    other = staged.files.upload_url("m9/barrier-run.bin")
    assert http(other, method="PUT", data=second, headers=other.headers)[0] == 200
    started = time.perf_counter()
    assert remote_sha256(staged, f"{HOME}/m9/barrier-run.bin") == sha256(second)
    report("PUT -> commands.run visible s (barrier)", f"{time.perf_counter() - started:.2f}")


def test_download_url_snapshot_and_range(staged: Sandbox) -> None:
    staged.commands.run(
        f"mkdir -p {HOME}/m9 && dd if=/dev/urandom of={HOME}/m9/big.bin bs=1M count=50 status=none"
    )
    original = remote_sha256(staged, f"{HOME}/m9/big.bin")
    started = time.perf_counter()
    link = staged.files.download_url("m9/big.bin")
    export_seconds = time.perf_counter() - started
    assert link.sha256 == original
    started = time.perf_counter()
    status, body = http(link)
    report("download_url export s (50 MB)", f"{export_seconds:.2f}")
    report(
        "download_url GET MB/s (S3 -> dev)", f"{rate(len(body), time.perf_counter() - started):.2f}"
    )
    assert status == 200 and sha256(body) == original
    ranged_status, ranged = http(link, headers={"Range": "bytes=0-99"})
    assert (ranged_status, len(ranged)) == (206, 100)
    staged.commands.run(f"head -c 1000 /dev/urandom > {HOME}/m9/big.bin")
    assert sha256(http(link)[1]) == original


def test_large_files_route_through_s3(
    staged: Sandbox,
    control_plane: LambdaMicrovmsControlPlane,
    monkeypatch: pytest.MonkeyPatch,
    s3: Any,
    transfer_settings: TransferSettings,
) -> None:
    with plain_connection(staged, control_plane, monkeypatch) as plain:
        calls: list[str] = []
        original_import = plain._files.StartImport

        def spy(*args: Any, **kwargs: Any) -> Any:
            calls.append("StartImport")
            return original_import(*args, **kwargs)

        plain._files.StartImport = spy
        baseline = os.urandom(GRPC_BASELINE_BYTES)
        started = time.perf_counter()
        plain.files.write("m9/baseline.bin", baseline)
        grpc_write = rate(len(baseline), time.perf_counter() - started)
        started = time.perf_counter()
        assert plain.files.read("m9/baseline.bin", format="bytes") == baseline
        grpc_read = rate(len(baseline), time.perf_counter() - started)
        assert calls == []
    data = os.urandom(LARGE_BYTES)
    started = time.perf_counter()
    staged.files.write("m9/large.bin", data)
    routed_write = rate(len(data), time.perf_counter() - started)
    started = time.perf_counter()
    assert staged.files.read("m9/large.bin", format="bytes") == data
    routed_read = rate(len(data), time.perf_counter() - started)
    report("gRPC baseline write/read MB/s (20 MB)", f"{grpc_write:.2f} / {grpc_read:.2f}")
    link_put, link_get = link_rates(s3, transfer_settings, staged.sandbox_id)
    report("S3-routed write/read MB/s (200 MB)", f"{routed_write:.2f} / {routed_read:.2f}")
    report("developer <-> S3 link PUT/GET MB/s (20 MB)", f"{link_put:.2f} / {link_get:.2f}")
    assert routed_write >= routed_floor(grpc_write, link_put)
    assert routed_read >= routed_floor(grpc_read, link_get)


def test_multipart_export(
    e2e_settings: E2ESettings,
    control_plane: LambdaMicrovmsControlPlane,
    template_arn: str,
    transfer_settings: TransferSettings,
    s3: Any,
) -> None:
    created = create_staged(
        e2e_settings,
        control_plane,
        template_arn,
        transfer_settings.staging(multipart_threshold_bytes=64 * MIB),
    )
    try:
        created.commands.run(
            f"mkdir -p {HOME}/m9 && "
            f"dd if=/dev/urandom of={HOME}/m9/parts.bin bs=1000000 count=100 status=none"
        )
        started = time.perf_counter()
        link = created.files.download_url("m9/parts.bin")
        report("multipart export 100 MB s", f"{time.perf_counter() - started:.2f}")
        assert sha256(http(link)[1]) == remote_sha256(created, f"{HOME}/m9/parts.bin")
        assert open_uploads(s3, transfer_settings, created.sandbox_id) == []
    finally:
        with contextlib.suppress(SandboxNotFoundException):
            created.kill()
        clean_staging(s3, transfer_settings, created.sandbox_id)


def test_expiry(staged: Sandbox) -> None:
    ticket = staged.files.upload_url("m9/never.bin", expires_in=5)
    started = time.perf_counter()
    with pytest.raises(TimeoutException):
        ticket.wait(timeout=60)
    elapsed = time.perf_counter() - started
    report("upload_url(expires_in=5) -> TimeoutException s", f"{elapsed:.2f}")
    assert 3.0 <= elapsed <= 12.0
    staged.files.write("m9/short.txt", "caduca")
    link = staged.files.download_url("m9/short.txt", expires_in=1)
    time.sleep(3)
    status, body = http(link)
    assert status == 403 and b"Request has expired" in body


def test_size_caps(staged: Sandbox, s3: Any, transfer_settings: TransferSettings) -> None:
    capped = staged.files.upload_url("m9/capped.bin", max_bytes=MIB)
    assert http(capped, method="PUT", data=os.urandom(2 * MIB), headers=capped.headers)[0] == 200
    with pytest.raises(InvalidArgumentException) as raised:
        capped.wait(timeout=WAIT_SECONDS)
    assert str(raised.value).startswith("too_large")
    assert not staged.files.exists("m9/capped.bin")
    assert staged_objects(s3, transfer_settings, staged.sandbox_id) == []
    form = staged.files.upload_url("m9/form.bin", form=True, max_bytes=1024)
    status, body = post_form(form, form.fields, os.urandom(2048))
    assert status == 400 and b"EntityTooLarge" in body
    small = os.urandom(512)
    assert post_form(form, form.fields, small)[0] in (200, 204)
    assert form.wait(timeout=WAIT_SECONDS).size == 512
    assert staged.files.read("m9/form.bin", format="bytes") == small


def test_ssrf_policy(staged: Sandbox, control_plane: LambdaMicrovmsControlPlane) -> None:
    staging = staged.transfer
    assert staging is not None
    target = new_staging_object(staging, staged.sandbox_id, control_plane.region, "up")
    gateway = S3Gateway.from_session(None, control_plane.region)
    valid_get = gateway.presign_get(target, 300)
    valid_delete = gateway.presign_delete(target, 300)
    host = f"{target.bucket}.s3.{control_plane.region}.amazonaws.com"
    path_and_query = valid_get.split(host, 1)[1]
    variants = {
        "imds": f"https://169.254.169.254{path_and_query}",
        "ip_literal": f"https://52.216.0.1{path_and_query}",
        "http": valid_get.replace("https://", "http://", 1),
        "global_host": valid_get.replace(host, f"{target.bucket}.s3.amazonaws.com", 1),
        "other_bucket": valid_get.replace(host, f"other-{host}", 1),
        "other_key": valid_get.replace(target.key.rsplit("/", 1)[1], "0" * 32, 1),
        "userinfo": valid_get.replace("https://", "https://user:pw@", 1),
        "port_8443": valid_get.replace(host, f"{host}:8443", 1),
    }
    baseline = connections(staged.commands.run("ss -Htan").stdout)
    sampler = staged.commands.run(
        "for i in $(seq 1 100); do ss -Htan >> /tmp/m9-ss.log; sleep 0.05; done",
        background=True,
    )
    expires = unix_ms(datetime.now(UTC) + timedelta(minutes=5))
    for name, url in variants.items():
        request = filesystem_pb2.StartImportRequest(
            path="m9/ssrf.bin",
            object=target.to_proto(),
            get=filesystem_pb2.PresignedRequest(url=url),
            wait_for_object=True,
            expires_at_unix_ms=expires,
        )
        request.delete.CopyFrom(filesystem_pb2.PresignedRequest(url=valid_delete))
        with pytest.raises(grpc.RpcError) as raised:
            staged._files.StartImport(request, timeout=30)
        assert raised.value.code() is grpc.StatusCode.INVALID_ARGUMENT, name
    sampler.wait()
    opened = connections(staged.files.read("/tmp/m9-ss.log")) - baseline
    report("sockets opened during the SSRF calls", len(opened))
    for _, peer in opened:
        host, _, port = peer.rpartition(":")
        assert host not in ("169.254.169.254", "52.216.0.1"), peer
        assert port != "8443", peer


def test_suspend_requeue(staged: Sandbox) -> None:
    data = os.urandom(MIB)
    ticket = staged.files.upload_url("m9/suspended.bin")
    assert staged.pause()
    assert http(ticket, method="PUT", data=data, headers=ticket.headers)[0] == 200
    staged.connect()
    entry = ticket.wait(timeout=WAIT_SECONDS)
    assert entry.size == len(data)
    assert remote_sha256(staged, f"{HOME}/m9/suspended.bin") == sha256(data)


def test_gzip(
    staged: Sandbox, control_plane: LambdaMicrovmsControlPlane, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = ("línea compresible de prueba " * 40 + "\n") * (20_000_000 // 1200)
    data = text.encode()
    with plain_connection(staged, control_plane, monkeypatch) as plain:
        started = time.perf_counter()
        plain.files.write("m9/plain.txt", data)
        plain.files.read("m9/plain.txt", format="bytes")
        plain_seconds = time.perf_counter() - started
        started = time.perf_counter()
        plain.files.write("m9/gzip.txt", data, gzip=True)
        assert plain.files.read("m9/gzip.txt", format="bytes", gzip=True) == data
        gzip_seconds = time.perf_counter() - started
    report(
        "gzip write+read MB/s (plain / gzip)",
        f"{rate(2 * len(data), plain_seconds):.2f} / {rate(2 * len(data), gzip_seconds):.2f}",
    )
    assert remote_sha256(staged, f"{HOME}/m9/gzip.txt") == sha256(data)
    assert plain_seconds >= GZIP_SPEEDUP * gzip_seconds


def test_metadata(staged: Sandbox) -> None:
    staged.files.write("m9/tagged.txt", "x", metadata={"Owner": "alice"})
    assert dict(staged.files.get_info("m9/tagged.txt").metadata) == {"owner": "alice"}
    [listed] = [entry for entry in staged.files.list("m9") if entry.name == "tagged.txt"]
    assert dict(listed.metadata) == {"owner": "alice"}
    staged.files.write("m9/tagged.txt", "y")
    assert dict(staged.files.get_info("m9/tagged.txt").metadata) == {}
    with pytest.raises(InvalidArgumentException):
        staged.files.write("m9/tagged.txt", "z", metadata={"a b": "c"})


def test_e2b_shim(
    staged: Sandbox,
    control_plane: LambdaMicrovmsControlPlane,
    transfer_settings: TransferSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rayito.e2b import AsyncSandbox as E2BAsyncSandbox
    from rayito.e2b import Sandbox as E2BSandbox

    monkeypatch.setenv("RAYITO_TRANSFER_BUCKET", transfer_settings.bucket)
    monkeypatch.setenv("RAYITO_TRANSFER_PREFIX", transfer_settings.prefix)
    shim = E2BSandbox.connect(
        staged.sandbox_id, access_token=staged.access_token, control_plane=control_plane
    )
    data = os.urandom(4096)
    url = shim.upload_url("m9/e2b.bin")
    assert (
        http(url, method="PUT", data=data, headers={"Content-Type": "application/octet-stream"})[0]
        == 200
    )
    assert shim.files.read("m9/e2b.bin", format="bytes") == data
    assert http(shim.download_url("m9/e2b.bin"))[1] == data
    with pytest.raises(InvalidArgumentException):
        shim.upload_url("m9/e2b.bin", use_signature_expiration=-1)

    async def run_async() -> None:
        async_shim = await E2BAsyncSandbox.connect(
            staged.sandbox_id, access_token=staged.access_token, control_plane=control_plane
        )
        async_url = await async_shim.upload_url("m9/e2b-async.bin")
        assert http(async_url, method="PUT", data=data)[0] == 200
        assert await async_shim.files.read("m9/e2b-async.bin", format="bytes") == data
        assert http(await async_shim.download_url("m9/e2b-async.bin"))[1] == data
        with pytest.raises(InvalidArgumentException):
            await async_shim.download_url("m9/e2b-async.bin", use_signature_expiration=-1)

    asyncio.run(run_async())


def test_log_hygiene(
    e2e_settings: E2ESettings,
    control_plane: LambdaMicrovmsControlPlane,
    template_arn: str,
    transfer_settings: TransferSettings,
    s3: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    role = e2e_settings.execution_role_arn
    if role is None:
        pytest.skip("exporta RAYITO_EXECUTION_ROLE_ARN (el rol de sólo logs) para este test")
    caplog.set_level("DEBUG", logger="rayito")
    created = create_staged(
        e2e_settings,
        control_plane,
        template_arn,
        transfer_settings.staging(),
        execution_role_arn=role,
    )
    try:
        ticket = created.files.upload_url("m9/secret-dir/in.bin")
        assert http(ticket, method="PUT", data=b"hola", headers=ticket.headers)[0] == 200
        ticket.wait(timeout=WAIT_SECONDS)
        link = created.files.download_url("m9/secret-dir/in.bin")
        assert http(link)[1] == b"hola"
        rayd_lines = "\n".join(rayd_log_lines(control_plane, created))
        sdk_lines = "\n".join(
            record.getMessage() for record in caplog.records if record.name.startswith("rayito")
        )
        report("rayd log lines read", len(rayd_lines.splitlines()))
        for text in (rayd_lines, sdk_lines):
            for forbidden in (
                "X-Amz-Signature",
                transfer_settings.bucket,
                f"{transfer_settings.prefix}/",
                "secret-dir",
            ):
                assert forbidden not in text
    finally:
        with contextlib.suppress(SandboxNotFoundException):
            created.kill()
        clean_staging(s3, transfer_settings, created.sandbox_id)


def test_async_upload_url_put_and_wait(
    staged: Sandbox, control_plane: LambdaMicrovmsControlPlane, transfer_settings: TransferSettings
) -> None:
    async def run() -> None:
        sandbox = await AsyncSandbox.connect(
            staged.sandbox_id,
            access_token=staged.access_token,
            control_plane=control_plane,
            transfer=transfer_settings.staging(),
        )
        try:
            data = os.urandom(MIB)
            ticket = await sandbox.files.upload_url("m9/async.bin")
            status = await asyncio.to_thread(
                http, ticket, method="PUT", data=data, headers=ticket.headers
            )
            assert status[0] == 200
            assert (await ticket.wait(timeout=WAIT_SECONDS)).size == len(data)
        finally:
            await sandbox.close()

    asyncio.run(run())
