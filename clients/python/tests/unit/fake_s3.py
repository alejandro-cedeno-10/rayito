"""S3 falso para las transferencias (ADR-010).

Dos caras sobre el mismo almacén en memoria:

- El SDK usa un cliente boto3 **real** (firmas SigV4 reales y validación de
  parámetros contra el modelo de botocore) creado desde `FakeS3.session()`,
  cuyo manejador `before-send.s3` responde las operaciones de
  `AWS_API_NOTES.md` §18 sin salir a la red.
- `rayd` y el usuario sólo tienen URLs prefirmadas: `put_url`, `get_url`,
  `delete_url` y `post_form` hacen lo que haría un cliente HTTP sin
  credenciales contra esa URL (host virtual regional, clave en la ruta).

El registro `calls` guarda la operación y la clave, nunca la URL.
"""

from __future__ import annotations

import base64
import hashlib
import io
import itertools
import json
import threading
import urllib.parse
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

import boto3
from botocore.awsrequest import AWSPreparedRequest, AWSResponse

BUCKET = "amzn-s3-demo-bucket"
REGION = "us-east-1"
FAKE_ACCESS_KEY = "AKIAIOSFODNN7EXAMPLE"
FAKE_SECRET_KEY = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
S3_HOST_SUFFIX = f".s3.{REGION}.amazonaws.com"
XML_HEADERS = {"Content-Type": "application/xml"}


class RawBody:
    """El `raw` de una `AWSResponse`: lo que botocore lee como cuerpo."""

    def __init__(self, data: bytes) -> None:
        self._buffer = io.BytesIO(data)

    def stream(self, amt: int = 1024, decode_content: bool = False) -> Iterator[bytes]:
        while True:
            chunk = self._buffer.read(amt)
            if not chunk:
                return
            yield chunk

    def read(self, amt: int | None = None) -> bytes:
        return self._buffer.read() if amt is None else self._buffer.read(amt)

    def close(self) -> None:
        self._buffer.close()


@dataclass(frozen=True)
class ParsedUrl:
    bucket: str
    key: str
    query: dict[str, str]


def parse_s3_url(url: str) -> ParsedUrl:
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    if not host.endswith(S3_HOST_SUFFIX):
        raise AssertionError("el fake sólo sirve el host virtual regional de us-east-1")
    query = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
    return ParsedUrl(
        bucket=host[: -len(S3_HOST_SUFFIX)],
        key=urllib.parse.unquote(parts.path.lstrip("/")),
        query=query,
    )


def header_text(headers: Mapping[str, Any], name: str) -> str:
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value.decode() if isinstance(value, bytes) else str(value)
    return ""


def request_body(request: AWSPreparedRequest) -> bytes:
    body = request.body
    if body is None:
        return b""
    if isinstance(body, bytes | bytearray):
        return bytes(body)
    if isinstance(body, str):
        return body.encode()
    return bytes(body.read())


def decode_aws_chunked(data: bytes) -> bytes:
    """El cuerpo `aws-chunked` que botocore manda con el checksum CRC32 en el
    trailer: `<hex>\\r\\n<bytes>\\r\\n ... 0\\r\\n<trailer>`."""
    decoded = bytearray()
    position = 0
    while True:
        line_end = data.index(b"\r\n", position)
        size = int(data[position:line_end].split(b";")[0], 16)
        position = line_end + 2
        if size == 0:
            return bytes(decoded)
        decoded += data[position : position + size]
        position += size + 2


def etag_of(data: bytes) -> str:
    return f'"{hashlib.md5(data, usedforsecurity=False).hexdigest()}"'


def error_response(url: str, status: int, code: str, message: str) -> AWSResponse:
    body = f"<Error><Code>{code}</Code><Message>{message}</Message></Error>".encode()
    return AWSResponse(url, status, XML_HEADERS, RawBody(body))


def xml_value(document: bytes, tag: str) -> list[str]:
    text = document.decode()
    values: list[str] = []
    start_tag, end_tag = f"<{tag}>", f"</{tag}>"
    position = text.find(start_tag)
    while position != -1:
        end = text.index(end_tag, position)
        values.append(text[position + len(start_tag) : end])
        position = text.find(start_tag, end)
    return values


