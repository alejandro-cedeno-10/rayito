"""Ninguna excepción del SDK expone credenciales de AWS.

botocore no cuelga la petición firmada de un `ClientError` (su `response`
sólo lleva `Error` y `ResponseMetadata` con las cabeceras de la *respuesta*),
pero lo que AWS devuelve sí repite la firma: un `SignatureDoesNotMatch` de S3
trae `AWSAccessKeyId` y `CanonicalRequest` (con `x-amz-security-token`) en
`response["Error"]`, y un `InvalidSignatureException` de un servicio JSON mete
la cadena canónica en el `Message`, que acaba en `str(exc)`. Con
`raise ... from exc` todo eso quedaba en `__cause__` y en cualquier traceback.
Los clientes boto3 son reales, contra un servidor HTTP local.
"""

from __future__ import annotations

import json
import threading
import traceback
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import boto3
import pytest
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError, EndpointConnectionError

from rayito._aws import LambdaMicrovmsControlPlane, translate_client_error
from rayito._aws_sanitize import AwsErrorSummary, redact_aws_text, sanitize_aws_error
from rayito._s3 import S3Gateway, s3_call
from rayito._transfer_base import StagingObject
from rayito.exceptions import AuthenticationException, SandboxException

ACCESS_KEY_ID = "ASIAFAKEKEYIDEXAMPLE"
SESSION_TOKEN = "FAKESESSIONTOKEN"
SIGNATURE_PARAM = "X-Amz-Signature"
SECRETS = (ACCESS_KEY_ID, SESSION_TOKEN, SIGNATURE_PARAM)
REQUEST_ID = "req-0123456789"
HOST_ID = "ext-id-2-abcdef"
REGION = "us-east-1"
BUCKET = "amzn-s3-demo-bucket"
PRESIGNED_URL = (
    f"https://{BUCKET}.s3.{REGION}.amazonaws.com/k?X-Amz-Algorithm=AWS4-HMAC-SHA256"
    f"&X-Amz-Credential={ACCESS_KEY_ID}%2F20260924%2F{REGION}%2Fs3%2Faws4_request"
    f"&X-Amz-Security-Token={SESSION_TOKEN}&{SIGNATURE_PARAM}={'a' * 64}"
)
CANONICAL_REQUEST = (
    "GET\n/2026-09-24/microvms/m-1\n\nhost:localhost\nx-amz-date:20260924T000000Z\n"
    f"x-amz-security-token:{SESSION_TOKEN}\n\nhost;x-amz-date;x-amz-security-token\n{'e' * 64}"
)
INVALID_SIGNATURE_MESSAGE = (
    "The request signature we calculated does not match the signature you provided. "
    "Check your AWS Secret Access Key and signing method. Consult the service documentation "
    f"for details.\n\nThe Canonical String for this request should have been\n"
    f"'{CANONICAL_REQUEST}'\n\nThe String-to-Sign should have been\n'AWS4-HMAC-SHA256\n"
    f"20260924T000000Z\n20260924/{REGION}/lambda-microvms/aws4_request\n{'b' * 64}'\n"
)
S3_SIGNATURE_MISMATCH = (
    '<?xml version="1.0" encoding="UTF-8"?><Error><Code>SignatureDoesNotMatch</Code>'
    "<Message>The request signature we calculated does not match the signature you "
    f"provided. Check your key and signing method.</Message><AWSAccessKeyId>{ACCESS_KEY_ID}"
    "</AWSAccessKeyId><StringToSign>AWS4-HMAC-SHA256</StringToSign><CanonicalRequest>"
    f"POST\n/{BUCKET}/k\nuploads=\nx-amz-security-token:{SESSION_TOKEN}</CanonicalRequest>"
    f"<SignatureProvided>{'c' * 64}</SignatureProvided><RequestId>{REQUEST_ID}</RequestId>"
    f"<HostId>{HOST_ID}</HostId></Error>"
)

STS_SIGNATURE_MISMATCH = (
    '<ErrorResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/"><Error><Type>Sender'
    "</Type><Code>SignatureDoesNotMatch</Code><Message>"
    + INVALID_SIGNATURE_MESSAGE.replace("'", "&apos;")
    + f"</Message></Error><RequestId>{REQUEST_ID}</RequestId></ErrorResponse>"
)


