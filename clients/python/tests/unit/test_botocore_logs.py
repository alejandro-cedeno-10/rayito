"""`import rayito` deja fuera el DEBUG del SDK de AWS que lleva parámetros y
cuerpos: con el logger raíz a DEBUG, llamadas reales de boto3 (contra un
servidor HTTP local, sin AWS) no escriben ni el valor que manda
`PutSecretValue` ni el que devuelve `GetSecretValue`, ni el `Item` de un
`Table.put_item` o de `batch_writer`, ni los `Metadata` y el cuerpo de un
`upload_fileobj`; y los avisos de botocore siguen saliendo."""

from __future__ import annotations

import io
import json
import logging
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Final

import boto3
import pytest

import rayito  # noqa: F401
from rayito._botocore_logs import (
    BOTOCORE_BODY_FILTER,
    BOTOCORE_BODY_LOGGERS,
    drop_botocore_body_logs,
)

SECRET_VALUE: Final = "not-a-real-secret-value"
SECRET_ID: Final = "rayito/test"
#: Los paquetes del SDK de AWS para Python; `urllib3` no es uno de ellos y su
#: DEBUG sólo lleva la línea de petición.
AWS_SDK_PREFIXES: Final = ("botocore", "boto3", "s3transfer")
FAKE_CREDENTIALS: Final = {
    "region_name": "us-east-1",
    "aws_access_key_id": "testing",
    "aws_secret_access_key": "testing",
}


class AwsDouble(BaseHTTPRequestHandler):
    """Responde a Secrets Manager con un `GetSecretValue` que lleva
    `SECRET_VALUE`, a DynamoDB con un `UnprocessedItems` vacío y a S3 con un `200` sin
    cuerpo."""

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("content-length", "0")))
        if "DynamoDB" in self.headers.get("x-amz-target", ""):
            self._reply(b'{"UnprocessedItems": {}}', "application/x-amz-json-1.0")
            return
        body = json.dumps({"Name": SECRET_ID, "SecretString": SECRET_VALUE}).encode()
        self._reply(body, "application/x-amz-json-1.1")

    def do_PUT(self) -> None:
        self.rfile.read(int(self.headers.get("content-length", "0")))
        self._reply(b"", "application/xml")

    def _reply(self, body: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return None


@pytest.fixture
def endpoint() -> Iterator[str]:
    server = HTTPServer(("127.0.0.1", 0), AwsDouble)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


def test_importing_rayito_installs_the_filter_once() -> None:
    drop_botocore_body_logs()
    for name in BOTOCORE_BODY_LOGGERS:
        assert logging.getLogger(name).filters.count(BOTOCORE_BODY_FILTER) == 1


@pytest.fixture
def botocore_at_debug(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> pytest.LogCaptureFixture:
    """La raíz y los loggers del SDK de AWS a DEBUG, activos y propagando,
    aunque otro test los haya subido de nivel o desactivado."""
    caplog.set_level(logging.DEBUG)
    for prefix in AWS_SDK_PREFIXES:
        caplog.set_level(logging.DEBUG, logger=prefix)
    for name, logger in list(logging.Logger.manager.loggerDict.items()):
        if name.startswith(AWS_SDK_PREFIXES) and isinstance(logger, logging.Logger):
            monkeypatch.setattr(logger, "disabled", False)
            monkeypatch.setattr(logger, "propagate", True)
            monkeypatch.setattr(logger, "level", logging.NOTSET)
    return caplog


def test_a_debug_root_logger_never_sees_a_secret_in_a_botocore_body(
    endpoint: str, botocore_at_debug: pytest.LogCaptureFixture
) -> None:
    caplog = botocore_at_debug
    client = boto3.client("secretsmanager", endpoint_url=endpoint, **FAKE_CREDENTIALS)
    client.put_secret_value(SecretId=SECRET_ID, SecretString=SECRET_VALUE)
    assert client.get_secret_value(SecretId=SECRET_ID)["SecretString"] == SECRET_VALUE
    assert any(record.name.startswith("botocore") for record in caplog.records)
    assert not any(SECRET_VALUE in record.getMessage() for record in caplog.records)


def _aws_sdk_messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage() for record in caplog.records if record.name.startswith(AWS_SDK_PREFIXES)
    ]


def test_a_debug_root_logger_never_sees_a_value_in_boto3_resource_or_transfer_calls(
    endpoint: str, botocore_at_debug: pytest.LogCaptureFixture
) -> None:
    caplog = botocore_at_debug
    table = boto3.resource("dynamodb", endpoint_url=endpoint, **FAKE_CREDENTIALS).Table("t")
    table.put_item(Item={"url": SECRET_VALUE, "secret_name": SECRET_ID})
    with table.batch_writer(overwrite_by_pkeys=["url"]) as batch:
        batch.put_item(Item={"url": SECRET_VALUE})
        batch.put_item(Item={"url": SECRET_VALUE})
    s3 = boto3.client("s3", endpoint_url=endpoint, **FAKE_CREDENTIALS)
    s3.upload_fileobj(
        io.BytesIO(SECRET_VALUE.encode()),
        "amzn-s3-demo-bucket",
        "object",
        ExtraArgs={"Metadata": {"m": SECRET_VALUE}},
    )
    messages = _aws_sdk_messages(caplog)
    assert any(record.name.startswith("boto3") for record in caplog.records)
    assert not any(SECRET_VALUE in message for message in messages)
    assert not any(SECRET_ID in message for message in messages)


def test_botocore_warnings_still_reach_the_logs(
    botocore_at_debug: pytest.LogCaptureFixture,
) -> None:
    caplog = botocore_at_debug
    logging.getLogger("botocore.endpoint").warning("retrying")
    logging.getLogger("botocore.parsers").debug("Response body:\n%r", SECRET_VALUE)
    assert [record.getMessage() for record in caplog.records] == ["retrying"]
