"""Núcleo puro de las transferencias (`rayito._transfer_base`, `S3Staging`,
la validación de metadatos y el adaptador de firmas `rayito._s3`): casos de
`openspec/changes/m9-file-transfer/design.md` D23 que no necesitan un
`rayd`."""

from __future__ import annotations

import hashlib
import io
import pickle
import re
import time
import urllib.parse
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any, cast

import boto3
import grpc
import pytest

from rayito._filesystem_base import (
    guarded_messages,
    read_call_options,
    validate_metadata,
    validate_stream_idle_timeout,
    write_call_options,
)
from rayito._limits import (
    TRANSFER_DEFAULT_MULTIPART_THRESHOLD_BYTES,
    TRANSFER_PART_SIZE_MIN_BYTES,
    TRANSFER_PRESIGN_MAX_SECONDS,
)
from rayito._models import (
    DownloadLink,
    S3Prefix,
    S3Staging,
    UploadTicket,
    WriteEntry,
)
from rayito._s3 import S3Gateway
from rayito._transfer_base import (
    MIB,
    HashingReader,
    OperationDeadline,
    StagingObject,
    content_disposition,
    delete_lifetime,
    effective_expires_in,
    expires_in_from_signature_expiration,
    export_budget_seconds,
    failure_from_state,
    internal_get_lifetime,
    new_staging_object,
    part_plan,
    plan_writes,
    post_conditions,
    presign_client_config,
    probe_supports_transfers,
    should_route,
    staging_key,
    state_from_proto,
    translate_transfer_error,
    upload_ticket_headers,
    validate_staging_against_persist,
)
from rayito._transport import translate_stream_error
from rayito.exceptions import (
    AuthenticationException,
    DiskFullException,
    FileNotFoundException,
    FileUploadException,
    InvalidArgumentException,
    NotFoundException,
    RateLimitException,
    SandboxException,
    TimeoutException,
    TransferException,
    UnimplementedError,
)
from rayito.v1 import common_pb2, filesystem_pb2

from .conftest import SANDBOX_ID, FakeRpcError
from .fake_s3 import BUCKET, FAKE_ACCESS_KEY, FAKE_SECRET_KEY, REGION

REGIONAL_HOST = f"{BUCKET}.s3.{REGION}.amazonaws.com"
TOKEN_PATTERN = re.compile(r"[0-9a-f]{32}")
TARGET = StagingObject(
    bucket=BUCKET, key=f"rayito-transfer/{SANDBOX_ID}/up/{'a' * 32}", region=REGION
)


def gateway() -> S3Gateway:
    session = boto3.session.Session(
        region_name=REGION, aws_access_key_id=FAKE_ACCESS_KEY, aws_secret_access_key=FAKE_SECRET_KEY
    )
    return S3Gateway.from_session(session, REGION)


def query_of(url: str) -> dict[str, str]:
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))


def failed_state(
    code: str | None, message: str, phase: filesystem_pb2.TransferPhase
) -> filesystem_pb2.TransferState:
    state = filesystem_pb2.TransferState(transfer_id="t" * 32, phase=phase)
    if code is not None:
        state.error.CopyFrom(common_pb2.StreamError(code=code, message=message))
    return state


# ------------------------------------------------------------------ presign


def test_presign_config_signs_sigv4_on_the_regional_virtual_host_in_us_east_1() -> None:
    urls = [
        gateway().presign_put(TARGET, 60),
        gateway().presign_get(TARGET, 60),
        gateway().presign_delete(TARGET, 60),
        gateway().presign_download(TARGET, 60, content_disposition("a.txt")),
        *gateway().presign_upload_parts(TARGET, "upload-1", 2, 60),
    ]
    for url in urls:
        parts = urllib.parse.urlsplit(url)
        assert parts.scheme == "https"
        assert parts.hostname == REGIONAL_HOST
        assert urllib.parse.unquote(parts.path) == "/" + TARGET.key
        query = query_of(url)
        assert query["X-Amz-Algorithm"] == "AWS4-HMAC-SHA256"
        assert "Signature" not in query and "AWSAccessKeyId" not in query
        assert query["X-Amz-SignedHeaders"] == "host"