class _Handler(BaseHTTPRequestHandler):
    seen: list[dict[str, str]]

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _answer(self) -> None:
        length = int(self.headers.get("content-length") or 0)
        request_body = self.rfile.read(length)
        type(self).seen.append({name.lower(): value for name, value in self.headers.items()})
        if request_body.startswith(b"Action="):
            body = STS_SIGNATURE_MISMATCH.encode()
            self.send_response(403)
            self.send_header("content-type", "text/xml")
            self.send_header("x-amzn-requestid", REQUEST_ID)
        elif self.path.startswith(f"/{BUCKET}"):
            body = S3_SIGNATURE_MISMATCH.encode()
            self.send_response(403)
            self.send_header("content-type", "application/xml")
            self.send_header("x-amz-request-id", REQUEST_ID)
            self.send_header("x-amz-id-2", HOST_ID)
        else:
            body = json.dumps({"message": INVALID_SIGNATURE_MESSAGE}).encode()
            self.send_response(403)
            self.send_header("content-type", "application/json")
            self.send_header("x-amzn-errortype", "InvalidSignatureException")
            self.send_header("x-amzn-requestid", REQUEST_ID)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = do_PUT = do_DELETE = _answer


@pytest.fixture
def aws_server() -> Iterator[tuple[str, list[dict[str, str]]]]:
    seen: list[dict[str, str]] = []
    handler = type("Handler", (_Handler,), {"seen": seen})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", seen
    finally:
        server.shutdown()
        server.server_close()


def session() -> boto3.session.Session:
    return boto3.session.Session(
        aws_access_key_id=ACCESS_KEY_ID,
        aws_secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        aws_session_token=SESSION_TOKEN,
        region_name=REGION,
    )


def client(service: str, endpoint: str) -> Any:
    config = Config(retries={"max_attempts": 1}, s3={"addressing_style": "path"})
    return session().client(service, endpoint_url=endpoint, config=config)


def chain(error: BaseException) -> list[BaseException]:
    """Todo lo alcanzable por `__cause__`/`__context__`: lo que un traceback,
    Sentry o un `logger.exception` pueden llegar a mostrar."""
    seen: list[BaseException] = []
    pending: list[BaseException | None] = [error]
    while pending:
        current = pending.pop()
        if current is None or any(current is known for known in seen):
            continue
        seen.append(current)
        pending.extend([current.__cause__, current.__context__])
    return seen


def renderings(error: BaseException) -> list[str]:
    views = ["".join(traceback.format_exception(error))]
    for link in chain(error):
        views.extend([str(link), repr(link), repr(getattr(link, "__dict__", {}))])
    return views


def assert_no_secrets(error: BaseException) -> None:
    for view in renderings(error):
        for secret in SECRETS:
            assert secret not in view
    raw = [link for link in chain(error) if isinstance(link, ClientError | BotoCoreError)]
    assert raw == []


def raised(action: Callable[[], object]) -> BaseException:
    with pytest.raises(Exception) as excinfo:
        action()
    return excinfo.value


# ------------------------------------------------------------ control plane


def test_control_plane_error_keeps_request_id_not_the_signature(
    aws_server: tuple[str, list[dict[str, str]]],
) -> None:
    endpoint, seen = aws_server
    plane = LambdaMicrovmsControlPlane(client("lambda-microvms", endpoint))
    error = raised(lambda: plane.get_microvm("m-1"))
    # La petición sí llevaba los secretos: el test mira el sitio correcto.
    assert ACCESS_KEY_ID in seen[0]["authorization"]
    assert seen[0]["x-amz-security-token"] == SESSION_TOKEN
    assert isinstance(error, SandboxException)
    assert error.aws_code == "InvalidSignatureException"
    assert error.status_code == 403
    assert "does not match the signature you provided" in str(error)
    assert_no_secrets(error)
    cause = error.__cause__
    assert cause is not None
    assert getattr(cause, "request_id", None) == REQUEST_ID
    assert getattr(cause, "status_code", None) == 403
    assert getattr(cause, "code", None) == "InvalidSignatureException"


def test_control_plane_listing_and_identity_paths_are_sanitized(
    aws_server: tuple[str, list[dict[str, str]]],
) -> None:
    endpoint, _ = aws_server
    plane = LambdaMicrovmsControlPlane(
        client("lambda-microvms", endpoint), sts_client=client("sts", endpoint)
    )
    listed = raised(lambda: list(plane.list_microvms()))
    assert isinstance(listed, SandboxException)
    assert_no_secrets(listed)
    identity = raised(lambda: plane.resolve_template_arn("my-image"))
    assert isinstance(identity, SandboxException)
    assert identity.aws_code == "SignatureDoesNotMatch"
    assert getattr(identity.__cause__, "request_id", None) == REQUEST_ID
    assert_no_secrets(identity)


