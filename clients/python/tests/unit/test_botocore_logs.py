"""`import rayito` deja fuera el DEBUG de botocore que lleva cuerpos de AWS:
con el logger raíz a DEBUG, una llamada real de boto3 (contra un servidor HTTP
local, sin AWS) no escribe ni el valor que manda `PutSecretValue` ni el que
devuelve `GetSecretValue`, y los avisos de botocore siguen saliendo."""

from __future__ import annotations

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


class SecretsManagerDouble(BaseHTTPRequestHandler):
    """Responde a cualquier llamada con un `GetSecretValue` que lleva
    `SECRET_VALUE`."""

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("content-length", "0")))
        body = json.dumps({"Name": SECRET_ID, "SecretString": SECRET_VALUE}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/x-amz-json-1.1")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return None


@pytest.fixture
def endpoint() -> Iterator[str]:
    server = HTTPServer(("127.0.0.1", 0), SecretsManagerDouble)
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
    """La raíz y `botocore` a DEBUG, y cada logger de botocore activo y
    propagando, aunque otro test los haya subido de nivel o desactivado."""
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger="botocore")
    for name, logger in list(logging.Logger.manager.loggerDict.items()):
        if name.startswith("botocore") and isinstance(logger, logging.Logger):
            monkeypatch.setattr(logger, "disabled", False)
            monkeypatch.setattr(logger, "propagate", True)
            monkeypatch.setattr(logger, "level", logging.NOTSET)
    return caplog


def test_a_debug_root_logger_never_sees_a_secret_in_a_botocore_body(
    endpoint: str, botocore_at_debug: pytest.LogCaptureFixture
) -> None:
    caplog = botocore_at_debug
    client = boto3.client(
        "secretsmanager",
        region_name="us-east-1",
        endpoint_url=endpoint,
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    )
    client.put_secret_value(SecretId=SECRET_ID, SecretString=SECRET_VALUE)
    assert client.get_secret_value(SecretId=SECRET_ID)["SecretString"] == SECRET_VALUE
    assert any(record.name.startswith("botocore") for record in caplog.records)
    assert not any(SECRET_VALUE in record.getMessage() for record in caplog.records)


def test_botocore_warnings_still_reach_the_logs(
    botocore_at_debug: pytest.LogCaptureFixture,
) -> None:
    caplog = botocore_at_debug
    logging.getLogger("botocore.endpoint").warning("retrying")
    logging.getLogger("botocore.parsers").debug("Response body:\n%r", SECRET_VALUE)
    assert [record.getMessage() for record in caplog.records] == ["retrying"]