def test_botocore_default_config_would_sign_sigv2_on_the_global_host() -> None:
    """El caso que `presign_client_config` corrige (AWS_API_NOTES.md Q62):
    si esta aserción deja de cumplirse, botocore cambió su valor por defecto."""
    session = boto3.session.Session(
        region_name=REGION, aws_access_key_id=FAKE_ACCESS_KEY, aws_secret_access_key=FAKE_SECRET_KEY
    )
    default = session.client("s3").generate_presigned_url(
        "put_object", Params={"Bucket": BUCKET, "Key": TARGET.key}, ExpiresIn=60
    )
    assert urllib.parse.urlsplit(default).hostname != REGIONAL_HOST
    assert presign_client_config().signature_version == "s3v4"


def test_upload_parts_carry_their_one_based_number_and_one_upload_id() -> None:
    urls = gateway().presign_upload_parts(TARGET, "upload-7", 3, 60)
    assert [query_of(url)["partNumber"] for url in urls] == ["1", "2", "3"]
    assert {query_of(url)["uploadId"] for url in urls} == {"upload-7"}


def test_presigned_post_is_sigv4_with_the_content_length_range() -> None:
    url, fields = gateway().presign_post(TARGET, 1024, 60)
    assert urllib.parse.urlsplit(url).hostname == REGIONAL_HOST
    assert fields["x-amz-algorithm"] == "AWS4-HMAC-SHA256"
    assert fields["key"] == TARGET.key
    assert post_conditions(1024) == [["content-length-range", 0, 1024]]
    assert post_conditions(None) == [["content-length-range", 0, 5_368_709_120]]


def test_download_url_carries_the_content_disposition() -> None:
    url = gateway().presign_download(TARGET, 60, content_disposition("informe final.pdf"))
    assert query_of(url)["response-content-disposition"] == (
        "attachment; filename=\"informe final.pdf\"; filename*=UTF-8''informe%20final.pdf"
    )


@pytest.mark.parametrize(
    ("requested", "maximum", "effective"),
    [
        (604_801, 604_800, 604_800),
        (3600, 86_400, 3600),
        (100_000, 86_400, 86_400),
        (1, 1, 1),
    ],
)
def test_expires_in_is_clamped_to_the_staging_maximum_and_seven_days(
    requested: int, maximum: int, effective: int
) -> None:
    staging = S3Staging(bucket=BUCKET, max_expires_in=maximum)
    assert effective_expires_in(requested, staging) == effective


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "60"])
def test_expires_in_must_be_a_positive_integer(value: object) -> None:
    with pytest.raises(InvalidArgumentException):
        effective_expires_in(value, S3Staging(bucket=BUCKET))


def test_signature_expiration_maps_like_e2b() -> None:
    assert expires_in_from_signature_expiration(None) == 3600
    assert expires_in_from_signature_expiration(120) == 120
    for value in (0, -1):
        with pytest.raises(InvalidArgumentException, match="use_signature_expiration"):
            expires_in_from_signature_expiration(value)


def test_internal_lifetimes_are_bounded_by_seven_days() -> None:
    assert delete_lifetime(3600) == 7200
    assert delete_lifetime(TRANSFER_PRESIGN_MAX_SECONDS) == TRANSFER_PRESIGN_MAX_SECONDS
    assert export_budget_seconds(0) == 900
    assert export_budget_seconds(1_000_001) == 902
    assert internal_get_lifetime(S3Staging(bucket=BUCKET)) == 900
    assert internal_get_lifetime(S3Staging(bucket=BUCKET, max_expires_in=60)) == 60


# ------------------------------------------------------------------- staging


def test_staging_keys_never_carry_the_user_path() -> None:
    target = new_staging_object(S3Staging(bucket=BUCKET), SANDBOX_ID, REGION, "down")
    prefix, sandbox, direction, token = target.key.split("/")
    assert (prefix, sandbox, direction) == ("rayito-transfer", SANDBOX_ID, "down")
    assert TOKEN_PATTERN.fullmatch(token)
    assert staging_key("a/b", "microvm-1", "up", "f" * 32) == f"a/b/microvm-1/up/{'f' * 32}"
    assert target.region == REGION
    assert new_staging_object(
        S3Staging(bucket=BUCKET, region="eu-west-1"), "s", REGION, "up"
    ).region == ("eu-west-1")


