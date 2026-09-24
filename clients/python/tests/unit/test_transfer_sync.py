"""`files.upload_url`/`download_url`, el enrutado de ficheros grandes por S3,
`gzip`, metadatos y `stream_idle_timeout` de `Sandbox` contra el `rayd`
falso con transferencias (gRPC real en loopback) y un S3 falso detrás de un
cliente boto3 real (`openspec/changes/m9-file-transfer/design.md` D23)."""

from __future__ import annotations

import hashlib
import logging
import pickle
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import grpc
import pytest

from rayito import (
    DownloadLink,
    S3Prefix,
    S3Staging,
    Sandbox,
    TransferStatus,
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
from rayito.v1 import filesystem_pb2, filesystem_pb2_grpc

from .conftest import (
    ACCESS_TOKEN,
    IMAGE_ARN,
    SANDBOX_ID,
    RaydEndpoint,
    StubbedControlPlane,
    always_running,
)
from .fake_s3 import BUCKET, FAKE_ACCESS_KEY, REGION
from .fake_transfer import FakeTransferFilesystemService
from .transfer_support import (
    MIB,
    REGIONAL_HOST,
    STAGING,
    non_seekable,
    spy_on_files,
    start_transfer_rayd,
    stub_launch,
    stub_terminate,
    transfer_files,
    url_host,
    url_query,
    wait_until,
)

HOME = "/home/user"
FILES_STUB = filesystem_pb2_grpc.FilesystemServiceStub
TICKET_TIMEOUT = 5.0


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


def launch(
    control_plane: StubbedControlPlane,
    endpoint: RaydEndpoint,
    *,
    transfer: S3Staging | None,
) -> Sandbox:
    stub_launch(control_plane, endpoint)
    return Sandbox.create(
        IMAGE_ARN,
        idle=None,
        access_token=ACCESS_TOKEN,
        control_plane=control_plane.plane,
        transport=endpoint.transport,
        session=transfer_files(endpoint).s3.session(),
        transfer=transfer,
    )


@pytest.fixture
def sandbox(control_plane: StubbedControlPlane, transfer_rayd: RaydEndpoint) -> Iterator[Sandbox]:
    created = launch(control_plane, transfer_rayd, transfer=STAGING)
    try:
        yield created
    finally:
        stub_terminate(control_plane)
        created.kill()


@pytest.fixture
def plain_sandbox(
    control_plane: StubbedControlPlane, transfer_rayd: RaydEndpoint
) -> Iterator[Sandbox]:
    created = launch(control_plane, transfer_rayd, transfer=None)
    try:
        yield created
    finally:
        stub_terminate(control_plane)
        created.kill()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ----------------------------------------------------------------- upload_url


def test_upload_url_without_staging_is_unimplemented_before_any_rpc(
    plain_sandbox: Sandbox, fake: FakeTransferFilesystemService
) -> None:
    assert plain_sandbox.transfer is None
    with pytest.raises(UnimplementedError, match=r"configura transfer=S3Staging\(\.\.\.\)"):
        plain_sandbox.files.upload_url("a.bin")
    with pytest.raises(UnimplementedError, match="RAYITO_TRANSFER_BUCKET"):
        plain_sandbox.files.download_url("a.bin")
    assert fake.rpc_names() == []


def test_an_older_agent_is_detected_once_and_cached(
    sandbox: Sandbox, fake: FakeTransferFilesystemService
) -> None:
    fake.unimplemented = True
    for _ in range(2):
        with pytest.raises(UnimplementedError, match="actualiza la imagen"):
            sandbox.files.upload_url("a.bin")
    with pytest.raises(UnimplementedError, match="actualiza la imagen"):
        sandbox.files.write("m.txt", "x", metadata={"k": "v"})
    assert fake.rpc_names() == ["GetTransfer"]
    assert fake.write_streams == []


def test_upload_ticket_is_armed_before_the_put_and_imports_on_wait(
    sandbox: Sandbox, fake: FakeTransferFilesystemService
) -> None:
    before = datetime.now(UTC)
    ticket = sandbox.files.upload_url("inbox/data.bin")
    assert isinstance(ticket, UploadTicket) and isinstance(ticket, str)
    assert ticket == ticket.url
    assert (ticket.method, ticket.headers, ticket.fields) == (
        "PUT",
        {"Content-Type": "application/octet-stream"},
        {},
    )
    assert ticket.path == "inbox/data.bin"
    assert before + timedelta(seconds=3590) <= ticket.expires_at <= before + timedelta(seconds=3610)
    [request] = fake.import_requests
    assert request.wait_for_object is True
    assert request.max_bytes == 0 and request.expected_sha256 == ""
    assert (request.object.bucket, request.object.region) == (BUCKET, REGION)
    prefix, sandbox_id, direction, token = request.object.key.split("/")
    assert (prefix, sandbox_id, direction, len(token)) == ("rayito-transfer", SANDBOX_ID, "up", 32)
    assert "inbox" not in request.object.key and "data.bin" not in request.object.key
    for url in (request.get.url, request.delete.url, ticket.url):
        assert url_host(url) == REGIONAL_HOST
        assert url_query(url)["X-Amz-Algorithm"] == "AWS4-HMAC-SHA256"
    assert abs(request.expires_at_unix_ms - int(ticket.expires_at.timestamp() * 1000)) <= 1

    payload = b"\x00payload" * 1000
    assert fake.s3.put_url(ticket, payload)[0] == 200
    entry = ticket.wait(timeout=TICKET_TIMEOUT)
    assert (entry.path, entry.size) == (f"{HOME}/inbox/data.bin", len(payload))
    assert fake.file_bytes("inbox/data.bin") == payload
    assert fake.s3.keys() == []
    status = ticket.status()
    assert isinstance(status, TransferStatus)
    assert (status.phase, status.direction, status.bytes_done) == ("done", "import", len(payload))


def test_a_ticket_never_shows_or_pickles_its_url(sandbox: Sandbox) -> None:
    ticket = sandbox.files.upload_url("a.bin")
    assert "X-Amz-Signature" not in repr(ticket)
    assert "X-Amz-Signature" in str(ticket)
    with pytest.raises(TypeError):
        pickle.dumps(ticket)
    ticket.cancel()


def test_an_expired_ticket_fails_with_timeout(sandbox: Sandbox) -> None:
    ticket = sandbox.files.upload_url("late.bin", expires_in=1)
    started = time.monotonic()
    with pytest.raises(TimeoutException) as raised:
        ticket.wait(timeout=TICKET_TIMEOUT)
    assert str(raised.value).startswith("expired")
    assert time.monotonic() - started < TICKET_TIMEOUT


def test_a_put_above_max_bytes_is_too_large_and_cleaned_up(
    sandbox: Sandbox, fake: FakeTransferFilesystemService
) -> None:
    ticket = sandbox.files.upload_url("capped.bin", max_bytes=4)
    assert fake.import_requests[-1].max_bytes == 4
    fake.s3.put_url(ticket, b"0123456789")
    with pytest.raises(InvalidArgumentException) as raised:
        ticket.wait(timeout=TICKET_TIMEOUT)
    assert str(raised.value).startswith("too_large")
    assert fake.node_at("capped.bin") is None
    assert fake.s3.keys() == []


def test_max_bytes_and_expires_in_are_validated_client_side(
    sandbox: Sandbox, fake: FakeTransferFilesystemService
) -> None:
    for kwargs in ({"max_bytes": 0}, {"max_bytes": True}, {"expires_in": 0}, {"expires_in": -5}):
        with pytest.raises(InvalidArgumentException):
            sandbox.files.upload_url("a.bin", **kwargs)
    assert fake.import_requests == []


def test_wait_with_a_timeout_leaves_the_ticket_armed(
    sandbox: Sandbox, fake: FakeTransferFilesystemService
) -> None:
    ticket = sandbox.files.upload_url("slow.bin")
    with pytest.raises(TimeoutException):
        ticket.wait(timeout=0.3)
    assert ticket.status().phase == "waiting"
    fake.s3.put_url(ticket, b"late")
    assert ticket.wait(timeout=TICKET_TIMEOUT).size == 4


def test_wait_reopens_the_watch_after_a_suspend(
    sandbox: Sandbox,
    fake: FakeTransferFilesystemService,
    transfer_rayd: RaydEndpoint,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    always_running(monkeypatch, sandbox, transfer_rayd.host)
    ticket = sandbox.files.upload_url("paused.bin")
    outcome: dict[str, Any] = {}

    def wait() -> None:
        try:
            outcome["entry"] = ticket.wait(timeout=10)
        except Exception as exc:
            outcome["error"] = exc

    waiter = threading.Thread(target=wait)
    waiter.start()
    wait_until(lambda: "WatchTransfer" in fake.rpc_names())
    transfer_rayd.suspend()
    fake.s3.put_url(ticket, b"put while suspended")
    time.sleep(0.2)
    assert fake.node_at("paused.bin") is None
    transfer_rayd.resume()
    waiter.join(10)
    assert "error" not in outcome, outcome.get("error")
    assert outcome["entry"].size == len(b"put while suspended")
    assert fake.watch_cuts >= 1
    assert fake.rpc_names().count("WatchTransfer") >= 2


def test_cancel_ends_the_ticket_as_a_file_upload_failure(sandbox: Sandbox) -> None:
    ticket = sandbox.files.upload_url("never.bin")
    assert ticket.status().phase == "waiting"
    ticket.cancel()
    ticket.cancel()
    assert ticket.status().phase == "cancelled"
    with pytest.raises(FileUploadException) as raised:
        ticket.wait(timeout=TICKET_TIMEOUT)
    assert (raised.value.code, raised.value.reason) == ("cancelled", "cancelled")


def test_form_tickets_enforce_max_bytes_in_s3(
    sandbox: Sandbox, fake: FakeTransferFilesystemService
) -> None:
    ticket = sandbox.files.upload_url("form.bin", form=True, max_bytes=1024)
    assert (ticket.method, ticket.headers) == ("POST", {})
    assert {"key", "policy", "x-amz-algorithm", "x-amz-signature"} <= set(ticket.fields)
    assert url_host(ticket) == REGIONAL_HOST
    assert fake.s3.post_form(ticket, ticket.fields, b"x" * 2048) == 400
    assert fake.s3.post_form(ticket, ticket.fields, b"y" * 512) == 204
    assert ticket.wait(timeout=TICKET_TIMEOUT).size == 512


def test_sandbox_upload_and_download_url_take_the_e2b_shape(
    sandbox: Sandbox, fake: FakeTransferFilesystemService
) -> None:
    before = datetime.now(UTC)
    ticket = sandbox.upload_url("e2b.bin", use_signature_expiration=120)
    assert isinstance(ticket, UploadTicket)
    assert ticket.expires_at <= before + timedelta(seconds=130)
    fake.s3.put_url(ticket, b"hola")
    ticket.wait(timeout=TICKET_TIMEOUT)
    link = sandbox.download_url("e2b.bin")
    assert isinstance(link, DownloadLink)
    assert link.expires_at >= before + timedelta(seconds=3590)
    for method in (sandbox.upload_url, sandbox.download_url):
        with pytest.raises(InvalidArgumentException, match="use_signature_expiration"):
            method("e2b.bin", use_signature_expiration=-1)


# --------------------------------------------------------------- download_url


def test_download_url_of_a_missing_file_raises_before_any_presign(
    sandbox: Sandbox, fake: FakeTransferFilesystemService
) -> None:
    with pytest.raises(FileNotFoundException):
        sandbox.files.download_url("nope.bin")
    fake.add_dir("folder")
    with pytest.raises(InvalidArgumentException):
        sandbox.files.download_url("folder")
    assert fake.export_requests == []
    assert fake.s3.calls == []


def test_download_url_exports_a_snapshot_with_one_put(
    sandbox: Sandbox, fake: FakeTransferFilesystemService
) -> None:
    data = b"report\n" * 100
    fake.add_file("out/report.txt", data)
    link = sandbox.files.download_url("out/report.txt", filename="informe.txt", expires_in=600)
    assert isinstance(link, DownloadLink) and isinstance(link, str)
    assert (link.size, link.sha256, link.path) == (len(data), sha256(data), "out/report.txt")
    [request] = fake.export_requests
    assert request.WhichOneof("target") == "put"
    assert request.object.key.split("/")[2] == "down"
    fake.add_file("out/report.txt", b"changed afterwards")
    status, served = fake.s3.get_url(link)
    assert (status, served) == (200, data)
    assert url_query(link)["response-content-disposition"].startswith(
        'attachment; filename="informe.txt"'
    )
    assert "X-Amz-Signature" not in repr(link)


def test_download_url_uses_multipart_from_the_threshold_and_completes_it(
    sandbox: Sandbox, fake: FakeTransferFilesystemService
) -> None:
    data = bytes(range(256)) * (16 * MIB // 256) + b"tail"
    fake.add_file("big.bin", data)
    link = sandbox.files.download_url("big.bin")
    [request] = fake.export_requests
    assert request.WhichOneof("target") == "multipart"
    assert request.multipart.part_size == 8 * MIB
    assert len(request.multipart.parts) == 3
    numbers = [url_query(part.url)["partNumber"] for part in request.multipart.parts]
    assert numbers == ["1", "2", "3"]
    assert len(fake.s3.completed_uploads) == 1 and fake.s3.aborted_uploads == []
    assert fake.s3.get_url(link) == (200, data)
    assert link.sha256 == sha256(data)


def test_a_failed_multipart_export_is_aborted(
    sandbox: Sandbox, fake: FakeTransferFilesystemService
) -> None:
    fake.add_file("big.bin", b"z" * (16 * MIB))
    fake.export_failure = ("unavailable", "s3_unavailable")
    with pytest.raises(TransferException) as raised:
        sandbox.files.download_url("big.bin")
    assert (raised.value.code, raised.value.reason) == ("unavailable", "s3_unavailable")
    assert not isinstance(raised.value, FileUploadException)
    assert len(fake.s3.aborted_uploads) == 1 and fake.s3.completed_uploads == []


# ----------------------------------------------------------------- routing


def test_a_write_at_the_threshold_goes_through_s3_with_its_sha256(
    sandbox: Sandbox, fake: FakeTransferFilesystemService
) -> None:
    data = bytes(range(256)) * (MIB // 256)
    entry = sandbox.files.write("big.bin", data, mode=0o600, metadata={"Owner": "alice"})
    assert (entry.size, entry.mode) == (MIB, 0o600)
    assert dict(entry.metadata) == {"owner": "alice"}
    [request] = fake.import_requests
    assert request.wait_for_object is False
    assert (request.expected_sha256, request.max_bytes) == (sha256(data), MIB)
    assert dict(request.metadata) == {"owner": "alice"}
    assert fake.file_bytes("big.bin") == data
    assert fake.write_streams == []
    assert ("SDK_PUT", request.object.key) in fake.s3.calls
    assert fake.s3.content_types[request.object.key] == "application/octet-stream"
    assert fake.s3.keys() == []


def test_a_write_below_the_threshold_stays_on_grpc(
    sandbox: Sandbox, fake: FakeTransferFilesystemService
) -> None:
    sandbox.files.write("small.bin", b"s" * (MIB - 1))
    assert fake.import_requests == []
    assert len(fake.write_streams) == 1


def test_write_files_keeps_request_order_across_both_paths(
    sandbox: Sandbox, fake: FakeTransferFilesystemService
) -> None:
    entries = sandbox.files.write_files(
        [
            WriteEntry("a.txt", "a"),
            WriteEntry("b.bin", b"b" * MIB),
            WriteEntry("c.bin", non_seekable(b"unknown size")),
            WriteEntry("d.txt", "d"),
        ]
    )
    assert [entry.name for entry in entries] == ["a.txt", "b.bin", "c.bin", "d.txt"]
    assert len(fake.write_streams) == 1
    assert [request.max_bytes for request in fake.import_requests] == [MIB, len(b"unknown size")]
    assert fake.file_bytes("c.bin") == b"unknown size"


def test_a_checksum_mismatch_on_a_large_write_fails_the_import(
    sandbox: Sandbox, fake: FakeTransferFilesystemService
) -> None:
    fake.import_failure = ("failed_precondition", "checksum_mismatch")
    with pytest.raises(FileUploadException) as raised:
        sandbox.files.write("big.bin", b"q" * MIB)
    assert raised.value.reason == "checksum_mismatch"
    assert fake.s3.keys() == []


def test_a_large_read_exports_downloads_and_verifies(
    sandbox: Sandbox, fake: FakeTransferFilesystemService
) -> None:
    data = ("línea\n" * (MIB // 6 + 1)).encode()
    fake.add_file("big.txt", data)
    assert sandbox.files.read("big.txt", format="bytes") == data
    assert sandbox.files.read("big.txt") == data.decode()
    chunks = list(sandbox.files.read("big.txt", format="stream"))
    assert b"".join(chunks) == data and len(chunks) > 1
    assert len(fake.export_requests) == 3
    assert fake.read_calls == 0
    assert fake.s3.keys() == []


def test_a_large_read_with_a_tampered_object_is_a_checksum_mismatch(
    sandbox: Sandbox, fake: FakeTransferFilesystemService, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake.add_file("big.bin", b"r" * MIB)
    original = fake.s3.put_url
    monkeypatch.setattr(fake.s3, "put_url", lambda url, data: original(url, data + b"!"))
    with pytest.raises(TransferException) as raised:
        sandbox.files.read("big.bin", format="bytes")
    assert (raised.value.code, raised.value.reason) == ("failed_precondition", "checksum_mismatch")


def test_without_staging_nothing_is_probed_or_routed(
    plain_sandbox: Sandbox, fake: FakeTransferFilesystemService
) -> None:
    spy = spy_on_files(plain_sandbox, FILES_STUB)
    data = b"p" * (2 * MIB)
    plain_sandbox.files.write("big.bin", data)
    assert plain_sandbox.files.read("big.bin", format="bytes") == data
    assert fake.rpc_names() == []
    assert {name for name, _ in spy.calls} == {"Write", "Stat", "Read"}


def test_routing_falls_back_to_grpc_on_an_older_agent(
    sandbox: Sandbox, fake: FakeTransferFilesystemService
) -> None:
    fake.unimplemented = True
    data = b"o" * MIB
    sandbox.files.write("big.bin", data)
    assert sandbox.files.read("big.bin", format="bytes") == data
    assert fake.rpc_names() == ["GetTransfer"]
    assert len(fake.write_streams) == 1 and fake.read_calls == 1


# ----------------------------------------------------- gzip, metadata, idle


def test_gzip_travels_as_grpc_compression_and_the_opt_in_header(
    sandbox: Sandbox, fake: FakeTransferFilesystemService
) -> None:
    spy = spy_on_files(sandbox, FILES_STUB)
    text = "compresible " * 1000
    sandbox.files.write("z.txt", text, gzip=True)
    assert sandbox.files.read("z.txt", gzip=True) == text
    assert sandbox.files.read("z.txt") == text
    assert [kwargs.get("compression") for kwargs in spy.kwargs_of("Write")] == [
        grpc.Compression.Gzip
    ]
    assert fake.metadata["Read"][0].get("rayito-compress") == "gzip"
    assert "rayito-compress" not in fake.metadata["Read"][1]


def test_gzip_writes_need_an_m9_agent_but_gzip_reads_do_not(
    sandbox: Sandbox, fake: FakeTransferFilesystemService
) -> None:
    fake.add_file("plain.txt", b"hola")
    fake.unimplemented = True
    with pytest.raises(UnimplementedError, match="gzip"):
        sandbox.files.write("z.txt", "x", gzip=True)
    assert sandbox.files.read("plain.txt", gzip=True) == "hola"
    assert fake.write_streams == []


def test_metadata_round_trips_through_write_get_info_and_list(
    sandbox: Sandbox, fake: FakeTransferFilesystemService
) -> None:
    written = sandbox.files.write("tagged.txt", "x", metadata={"Owner": "alice", "Team": "core"})
    assert dict(written.metadata) == {"owner": "alice", "team": "core"}
    assert dict(sandbox.files.get_info("tagged.txt").metadata) == {"owner": "alice", "team": "core"}
    [listed] = [entry for entry in sandbox.files.list(HOME) if entry.name == "tagged.txt"]
    assert dict(listed.metadata) == {"owner": "alice", "team": "core"}
    with pytest.raises(TypeError):
        listed.metadata["owner"] = "mallory"  # type: ignore[index]
    sandbox.files.write("tagged.txt", "y")
    assert dict(sandbox.files.get_info("tagged.txt").metadata) == {}


def test_invalid_metadata_never_reaches_the_agent(
    sandbox: Sandbox, fake: FakeTransferFilesystemService
) -> None:
    with pytest.raises(InvalidArgumentException, match="metadatos inválidos"):
        sandbox.files.write("x.txt", "x", metadata={"a b": "c"})
    assert fake.write_streams == [] and fake.rpc_names() == []


def test_use_octet_stream_is_accepted_and_changes_nothing(
    sandbox: Sandbox, fake: FakeTransferFilesystemService
) -> None:
    entry = sandbox.files.write("o.txt", "x", use_octet_stream=True)
    assert entry.size == 1
    assert fake.rpc_names() == []


class StalledRead:
    """Un `Read` que entrega un chunk y después se queda callado hasta que
    lo cancelan."""

    def __init__(self) -> None:
        self.cancelled = threading.Event()

    def __iter__(self) -> StalledRead:
        return self

    def __next__(self) -> Any:
        self.cancelled.wait(10)
        raise grpc.RpcError()

    def cancel(self) -> bool:
        self.cancelled.set()
        return True


def test_stream_idle_timeout_cancels_a_stalled_read(
    sandbox: Sandbox, fake: FakeTransferFilesystemService, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake.add_file("slow.txt", b"abc")
    stalled = StalledRead()
    first = filesystem_pb2.ReadResponse(chunk=b"a")
    monkeypatch.setattr(sandbox, "_open_stream", lambda *args, **kwargs: (stalled, first))
    started = time.monotonic()
    with pytest.raises(TimeoutException, match="stream_idle_timeout"):
        sandbox.files.read("slow.txt", stream_idle_timeout=0.2)
    assert stalled.cancelled.is_set()
    assert time.monotonic() - started < 5
    with pytest.raises(InvalidArgumentException):
        sandbox.files.read("slow.txt", stream_idle_timeout=-1)


class StalledS3:
    """Una pata S3 que se queda colgada: la subida no vuelve y el cuerpo de
    `get_object` no entrega el siguiente trozo hasta que lo cierran."""

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


def test_request_timeout_bounds_a_stalled_s3_upload(
    sandbox: Sandbox, fake: FakeTransferFilesystemService, stalled_s3: StalledS3
) -> None:
    started = time.monotonic()
    with pytest.raises(TimeoutException, match="plazo"):
        sandbox.files.write("big.bin", b"u" * MIB, request_timeout=0.5)
    assert time.monotonic() - started < 5
    assert fake.import_requests == []


def test_request_timeout_bounds_a_stalled_s3_download(
    sandbox: Sandbox, fake: FakeTransferFilesystemService, stalled_s3: StalledS3
) -> None:
    fake.add_file("big.bin", b"d" * MIB)
    started = time.monotonic()
    with pytest.raises(TimeoutException, match="plazo"):
        sandbox.files.read("big.bin", format="bytes", request_timeout=1.0)
    assert time.monotonic() - started < 5
    assert stalled_s3.body_closed.wait(5)
    assert fake.s3.keys() == []


def test_stream_idle_timeout_bounds_a_stalled_s3_download(
    sandbox: Sandbox, fake: FakeTransferFilesystemService, stalled_s3: StalledS3
) -> None:
    fake.add_file("big.txt", b"t" * MIB)
    started = time.monotonic()
    with pytest.raises(TimeoutException, match="stream_idle_timeout"):
        sandbox.files.read("big.txt", stream_idle_timeout=0.2)
    assert time.monotonic() - started < 5
    assert stalled_s3.body_closed.wait(5)


# --------------------------------------------------------------- config & logs


def test_transfer_comes_from_the_environment_when_not_given(
    control_plane: StubbedControlPlane,
    transfer_rayd: RaydEndpoint,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RAYITO_TRANSFER_BUCKET", BUCKET)
    monkeypatch.setenv("RAYITO_TRANSFER_PREFIX", "env/prefix")
    created = launch(control_plane, transfer_rayd, transfer=None)
    try:
        assert created.transfer == S3Staging(bucket=BUCKET, prefix="env/prefix")
    finally:
        stub_terminate(control_plane)
        created.kill()


def test_overlapping_transfer_and_persist_fail_before_any_aws_call(
    control_plane: StubbedControlPlane, transfer_rayd: RaydEndpoint
) -> None:
    with pytest.raises(InvalidArgumentException, match="disjuntos"):
        Sandbox.create(
            IMAGE_ARN,
            execution_role_arn="arn:aws:iam::123456789012:role/rayito-execution",
            persist=S3Prefix(bucket=BUCKET, prefix="homes"),
            transfer=S3Staging(bucket=BUCKET, prefix="homes/tmp"),
            access_token=ACCESS_TOKEN,
            control_plane=control_plane.plane,
            transport=transfer_rayd.transport,
        )
    with pytest.raises(InvalidArgumentException, match="disjuntos"):
        Sandbox.connect(
            SANDBOX_ID,
            persist=S3Prefix(bucket=BUCKET, prefix="homes", name="alice"),
            transfer=S3Staging(bucket=BUCKET, prefix="homes"),
            access_token=ACCESS_TOKEN,
            control_plane=control_plane.plane,
            transport=transfer_rayd.transport,
        )


def test_logs_never_carry_urls_buckets_keys_or_paths(
    sandbox: Sandbox, fake: FakeTransferFilesystemService, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="rayito")
    ticket = sandbox.files.upload_url("secret/dir/in.bin")
    fake.s3.put_url(ticket, b"x")
    ticket.wait(timeout=TICKET_TIMEOUT)
    sandbox.files.download_url("secret/dir/in.bin")
    sandbox.files.write("secret/dir/big.bin", b"b" * MIB)
    sandbox.files.read("secret/dir/big.bin", format="bytes")
    fake.add_file("secret/dir/huge.bin", b"h" * (16 * MIB))
    fake.export_failure = ("unavailable", "s3_unavailable")
    with pytest.raises(TransferException):
        sandbox.files.download_url("secret/dir/huge.bin")
    own = "\n".join(
        record.getMessage() for record in caplog.records if record.name.startswith("rayito")
    )
    assert "transferencia" in own
    for forbidden in ("X-Amz-Signature", "X-Amz-Credential", BUCKET, "rayito-transfer/", "secret"):
        assert forbidden not in own


def test_a_plane_built_from_a_session_signs_with_that_session(
    control_plane: StubbedControlPlane, transfer_rayd: RaydEndpoint
) -> None:
    signing = transfer_files(transfer_rayd).s3.session()
    plane = LambdaMicrovmsControlPlane(
        control_plane.microvms.client, sts_client=control_plane.sts.client, session=signing
    )
    stub_launch(control_plane, transfer_rayd)
    sandbox = Sandbox.create(
        IMAGE_ARN,
        idle=None,
        access_token=ACCESS_TOKEN,
        control_plane=plane,
        transport=transfer_rayd.transport,
        transfer=STAGING,
    )
    try:
        assert sandbox._session is signing
        ticket = sandbox.files.upload_url("a.bin")
        assert url_query(ticket.url)["X-Amz-Credential"].startswith(f"{FAKE_ACCESS_KEY}/")
    finally:
        stub_terminate(control_plane)
        sandbox.kill()