@dataclass
class FakeS3:
    """Almacén de objetos y subidas multiparte de un único bucket."""

    objects: dict[str, bytes] = field(default_factory=dict)
    content_types: dict[str, str] = field(default_factory=dict)
    uploads: dict[str, dict[int, bytes]] = field(default_factory=dict)
    upload_keys: dict[str, str] = field(default_factory=dict)
    completed_uploads: list[str] = field(default_factory=list)
    aborted_uploads: list[str] = field(default_factory=list)
    calls: list[tuple[str, str]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    _upload_ids: Iterator[int] = field(default_factory=itertools.count)

    def session(self) -> boto3.session.Session:
        """Una sesión con credenciales de prueba cuyo tráfico S3 responde
        este fake; se registra antes de crear ningún cliente."""
        session = boto3.session.Session(
            region_name=REGION,
            aws_access_key_id=FAKE_ACCESS_KEY,
            aws_secret_access_key=FAKE_SECRET_KEY,
        )
        session.events.register("before-send.s3", self.handle)
        return session

    # ------------------------------------------------------ presigned URLs

    def put_url(self, url: str, data: bytes) -> tuple[int, str]:
        """`PUT` crudo sobre una URL de `put_object` o de `upload_part`:
        `(status, etag)`."""
        parsed = parse_s3_url(url)
        if "partNumber" in parsed.query:
            return self._put_part(parsed, data)
        with self.lock:
            self.calls.append(("PUT", parsed.key))
            self.objects[parsed.key] = data
        return 200, etag_of(data)

    def get_url(self, url: str) -> tuple[int, bytes]:
        parsed = parse_s3_url(url)
        with self.lock:
            self.calls.append(("GET", parsed.key))
            data = self.objects.get(parsed.key)
        if data is None:
            return 404, b"<Error><Code>NoSuchKey</Code></Error>"
        return 200, data

    def delete_url(self, url: str) -> int:
        parsed = parse_s3_url(url)
        with self.lock:
            self.calls.append(("DELETE", parsed.key))
            self.objects.pop(parsed.key, None)
        return 204

    def post_form(self, url: str, fields: Mapping[str, str], data: bytes) -> int:
        """El `POST` multiparte de `generate_presigned_post`: aplica la
        condición `content-length-range` de su política como S3."""
        policy = json.loads(base64.b64decode(fields["policy"]))
        for condition in policy["conditions"]:
            is_range = isinstance(condition, list) and condition[0] == "content-length-range"
            if is_range and not condition[1] <= len(data) <= condition[2]:
                return 400
        key = fields["key"]
        with self.lock:
            self.calls.append(("POST", key))
            self.objects[key] = data
        return 204

    def seed(self, key: str, data: bytes) -> None:
        with self.lock:
            self.objects[key] = data

    def keys(self) -> list[str]:
        with self.lock:
            return sorted(self.objects)

    def operations(self) -> list[str]:
        with self.lock:
            return [operation for operation, _ in self.calls]

    # --------------------------------------------------- boto3 before-send

    def handle(self, request: AWSPreparedRequest, **_: Any) -> AWSResponse:
        parsed = parse_s3_url(request.url)
        method = request.method
        if method == "POST" and "uploads" in parsed.query:
            return self._create_upload(request.url, parsed)
        if method == "POST" and "uploadId" in parsed.query:
            return self._complete_upload(request.url, parsed, request_body(request))
        if method == "DELETE" and "uploadId" in parsed.query:
            return self._abort_upload(request.url, parsed)
        if method == "PUT":
            return self._sdk_put(request, parsed)
        if method in ("GET", "HEAD"):
            return self._sdk_get(request, parsed, head=method == "HEAD")
        if method == "DELETE":
            self.delete_url(request.url)
            return AWSResponse(request.url, 204, {}, RawBody(b""))
        return error_response(request.url, 400, "NotImplemented", "fake")

    def _sdk_put(self, request: AWSPreparedRequest, parsed: ParsedUrl) -> AWSResponse:
        body = request_body(request)
        if "aws-chunked" in header_text(request.headers, "Content-Encoding"):
            body = decode_aws_chunked(body)
        if "partNumber" in parsed.query:
            status, etag = self._put_part(parsed, body)
            return AWSResponse(request.url, status, {"ETag": etag}, RawBody(b""))
        with self.lock:
            self.calls.append(("SDK_PUT", parsed.key))
            self.objects[parsed.key] = body
            self.content_types[parsed.key] = header_text(request.headers, "Content-Type")
        return AWSResponse(request.url, 200, {"ETag": etag_of(body)}, RawBody(b""))

    def _sdk_get(
        self, request: AWSPreparedRequest, parsed: ParsedUrl, *, head: bool
    ) -> AWSResponse:
        with self.lock:
            self.calls.append(("SDK_HEAD" if head else "SDK_GET", parsed.key))
            data = self.objects.get(parsed.key)
        if data is None:
            return error_response(
                request.url, 404, "NoSuchKey", "The specified key does not exist."
            )
        headers = {"Content-Length": str(len(data)), "ETag": etag_of(data)}
        byte_range = header_text(request.headers, "Range")
        if byte_range and not head:
            first, last = byte_range.removeprefix("bytes=").split("-")
            end = len(data) - 1 if not last else min(int(last), len(data) - 1)
            chunk = data[int(first) : end + 1]
            headers["Content-Length"] = str(len(chunk))
            headers["Content-Range"] = f"bytes {first}-{end}/{len(data)}"
            return AWSResponse(request.url, 206, headers, RawBody(chunk))
        return AWSResponse(request.url, 200, headers, RawBody(b"" if head else data))

    def _put_part(self, parsed: ParsedUrl, data: bytes) -> tuple[int, str]:
        upload_id = parsed.query["uploadId"]
        with self.lock:
            self.calls.append(("UPLOAD_PART", parsed.key))
            parts = self.uploads.get(upload_id)
            if parts is None or self.upload_keys.get(upload_id) != parsed.key:
                return 404, ""
            parts[int(parsed.query["partNumber"])] = data
        return 200, etag_of(data)

    def _create_upload(self, url: str, parsed: ParsedUrl) -> AWSResponse:
        with self.lock:
            upload_id = f"upload-{next(self._upload_ids)}"
            self.calls.append(("CREATE_MULTIPART", parsed.key))
            self.uploads[upload_id] = {}
            self.upload_keys[upload_id] = parsed.key
        body = (
            "<InitiateMultipartUploadResult>"
            f"<Bucket>{parsed.bucket}</Bucket><Key>{parsed.key}</Key>"
            f"<UploadId>{upload_id}</UploadId></InitiateMultipartUploadResult>"
        ).encode()
        return AWSResponse(url, 200, XML_HEADERS, RawBody(body))

    def _complete_upload(self, url: str, parsed: ParsedUrl, body: bytes) -> AWSResponse:
        upload_id = parsed.query["uploadId"]
        numbers = [int(value) for value in xml_value(body, "PartNumber")]
        etags = [value.replace("&quot;", '"') for value in xml_value(body, "ETag")]
        with self.lock:
            self.calls.append(("COMPLETE_MULTIPART", parsed.key))
            parts = self.uploads.pop(upload_id, None)
            if parts is None:
                return error_response(url, 404, "NoSuchUpload", "fake")
            if [etag_of(parts[number]) for number in numbers] != etags:
                return error_response(url, 400, "InvalidPart", "fake")
            self.objects[parsed.key] = b"".join(parts[number] for number in numbers)
            self.completed_uploads.append(upload_id)
        body_out = (
            "<CompleteMultipartUploadResult>"
            f"<Bucket>{parsed.bucket}</Bucket><Key>{parsed.key}</Key>"
            f"<ETag>&quot;done&quot;</ETag></CompleteMultipartUploadResult>"
        ).encode()
        return AWSResponse(url, 200, XML_HEADERS, RawBody(body_out))

    def _abort_upload(self, url: str, parsed: ParsedUrl) -> AWSResponse:
        with self.lock:
            self.calls.append(("ABORT_MULTIPART", parsed.key))
            self.uploads.pop(parsed.query["uploadId"], None)
            self.aborted_uploads.append(parsed.query["uploadId"])
        return AWSResponse(url, 204, {}, RawBody(b""))