@pytest.mark.parametrize(
    "bucket",
    [
        "my.dotted.bucket",
        "UPPER",
        "ab",
        "-lead",
        "trail-",
        "xn--bucket",
        "bucket-s3alias",
        "a" * 64,
    ],
)
def test_staging_bucket_must_be_dns_compatible_without_dots(bucket: str) -> None:
    with pytest.raises(InvalidArgumentException) as raised:
        S3Staging(bucket=bucket)
    assert bucket not in str(raised.value)


@pytest.mark.parametrize(
    "prefix",
    ["", "/lead", "trail/", "a//b", "a/../b", ".", "rayito", "rayito/sub", "sp ace", "x" * 257],
)
def test_staging_prefix_rules_and_artifact_namespace(prefix: str) -> None:
    with pytest.raises(InvalidArgumentException):
        S3Staging(bucket=BUCKET, prefix=prefix)


def test_staging_accepts_nested_prefixes_disjoint_from_rayito() -> None:
    assert S3Staging(bucket=BUCKET, prefix="rayito-transfer").prefix == "rayito-transfer"
    assert S3Staging(bucket=BUCKET, prefix="team/rayito").prefix == "team/rayito"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("region", "us_east_1"),
        ("max_expires_in", 0),
        ("max_expires_in", 604_801),
        ("threshold_bytes", MIB - 1),
        ("threshold_bytes", 5_368_709_121),
        ("multipart_threshold_bytes", 16 * MIB - 1),
        ("multipart_threshold_bytes", True),
    ],
)
def test_staging_numeric_and_region_bounds(field: str, value: object) -> None:
    with pytest.raises(InvalidArgumentException, match=field):
        S3Staging(bucket=BUCKET, **cast("dict[str, Any]", {field: value}))


def test_staging_from_env_reads_the_three_variables_and_nothing_else() -> None:
    assert S3Staging.from_env({}) is None
    assert S3Staging.from_env({"RAYITO_TRANSFER_BUCKET": ""}) is None
    staging = S3Staging.from_env(
        {
            "RAYITO_TRANSFER_BUCKET": BUCKET,
            "RAYITO_TRANSFER_PREFIX": "uploads/tmp",
            "RAYITO_TRANSFER_REGION": "eu-west-1",
        }
    )
    assert staging == S3Staging(bucket=BUCKET, prefix="uploads/tmp", region="eu-west-1")
    defaults = S3Staging.from_env({"RAYITO_TRANSFER_BUCKET": BUCKET})
    assert defaults == S3Staging(bucket=BUCKET)


def test_staging_and_persist_prefixes_must_be_disjoint_in_the_same_bucket() -> None:
    persist = S3Prefix(bucket=BUCKET, prefix="homes")
    with pytest.raises(InvalidArgumentException, match="disjuntos"):
        validate_staging_against_persist(S3Staging(bucket=BUCKET, prefix="homes/tmp"), persist)
    with pytest.raises(InvalidArgumentException):
        validate_staging_against_persist(S3Staging(bucket=BUCKET, prefix="homes"), persist)
    validate_staging_against_persist(S3Staging(bucket=BUCKET, prefix="homes-tmp"), persist)
    validate_staging_against_persist(S3Staging(bucket="other-bucket", prefix="homes"), persist)
    validate_staging_against_persist(None, persist)
    validate_staging_against_persist(S3Staging(bucket=BUCKET), None)


# ------------------------------------------------------------------ multipart


def test_part_plan_starts_at_the_multipart_threshold() -> None:
    staging = S3Staging(bucket=BUCKET, multipart_threshold_bytes=64 * MIB)
    assert part_plan(64 * MIB - 1, staging) is None
    plan = part_plan(64 * MIB, staging)
    assert plan is not None
    assert (plan.part_size, plan.part_count) == (TRANSFER_PART_SIZE_MIN_BYTES, 8)
    odd = part_plan(100_000_000, staging)
    assert odd is not None and (odd.part_size, odd.part_count) == (8 * MIB, 12)


def test_part_plan_never_needs_more_than_a_thousand_urls() -> None:
    staging = S3Staging(bucket=BUCKET)
    assert part_plan(TRANSFER_DEFAULT_MULTIPART_THRESHOLD_BYTES - 1, staging) is None
    huge = 32 * 1024 * MIB
    plan = part_plan(huge, staging)
    assert plan is not None
    assert plan.part_count <= 1000
    assert plan.part_size % MIB == 0
    assert plan.part_size * plan.part_count >= huge


