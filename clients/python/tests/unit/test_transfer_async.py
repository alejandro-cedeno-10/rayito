"""La superficie de transferencias de `AsyncSandbox` (misma que la síncrona
sobre `grpc.aio`) contra el `rayd` falso con transferencias y el S3 falso,
más la paridad de firmas con `Sandbox`."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import pickle
import threading
import time
from collections.abc import AsyncIterator, Iterator
from typing import Any

import grpc
import pytest

from rayito import (
    AsyncSandbox,
    AsyncUploadTicket,
    DownloadLink,
    S3Prefix,
    S3Staging,
    Sandbox,
    UploadTicket,
    WriteEntry,
)
from rayito._aws import LambdaMicrovmsControlPlane
from rayito.exceptions import (
    FileNotFoundException,
    FileUploadException,
    InvalidArgumentException,
    TimeoutException,
    TransferException,
    UnimplementedError,
)
from rayito.sandbox_async.filesystem import AsyncFilesystem
from rayito.sandbox_sync.filesystem import Filesystem
from rayito.v1 import filesystem_pb2, filesystem_pb2_grpc

from .conftest import (
    ACCESS_TOKEN,
    IMAGE_ARN,
    SANDBOX_ID,
    RaydEndpoint,
    StubbedControlPlane,
    always_running,
)
from .fake_s3 import BUCKET, FAKE_ACCESS_KEY
from .fake_transfer import FakeTransferFilesystemService
from .transfer_support import (
    MIB,
    STAGING,
    non_seekable,
    spy_on_files,
    start_transfer_rayd,
    stub_launch,
    stub_terminate,
    transfer_files,
    url_query,
)

HOME = "/home/user"
FILES_STUB = filesystem_pb2_grpc.FilesystemServiceStub
TICKET_TIMEOUT = 5.0
TRANSFER_METHODS = ("read", "write", "write_files", "upload_url", "download_url")


@pytest.fixture
def transfer_rayd() -> Iterator[RaydEndpoint]:
    server, endpoint = start_transfer_rayd()
    try:
        yield endpoint
    finally:
        server.stop(grace=None)


@pytest.fixture
def fake(transfer_rayd: RaydEndpoint) -> FakeTransferFilesystemService:
    return transfer_files(transfer_rayd)


async def launch(
    control_plane: StubbedControlPlane, endpoint: RaydEndpoint, *, transfer: S3Staging | None
) -> AsyncSandbox:
    stub_launch(control_plane, endpoint)
    return await AsyncSandbox.create(
        IMAGE_ARN,
        idle=None,
        access_token=ACCESS_TOKEN,
        control_plane=control_plane.plane,
        transport=endpoint.transport,
        session=transfer_files(endpoint).s3.session(),
        transfer=transfer,
    )


@pytest.fixture
async def sandbox(
    control_plane: StubbedControlPlane, transfer_rayd: RaydEndpoint
) -> AsyncIterator[AsyncSandbox]:
    created = await launch(control_plane, transfer_rayd, transfer=STAGING)
    try:
        yield created
    finally:
        stub_terminate(control_plane)
        await created.kill()


@pytest.fixture
async def plain_sandbox(
    control_plane: StubbedControlPlane, transfer_rayd: RaydEndpoint
) -> AsyncIterator[AsyncSandbox]:
    created = await launch(control_plane, transfer_rayd, transfer=None)
    try:
        yield created
    finally:
        stub_terminate(control_plane)
        await created.kill()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def put_in_thread(fake: FakeTransferFilesystemService, url: str, data: bytes) -> None:
    await asyncio.to_thread(fake.s3.put_url, url, data)


# ------------------------------------------------------------------- parity


def test_async_transfer_surface_matches_sync() -> None:
    for name in TRANSFER_METHODS:
        sync_parameters = inspect.signature(getattr(Filesystem, name)).parameters
        async_parameters = inspect.signature(getattr(AsyncFilesystem, name)).parameters
        assert list(sync_parameters) == list(async_parameters), name
        assert inspect.iscoroutinefunction(getattr(AsyncFilesystem, name)), name
    for name in ("upload_url", "download_url"):
        assert list(inspect.signature(getattr(Sandbox, name)).parameters) == list(
            inspect.signature(getattr(AsyncSandbox, name)).parameters
        )
    for factory in ("create", "_class_connect"):
        assert "transfer" in inspect.signature(getattr(Sandbox, factory)).parameters
        assert "transfer" in inspect.signature(getattr(AsyncSandbox, factory)).parameters
    for attribute in ("url", "method", "headers", "fields", "path", "expires_at", "transfer_id"):
        assert hasattr(UploadTicket, attribute) and hasattr(AsyncUploadTicket, attribute)
    for method in ("wait", "status", "cancel"):
        assert not inspect.iscoroutinefunction(getattr(UploadTicket, method))
        assert inspect.iscoroutinefunction(getattr(AsyncUploadTicket, method))
    assert isinstance(inspect.getattr_static(AsyncSandbox, "transfer"), property)


# --------------------------------------------------------------- upload_url


async def test_async_upload_url_without_staging_is_unimplemented(
    plain_sandbox: AsyncSandbox, fake: FakeTransferFilesystemService
) -> None:
    with pytest.raises(UnimplementedError, match="RAYITO_TRANSFER_BUCKET"):
        await plain_sandbox.files.upload_url("a.bin")
    assert fake.rpc_names() == []


async def test_async_older_agent_is_detected_once(
    sandbox: AsyncSandbox, fake: FakeTransferFilesystemService
) -> None:
    fake.unimplemented = True
    for _ in range(2):
        with pytest.raises(UnimplementedError, match="actualiza la imagen"):
            await sandbox.files.upload_url("a.bin")
    assert fake.rpc_names() == ["GetTransfer"]


async def test_async_ticket_imports_on_wait(
    sandbox: AsyncSandbox, fake: FakeTransferFilesystemService
) -> None:
    ticket = await sandbox.files.upload_url("inbox/data.bin")
    assert isinstance(ticket, AsyncUploadTicket) and ticket == ticket.url
    assert "X-Amz-Signature" not in repr(ticket)
    with pytest.raises(TypeError):
        pickle.dumps(ticket)
    assert (await ticket.status()).phase == "waiting"
    await put_in_thread(fake, ticket, b"async payload")
    entry = await ticket.wait(timeout=TICKET_TIMEOUT)
    assert (entry.path, entry.size) == (f"{HOME}/inbox/data.bin", len(b"async payload"))
    assert fake.s3.keys() == []
    assert fake.import_requests[0].object.key.split("/")[1:3] == [SANDBOX_ID, "up"]


async def test_async_ticket_expiry_timeout_and_cancel(
    sandbox: AsyncSandbox, fake: FakeTransferFilesystemService
) -> None:
    expiring = await sandbox.files.upload_url("late.bin", expires_in=1)
    with pytest.raises(TimeoutException) as raised:
        await expiring.wait(timeout=TICKET_TIMEOUT)
    assert str(raised.value).startswith("expired")
    armed = await sandbox.files.upload_url("armed.bin")
    with pytest.raises(TimeoutException):
        await armed.wait(timeout=0.3)
    await armed.cancel()
    with pytest.raises(FileUploadException) as cancelled:
        await armed.wait(timeout=TICKET_TIMEOUT)
    assert cancelled.value.code == "cancelled"


async def test_async_too_large_put_fails_the_ticket(
    sandbox: AsyncSandbox, fake: FakeTransferFilesystemService
) -> None:
    ticket = await sandbox.files.upload_url("capped.bin", max_bytes=2)
    await put_in_thread(fake, ticket, b"abc")
    with pytest.raises(InvalidArgumentException) as raised:
        await ticket.wait(timeout=TICKET_TIMEOUT)
    assert str(raised.value).startswith("too_large")


async def test_async_wait_reopens_the_watch_after_a_suspend(
    sandbox: AsyncSandbox,
    fake: FakeTransferFilesystemService,
    transfer_rayd: RaydEndpoint,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    always_running(monkeypatch, sandbox, transfer_rayd.host)
    ticket = await sandbox.files.upload_url("paused.bin")
    waiter = asyncio.create_task(ticket.wait(timeout=10))
    while "WatchTransfer" not in fake.rpc_names():
        await asyncio.sleep(0.02)
    transfer_rayd.suspend()
    await put_in_thread(fake, ticket, b"while suspended")
    await asyncio.sleep(0.2)
    transfer_rayd.resume()
    entry = await asyncio.wait_for(waiter, 10)
    assert entry.size == len(b"while suspended")
    assert fake.watch_cuts >= 1


async def test_async_sandbox_level_urls_take_the_e2b_shape(
    sandbox: AsyncSandbox, fake: FakeTransferFilesystemService
) -> None:
    ticket = await sandbox.upload_url("e2b.bin", use_signature_expiration=60)
    assert isinstance(ticket, AsyncUploadTicket)
    await put_in_thread(fake, ticket, b"hola")
    await ticket.wait(timeout=TICKET_TIMEOUT)
    link = await sandbox.download_url("e2b.bin")
    assert isinstance(link, DownloadLink) and link.size == 4
    with pytest.raises(InvalidArgumentException, match="use_signature_expiration"):
        await sandbox.upload_url("e2b.bin", use_signature_expiration=-1)


# ------------------------------------------------------------- download_url


async def test_async_download_url_missing_file_and_single_put(
    sandbox: AsyncSandbox, fake: FakeTransferFilesystemService
) -> None:
    with pytest.raises(FileNotFoundException):
        await sandbox.files.download_url("nope.bin")
    assert fake.export_requests == [] and fake.s3.calls == []
    data = b"snapshot" * 10
    fake.add_file("report.txt", data)
    link = await sandbox.files.download_url("report.txt", filename="r.txt")
    assert (link.size, link.sha256) == (len(data), sha256(data))
    assert fake.s3.get_url(link) == (200, data)
    assert url_query(link)["response-content-disposition"].startswith(
        'attachment; filename="r.txt"'
    )


async def test_async_multipart_export_completes_and_aborts(
    sandbox: AsyncSandbox, fake: FakeTransferFilesystemService
) -> None:
    data = b"m" * (16 * MIB + 3)
    fake.add_file("big.bin", data)
    link = await sandbox.files.download_url("big.bin")
    assert fake.s3.get_url(link) == (200, data)
    assert len(fake.s3.completed_uploads) == 1
    fake.export_failure = ("unavailable", "s3_unavailable")
    with pytest.raises(TransferException) as raised:
        await sandbox.files.download_url("big.bin")
    assert raised.value.reason == "s3_unavailable"
    assert len(fake.s3.aborted_uploads) == 1


# ------------------------------------------------------------------ routing


async def test_async_large_writes_and_reads_route_through_s3(
    sandbox: AsyncSandbox, fake: FakeTransferFilesystemService
) -> None:
    data = bytes(range(256)) * (MIB // 256)
    entries = await sandbox.files.write_files(
        [
            WriteEntry("small.txt", "s"),
            WriteEntry("big.bin", data),
            WriteEntry("pipe.bin", non_seekable(b"pipe")),
        ],
        metadata={"Kind": "blob"},
    )
    assert [entry.name for entry in entries] == ["small.txt", "big.bin", "pipe.bin"]
    assert [dict(entry.metadata) for entry in entries] == [{"kind": "blob"}] * 3
    assert [request.expected_sha256 for request in fake.import_requests] == [
        sha256(data),
        sha256(b"pipe"),
    ]
    assert len(fake.write_streams) == 1
    assert await sandbox.files.read("big.bin", format="bytes") == data
    chunks = [chunk async for chunk in await sandbox.files.read("big.bin", format="stream")]
    assert b"".join(chunks) == data
    assert fake.read_calls == 0
    assert fake.s3.keys() == []


async def test_async_without_staging_nothing_is_probed(
    plain_sandbox: AsyncSandbox, fake: FakeTransferFilesystemService
) -> None:
    data = b"p" * (2 * MIB)
    await plain_sandbox.files.write("big.bin", data)
    assert await plain_sandbox.files.read("big.bin", format="bytes") == data
    assert fake.rpc_names() == []


async def test_async_tampered_large_read_is_a_checksum_mismatch(
    sandbox: AsyncSandbox, fake: FakeTransferFilesystemService, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake.add_file("big.bin", b"r" * MIB)
    original = fake.s3.put_url
    monkeypatch.setattr(fake.s3, "put_url", lambda url, data: original(url, data + b"!"))
    with pytest.raises(TransferException) as raised:
        await sandbox.files.read("big.bin", format="bytes")
    assert raised.value.reason == "checksum_mismatch"


# ------------------------------------------------------ gzip, metadata, idle


async def test_async_gzip_and_metadata_on_the_wire(
    sandbox: AsyncSandbox, fake: FakeTransferFilesystemService
) -> None:
    spy = spy_on_files(sandbox, FILES_STUB)
    text = "compresible " * 1000
    written = await sandbox.files.write("z.txt", text, gzip=True, metadata={"Owner": "bob"})
    assert dict(written.metadata) == {"owner": "bob"}
    assert await sandbox.files.read("z.txt", gzip=True) == text
    assert [kwargs.get("compression") for kwargs in spy.kwargs_of("Write")] == [
        grpc.Compression.Gzip
    ]
    assert fake.metadata["Read"][0].get("rayito-compress") == "gzip"
    assert dict((await sandbox.files.get_info("z.txt")).metadata) == {"owner": "bob"}
    await sandbox.files.write("z.txt", "plain")
    assert dict((await sandbox.files.get_info("z.txt")).metadata) == {}
    with pytest.raises(InvalidArgumentException, match="metadatos inválidos"):
        await sandbox.files.write("z.txt", "x", metadata={"a b": "c"})


class StalledAioRead:
    """Un `Read` de `grpc.aio` que entrega un chunk y luego calla."""

    def __init__(self) -> None:
        self.cancelled = False

    async def read(self) -> Any:
        await asyncio.sleep(10)
        return None

    def cancel(self) -> bool:
        self.cancelled = True
        return True


async def test_async_stream_idle_timeout_cancels_a_stalled_read(
    sandbox: AsyncSandbox, fake: FakeTransferFilesystemService, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake.add_file("slow.txt", b"abc")
    stalled = StalledAioRead()

    async def open_stream(*args: Any, **kwargs: Any) -> tuple[Any, Any]:
        return stalled, filesystem_pb2.ReadResponse(chunk=b"a")

    monkeypatch.setattr(sandbox, "_open_stream", open_stream)
    with pytest.raises(TimeoutException, match="stream_idle_timeout"):
        await sandbox.files.read("slow.txt", stream_idle_timeout=0.2)
    assert stalled.cancelled


class StalledS3:
    """Una pata S3 colgada: la subida no vuelve y el cuerpo de `get_object`
    no entrega el siguiente trozo hasta que lo cierran."""

    def __init__(self) -> None:
        self.released = threading.Event()
        self.body_closed = threading.Event()

    def upload(self, *args: Any, **kwargs: Any) -> None:
        self.released.wait(10)

    def next_chunk(self, *args: Any) -> bytes:
        self.body_closed.wait(10)
        raise ValueError("cuerpo cerrado")

    def close_body(self, *args: Any) -> None:
        self.body_closed.set()


@pytest.fixture
def stalled_s3(monkeypatch: pytest.MonkeyPatch) -> Iterator[StalledS3]:
    stalled = StalledS3()
    monkeypatch.setattr("rayito._s3.S3Gateway.upload", stalled.upload)
    monkeypatch.setattr("rayito._s3.ObjectChunks.__next__", stalled.next_chunk)
    monkeypatch.setattr("rayito._s3.ObjectChunks.close", stalled.close_body)
    try:
        yield stalled
    finally:
        stalled.released.set()
        stalled.body_closed.set()


async def test_async_request_timeout_bounds_a_stalled_s3_upload(
    sandbox: AsyncSandbox, fake: FakeTransferFilesystemService, stalled_s3: StalledS3
) -> None:
    started = time.monotonic()
    with pytest.raises(TimeoutException, match="plazo"):
        await sandbox.files.write("big.bin", b"u" * MIB, request_timeout=0.5)
    assert time.monotonic() - started < 5
    assert fake.import_requests == []


async def test_async_request_timeout_bounds_a_stalled_s3_download(
    sandbox: AsyncSandbox, fake: FakeTransferFilesystemService, stalled_s3: StalledS3
) -> None:
    fake.add_file("big.bin", b"d" * MIB)
    started = time.monotonic()
    with pytest.raises(TimeoutException, match="plazo"):
        await sandbox.files.read("big.bin", format="bytes", request_timeout=1.0)
    assert time.monotonic() - started < 5
    assert stalled_s3.body_closed.wait(5)
    assert fake.s3.keys() == []


async def test_async_stream_idle_timeout_bounds_a_stalled_s3_download(
    sandbox: AsyncSandbox, fake: FakeTransferFilesystemService, stalled_s3: StalledS3
) -> None:
    fake.add_file("big.txt", b"t" * MIB)
    started = time.monotonic()
    with pytest.raises(TimeoutException, match="stream_idle_timeout"):
        await sandbox.files.read("big.txt", stream_idle_timeout=0.2)
    assert time.monotonic() - started < 5
    assert stalled_s3.body_closed.wait(5)


async def test_async_overlapping_transfer_and_persist_fail_before_aws(
    control_plane: StubbedControlPlane, transfer_rayd: RaydEndpoint
) -> None:
    with pytest.raises(InvalidArgumentException, match="disjuntos"):
        await AsyncSandbox.create(
            IMAGE_ARN,
            execution_role_arn="arn:aws:iam::123456789012:role/rayito-execution",
            persist=S3Prefix(bucket=BUCKET, prefix="homes"),
            transfer=S3Staging(bucket=BUCKET, prefix="homes"),
            access_token=ACCESS_TOKEN,
            control_plane=control_plane.plane,
            transport=transfer_rayd.transport,
        )


async def test_async_a_plane_built_from_a_session_signs_with_that_session(
    control_plane: StubbedControlPlane, transfer_rayd: RaydEndpoint
) -> None:
    signing = transfer_files(transfer_rayd).s3.session()
    plane = LambdaMicrovmsControlPlane(
        control_plane.microvms.client, sts_client=control_plane.sts.client, session=signing
    )
    stub_launch(control_plane, transfer_rayd)
    sandbox = await AsyncSandbox.create(
        IMAGE_ARN,
        idle=None,
        access_token=ACCESS_TOKEN,
        control_plane=plane,
        transport=transfer_rayd.transport,
        transfer=STAGING,
    )
    try:
        assert sandbox._session is signing
        ticket = await sandbox.files.upload_url("a.bin")
        assert url_query(ticket.url)["X-Amz-Credential"].startswith(f"{FAKE_ACCESS_KEY}/")
    finally:
        stub_terminate(control_plane)
        await sandbox.kill()