def test_translate_client_error_redacts_the_message() -> None:
    response: dict[str, Any] = {
        "Error": {"Code": "InvalidSignatureException", "Message": INVALID_SIGNATURE_MESSAGE},
        "ResponseMetadata": {"HTTPStatusCode": 403, "RequestId": REQUEST_ID},
    }
    mapped = translate_client_error(ClientError(response, "GetMicrovm"))
    assert isinstance(mapped, SandboxException)
    for secret in SECRETS:
        assert secret not in str(mapped)
    assert mapped.status_code == 403


# ---------------------------------------------------------------------- S3


def test_s3_error_keeps_request_id_not_the_canonical_request(
    aws_server: tuple[str, list[dict[str, str]]],
) -> None:
    endpoint, seen = aws_server
    gateway = S3Gateway(client("s3", endpoint))
    target = StagingObject(bucket=BUCKET, key="k", region=REGION)
    error = raised(lambda: gateway.create_multipart_upload(target))
    assert ACCESS_KEY_ID in seen[0]["authorization"]
    assert seen[0]["x-amz-security-token"] == SESSION_TOKEN
    assert isinstance(error, AuthenticationException)
    assert error.aws_code == "SignatureDoesNotMatch"
    assert_no_secrets(error)
    cause = error.__cause__
    assert cause is not None
    assert getattr(cause, "request_id", None) == REQUEST_ID
    assert getattr(cause, "extended_request_id", None) == HOST_ID
    assert getattr(cause, "status_code", None) == 403


def test_s3_botocore_error_never_names_the_url() -> None:
    def fail() -> None:
        raise EndpointConnectionError(endpoint_url=PRESIGNED_URL)

    error = raised(lambda: s3_call(fail))
    assert isinstance(error, SandboxException)
    assert_no_secrets(error)
    for view in renderings(error):
        assert BUCKET not in view


# ----------------------------------------------------------------- helpers


def test_sanitize_keeps_only_the_allow_listed_fields() -> None:
    response: dict[str, Any] = {
        "Error": {
            "Code": "SignatureDoesNotMatch",
            "Message": "bad signature",
            "AWSAccessKeyId": ACCESS_KEY_ID,
            "CanonicalRequest": f"x-amz-security-token:{SESSION_TOKEN}",
        },
        "ResponseMetadata": {
            "HTTPStatusCode": 403,
            "RequestId": REQUEST_ID,
            "HostId": HOST_ID,
            "RetryAttempts": 2,
            "HTTPHeaders": {"x-debug": SESSION_TOKEN},
        },
    }
    summary = sanitize_aws_error(ClientError(response, "PutObject"))
    assert isinstance(summary, AwsErrorSummary)
    assert str(summary) == "SignatureDoesNotMatch: bad signature"
    assert vars(summary) == {
        "name": "SignatureDoesNotMatch",
        "message": "bad signature",
        "code": "SignatureDoesNotMatch",
        "status_code": 403,
        "request_id": REQUEST_ID,
        "extended_request_id": HOST_ID,
        "attempts": 2,
    }
    assert repr(summary) == (
        "AwsErrorSummary(name='SignatureDoesNotMatch', code='SignatureDoesNotMatch', "
        f"status_code=403, request_id='{REQUEST_ID}', extended_request_id='{HOST_ID}', "
        "attempts=2)"
    )
    assert str(sanitize_aws_error(ClientError(response, "PutObject"), include_message=False)) == (
        "SignatureDoesNotMatch"
    )


def test_redact_aws_text_strips_signatures_tokens_and_key_ids() -> None:
    raw_request = (
        f"authorization: AWS4-HMAC-SHA256 Credential={ACCESS_KEY_ID}/20260924/{REGION}/s3/"
        f"aws4_request, Signature={'f' * 64}\r\nx-amz-security-token: {SESSION_TOKEN}\r\n"
    )
    redacted = redact_aws_text(f"{INVALID_SIGNATURE_MESSAGE} {raw_request} {PRESIGNED_URL}")
    for secret in SECRETS:
        assert secret not in redacted
    assert redact_aws_text(PRESIGNED_URL) == (
        f"https://{BUCKET}.s3.{REGION}.amazonaws.com/k?<redacted>"
    )
    assert "authorization: <redacted>" in redact_aws_text(raw_request)
    assert redact_aws_text("Rate exceeded") == "Rate exceeded"
    assert len(redact_aws_text("x" * 5000)) == 2049