def test_content_disposition_escapes_quotes_backslashes_and_non_ascii() -> None:
    assert content_disposition('a"b\\c.txt') == (
        "attachment; filename=\"a_b_c.txt\"; filename*=UTF-8''a%22b%5Cc.txt"
    )
    assert content_disposition("año.csv") == (
        "attachment; filename=\"a__o.csv\"; filename*=UTF-8''a%C3%B1o.csv"
    )


# ------------------------------------------------------------------ routing


def test_routing_predicate_is_inclusive_at_the_threshold() -> None:
    staging = S3Staging(bucket=BUCKET, threshold_bytes=MIB)
    assert should_route(MIB, staging)
    assert not should_route(MIB - 1, staging)
    assert not should_route(10 * MIB, None)


class NonSeekableRaw(io.RawIOBase):
    def __init__(self, data: bytes) -> None:
        self._inner = io.BytesIO(data)

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False

    def readinto(self, buffer: Any) -> int:
        chunk = self._inner.read(len(buffer))
        buffer[: len(chunk)] = chunk
        return len(chunk)


def test_plan_writes_routes_large_and_unsized_entries_only_with_staging() -> None:
    staging = S3Staging(bucket=BUCKET, threshold_bytes=MIB)
    big = b"x" * MIB
    entries = [
        WriteEntry("small.txt", "hola"),
        WriteEntry("big.bin", big),
        WriteEntry("pipe.bin", io.BufferedReader(NonSeekableRaw(b"stream"))),
        WriteEntry("seek.bin", io.BytesIO(b"y" * (MIB - 1))),
    ]
    plan = plan_writes(entries, staging)
    assert [index for index, _ in plan.grpc] == [0, 3]
    assert [(routed.index, routed.size) for routed in plan.routed] == [(1, MIB), (2, None)]
    plain = plan_writes([WriteEntry("big.bin", big)], None)
    assert plain.routed == []
    assert plain.grpc_entries == [("big.bin", big, None)]


def test_plan_without_routing_keeps_request_order() -> None:
    staging = S3Staging(bucket=BUCKET, threshold_bytes=MIB)
    plan = plan_writes([WriteEntry("a", b"z" * MIB), WriteEntry("b", "b")], staging)
    demoted = plan.without_routing()
    assert demoted.routed == []
    assert [index for index, _ in demoted.grpc] == [0, 1]
    assert demoted.grpc_entries[0][1] == b"z" * MIB


def test_hashing_reader_hashes_in_order_without_exposing_seek() -> None:
    reader = HashingReader(io.StringIO("héllo"))
    assert reader.read(2) + reader.read() == "héllo".encode()
    assert reader.size == len("héllo".encode())
    assert reader.sha256 == hashlib.sha256("héllo".encode()).hexdigest()
    assert not hasattr(reader, "seek")


def test_an_aborted_hashing_reader_fails_its_next_read() -> None:
    reader = HashingReader(b"abcdef")
    assert reader.read(2) == b"ab"
    reader.abort()
    with pytest.raises(TimeoutException, match="plazo"):
        reader.read(2)
    assert reader.size == 2


def test_the_s3_leg_budget_follows_the_operation_deadline() -> None:
    assert OperationDeadline(100.0, None).budget(None, 100.0) is None
    assert OperationDeadline(100.0, 5.0).budget(None, 101.0) == 4.0
    assert OperationDeadline(100.0, None).budget(MIB, 100.0) == pytest.approx(61.048576)
    with pytest.raises(TimeoutException, match="plazo"):
        OperationDeadline(100.0, 1.0).budget(MIB, 102.0)


# ------------------------------------------------------------------- states


@pytest.mark.parametrize(
    ("code", "import_type", "export_type"),
    [
        ("deadline_exceeded", TimeoutException, TimeoutException),
        ("invalid_argument", InvalidArgumentException, InvalidArgumentException),
        ("resource_exhausted", DiskFullException, DiskFullException),
        ("permission_denied", AuthenticationException, AuthenticationException),
        ("not_found", FileNotFoundException, FileNotFoundException),
        ("failed_precondition", FileUploadException, TransferException),
        ("unavailable", FileUploadException, TransferException),
        ("cancelled", FileUploadException, TransferException),
        ("internal", FileUploadException, TransferException),
        ("brand_new_code", FileUploadException, TransferException),
    ],
)
def test_terminal_states_map_per_d13(
    code: str, import_type: type[Exception], export_type: type[Exception]
) -> None:
    state = failed_state(code, "too_large: frase fija", filesystem_pb2.TRANSFER_PHASE_FAILED)
    imported = failure_from_state(state, "import")
    exported = failure_from_state(state, "export")
    assert type(imported) is import_type
    assert type(exported) is export_type
    assert str(imported).startswith("too_large")
    if isinstance(imported, TransferException):
        assert (imported.code, imported.reason) == (code, "too_large")
        assert type(exported) is TransferException
    if isinstance(imported, AuthenticationException):
        assert imported.proxy_rejected is False


def test_a_cancelled_state_without_error_is_cancelled() -> None:
    state = failed_state(None, "", filesystem_pb2.TRANSFER_PHASE_CANCELLED)
    failure = failure_from_state(state, "import")
    assert isinstance(failure, FileUploadException)
    assert (failure.code, failure.reason) == ("cancelled", "cancelled")


def test_state_from_proto_names_phase_direction_and_error() -> None:
    state = failed_state("unavailable", "s3_unavailable: x", filesystem_pb2.TRANSFER_PHASE_FAILED)
    state.direction = filesystem_pb2.TRANSFER_DIRECTION_EXPORT
    state.bytes_done, state.bytes_total, state.probes = 3, 9, 4
    status = state_from_proto(state)
    assert (status.phase, status.direction) == ("failed", "export")
    assert (status.bytes_done, status.bytes_total, status.probes) == (3, 9, 4)
    assert (status.error_code, status.error_reason) == ("unavailable", "s3_unavailable")
    running = state_from_proto(
        filesystem_pb2.TransferState(phase=filesystem_pb2.TRANSFER_PHASE_RUNNING)
    )
    assert running.phase == "running" and running.error_code is None


def test_capability_probe_reads_not_found_as_capable_and_unimplemented_as_old() -> None:
    assert probe_supports_transfers(FakeRpcError(grpc.StatusCode.NOT_FOUND)) is True
    assert probe_supports_transfers(FakeRpcError(grpc.StatusCode.UNIMPLEMENTED)) is False
    with pytest.raises(AuthenticationException):
        probe_supports_transfers(FakeRpcError(grpc.StatusCode.UNAUTHENTICATED))


def test_unimplemented_transfer_rpc_asks_for_a_newer_image() -> None:
    error = translate_transfer_error(FakeRpcError(grpc.StatusCode.UNIMPLEMENTED), "upload_url")
    assert isinstance(error, UnimplementedError)
    assert error.feature == "upload_url"
    assert "actualiza la imagen" in str(error)
    assert isinstance(
        translate_transfer_error(FakeRpcError(grpc.StatusCode.RESOURCE_EXHAUSTED), "x"),
        RateLimitException,
    )
    assert isinstance(
        translate_transfer_error(FakeRpcError(grpc.StatusCode.NOT_FOUND), "x"), NotFoundException
    )


@pytest.mark.parametrize(
    ("code", "message", "expected"),
    [
        ("resource_exhausted", "disk_full", DiskFullException),
        ("resource_exhausted", "demasiadas transferencias", RateLimitException),
        ("failed_precondition", "file_changed", InvalidArgumentException),
        ("unavailable", "s3_unavailable", SandboxException),
        ("cancelled", "cancelled", SandboxException),
    ],
)
def test_new_stream_error_codes_are_mapped(
    code: str, message: str, expected: type[Exception]
) -> None:
    assert type(translate_stream_error(code, message)) is expected


def test_upload_ticket_headers_recommend_octet_stream_only_for_put() -> None:
    assert upload_ticket_headers(form=False) == {"Content-Type": "application/octet-stream"}
    assert upload_ticket_headers(form=True) == {}


# ------------------------------------------------------------- str subclasses


class NoOperations:
    def wait_transfer(self, transfer_id: str, timeout: float | None) -> Any:
        raise AssertionError("no")

    def transfer_status(self, transfer_id: str) -> Any:
        raise AssertionError("no")

    def cancel_transfer(self, transfer_id: str) -> None:
        raise AssertionError("no")


SECRET_URL = "https://x.example/k?X-Amz-Signature=deadbeef"
EXPIRES = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)


def test_upload_ticket_is_the_url_with_a_redacted_repr_and_no_pickle() -> None:
    ticket = UploadTicket(
        SECRET_URL,
        method="PUT",
        headers={"Content-Type": "application/octet-stream"},
        fields={},
        path="/home/user/a.bin",
        expires_at=EXPIRES,
        transfer_id="t" * 32,
        operations=NoOperations(),
    )
    assert ticket == SECRET_URL and ticket.url == SECRET_URL and str(ticket) == SECRET_URL
    assert "X-Amz-Signature" not in repr(ticket)
    assert repr(ticket).startswith("UploadTicket(path='/home/user/a.bin', method='PUT'")
    ticket.headers["x"] = "mutated"
    assert "x" not in ticket.headers
    with pytest.raises(TypeError):
        pickle.dumps(ticket)


def test_download_link_is_the_url_with_size_and_sha256() -> None:
    link = DownloadLink(
        SECRET_URL,
        path="/home/user/a.bin",
        expires_at=EXPIRES,
        transfer_id="t",
        size=3,
        sha256="ab",
    )
    assert link == SECRET_URL
    assert (link.size, link.sha256, link.expires_at) == (3, "ab", EXPIRES)
    assert "X-Amz-Signature" not in repr(link)


# ---------------------------------------------------------- metadata & gzip


def test_metadata_is_lowercased_and_bounded() -> None:
    assert validate_metadata(None) == {}
    assert validate_metadata({"Owner": "alice", "X-Team": ""}) == {"owner": "alice", "x-team": ""}
    assert validate_metadata({f"k{index}": "v" for index in range(64)})
    assert validate_metadata({"k": "v" * (4000 - len("user.rayito.") - 1)})


@pytest.mark.parametrize(
    "metadata",
    [
        {"a b": "x"},
        {"": "x"},
        {"k" * 256: "x"},
        {"ok": "tab\there"},
        {"ok": "ñ"},
        {"Owner": "a", "owner": "b"},
        {f"k{index}": "v" for index in range(65)},
        {"k": "v" * (4000 - len("user.rayito.") - 1 + 1)},
        {"k": 1},
    ],
)
def test_invalid_metadata_is_refused_without_echoing_it(metadata: dict[str, Any]) -> None:
    with pytest.raises(InvalidArgumentException, match="metadatos inválidos") as raised:
        validate_metadata(metadata)
    for key, value in metadata.items():
        if len(str(key)) > 2:
            assert str(key) not in str(raised.value)
        if isinstance(value, str) and len(value) > 2:
            assert value not in str(raised.value)


def test_gzip_options_and_idle_timeout_validation() -> None:
    assert read_call_options(True) == {"metadata": (("rayito-compress", "gzip"),)}
    assert read_call_options(False) == {}
    assert write_call_options(True) == {"compression": grpc.Compression.Gzip}
    assert write_call_options(False) == {}
    assert validate_stream_idle_timeout(None) is None
    assert validate_stream_idle_timeout(0) is None
    assert validate_stream_idle_timeout(2) == 2.0
    for value in (-1, "1", True):
        with pytest.raises(InvalidArgumentException):
            validate_stream_idle_timeout(value)


def test_guarded_messages_cancels_a_silent_stream_and_raises_timeout() -> None:
    cancelled: list[bool] = []

    class Silent:
        def __iter__(self) -> Iterator[int]:
            return self

        def __next__(self) -> int:
            while not cancelled:
                time.sleep(0.01)
            raise RuntimeError("cancelled")

    with pytest.raises(TimeoutException, match="stream_idle_timeout"):
        list(guarded_messages(Silent(), 0.1, lambda: cancelled.append(True)))
    assert cancelled == [True]
    assert list(guarded_messages(iter([1, 2]), None, lambda: None)) == [1, 2]
    assert list(guarded_messages(iter([1, 2]), 5.0, lambda: None)) == [1, 2]
