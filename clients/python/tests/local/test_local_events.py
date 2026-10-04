"""Eventos de ciclo de vida y webhooks contra Floci (`make local-e2e`).

`LifecycleEvents` despliega la pila `events-webhooks` con CloudFormation y
gestiona los webhooks en DynamoDB, y `create(events=)` entrega la clave
derivada al `rayd` del guest. Los tres Lambdas (forwarder, deliverer,
reconciler) se ejecutan **en proceso** con su propio código
(`infra/lambdas/events_webhooks`) contra las tablas, secretos y el plano de
control de Floci: el Lambda de Floci necesitaría el socket de Docker, que el
entorno local no le da. El forwarder recibe una línea firmada como la que
escribe `rayd` (la suscripción de CloudWatch Logs no existe en local) y el
deliverer entrega a un receptor HTTP local con un `HttpSender` de test en
lugar del cliente HTTPS con su filtro SSRF, que cubren los tests unitarios
de los Lambdas.
"""

from __future__ import annotations

import base64
import contextlib
import gzip
import hashlib
import hmac
import http.client
import http.server
import importlib
import json
import secrets as stdlib_secrets
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, Final

import boto3
import pytest

from rayito import LifecycleEvents, SecretStore
from rayito._lifecycle_events._keys import derive_sandbox_key
from rayito._secrets import WEBHOOK_SECRET_PREFIX
from rayito.exceptions import SandboxNotFoundException

from .conftest import LocalSettings, create_local_sandbox
from .guest import LocalGuestControlPlane

pytestmark = pytest.mark.local

LAMBDA_ROOT: Final = Path(__file__).resolve().parents[4] / "infra" / "lambdas" / "events_webhooks"
KILLED_TYPE: Final = "sandbox.lifecycle.killed"
#: El prefijo de una línea de evento de `rayd` (`domain/event.py`).
EVENT_LINE_TOKEN: Final = "rayito.event.v1"
LOG_GROUP: Final = "/aws/lambda-microvms/rayito-local"
#: El `Timeout` de los Lambdas en `infra/events-webhooks.yaml`.
LAMBDA_TIMEOUT_MILLIS: Final = 60_000
RECEIVER_OK: Final = 204
#: `rayd` mints `event_id` as this many random bytes, hex-encoded
#: (`features/lifecycle_events.rs::EVENT_ID_BYTES`); the forwarder refuses
#: any other shape as `malformed_line`.
RAYD_EVENT_ID_BYTES: Final = 16
#: `events=` exige logs de CloudWatch y, con ellos, un rol de ejecución;
#: Floci no lo asume.
LOCAL_EXECUTION_ROLE_ARN: Final = "arn:aws:iam::000000000000:role/rayito-local-sandbox"


def load_lambda(module: str) -> ModuleType:
    """Un módulo de `infra/lambdas/events_webhooks` importado como en el
    runtime de Lambda (la raíz del zip en `sys.path`)."""
    if str(LAMBDA_ROOT) not in sys.path:
        sys.path.insert(0, str(LAMBDA_ROOT))
    return importlib.import_module(module)


@dataclass
class Received:
    headers: dict[str, str]
    body: bytes


@dataclass
class Receiver:
    """Receptor de webhooks en loopback: guarda cada POST y responde 204."""

    deliveries: list[Received] = field(default_factory=list)
    server: http.server.ThreadingHTTPServer | None = None

    @property
    def port(self) -> int:
        assert self.server is not None
        return int(self.server.server_address[1])


@pytest.fixture
def receiver() -> Iterator[Receiver]:
    state = Receiver()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("content-length", "0"))
            state.deliveries.append(
                Received({k.lower(): v for k, v in self.headers.items()}, self.rfile.read(length))
            )
            self.send_response(RECEIVER_OK)
            self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:
            return None

    state.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=state.server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        state.server.shutdown()
        state.server.server_close()


class LoopbackSender:
    """`ports.HttpSender` de test: entrega en el receptor local sea cual sea
    la URL registrada (que `register_webhook` exige `https://`)."""

    def __init__(self, port: int) -> None:
        self._port = port

    def post(self, url: str, headers: dict[str, str], body: bytes, *, timeout: float) -> int:
        connection = http.client.HTTPConnection("127.0.0.1", self._port, timeout=timeout)
        try:
            connection.request("POST", "/hook", body=body, headers=headers)
            return connection.getresponse().status
        finally:
            connection.close()


class LambdaContext:
    def get_remaining_time_in_millis(self) -> int:
        return LAMBDA_TIMEOUT_MILLIS


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def signed_event_line(stack_key: bytes, sandbox_id: str, image_arn: str) -> str:
    """La línea que `rayd` escribe al terminar (`rayito.event.v1 <payload>
    <mac>`, HMAC-SHA256 con la clave derivada del sandbox)."""
    payload = json.dumps(
        {
            "event_id": stdlib_secrets.token_hex(RAYD_EVENT_ID_BYTES),
            "sandbox_id": sandbox_id,
            "kind": "killed",
            "generation": 0,
            "occurred_at_ms": int(time.time() * 1000),
            "image_arn": image_arn,
            "image_version": "1.0",
            "kill_reason": "request",
        }
    ).encode()
    mac = hmac.new(derive_sandbox_key(stack_key, sandbox_id), payload, hashlib.sha256).digest()
    return f"{EVENT_LINE_TOKEN} {b64(payload)} {b64(mac)}"


def subscription_payload(sandbox_id: str, line: str) -> dict[str, Any]:
    """Lo que una suscripción de CloudWatch Logs entrega al forwarder; el
    stream se llama como el del MicroVM (`AWS_API_NOTES.md` §25)."""
    record = {
        "logStream": f"2026/10/04[1.0]{sandbox_id}",
        "logEvents": [{"id": "1", "timestamp": int(time.time() * 1000), "message": line}],
    }
    return {"awslogs": {"data": b64(gzip.compress(json.dumps(record).encode()))}}


def stream_records(session: boto3.session.Session, table: str) -> list[dict[str, Any]]:
    """Los registros del stream de la tabla de eventos (el disparador del
    deliverer), leídos de Floci desde el principio de cada shard."""
    stream_arn = session.client("dynamodb").describe_table(TableName=table)["Table"][
        "LatestStreamArn"
    ]
    streams: Any = session.client("dynamodbstreams")
    records: list[dict[str, Any]] = []
    for shard in streams.describe_stream(StreamArn=stream_arn)["StreamDescription"]["Shards"]:
        iterator = streams.get_shard_iterator(
            StreamArn=stream_arn, ShardId=shard["ShardId"], ShardIteratorType="TRIM_HORIZON"
        )["ShardIterator"]
        records.extend(streams.get_records(ShardIterator=iterator)["Records"])
    return records


def test_events_pipeline_from_create_to_signed_webhook(
    local_settings: LocalSettings,
    control_plane: LocalGuestControlPlane,
    template_arn: str,
    artifact_bucket: str,
    aws_session: boto3.session.Session,
    receiver: Receiver,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logs: Any = aws_session.client("logs")
    with contextlib.suppress(logs.exceptions.ResourceAlreadyExistsException):
        logs.create_log_group(logGroupName=LOG_GROUP)
    events = LifecycleEvents(
        stack_name=f"rayito-local-events-{stdlib_secrets.token_hex(4)}", session=aws_session
    )
    stack = events.deploy(artifact_bucket=artifact_bucket, log_group_name=LOG_GROUP)
    secrets = SecretStore(session=aws_session, prefix=WEBHOOK_SECRET_PREFIX)
    secret_name = f"local-{stdlib_secrets.token_hex(4)}"
    secret_value = stdlib_secrets.token_hex(16)
    secrets.create(secret_name, secret_value)
    try:
        webhook = events.register_webhook(
            "https://receiver.example.com/hook", secret_name=secret_name, types=[KILLED_TYPE]
        )
        assert webhook.webhook_id in {item.webhook_id for item in events.list_webhooks()}

        sandbox = create_local_sandbox(
            local_settings,
            control_plane,
            template_arn,
            events=events,
            logging="cloudwatch",
            execution_role_arn=LOCAL_EXECUTION_ROLE_ARN,
        )
        with contextlib.suppress(SandboxNotFoundException):
            sandbox.kill()

        table = stack.outputs["EventsTableName"]
        monkeypatch.setenv("EVENTS_TABLE_NAME", table)
        monkeypatch.setenv("STACK_KEY_SECRET_ID", stack.outputs["StackKeySecretArn"])
        stack_key = aws_session.client("secretsmanager").get_secret_value(
            SecretId=stack.outputs["StackKeySecretArn"]
        )["SecretString"]
        forwarder = load_lambda("handlers.forwarder")
        line = signed_event_line(stack_key.encode(), sandbox.sandbox_id, template_arn)
        forged = signed_event_line(b"not-the-stack-key", sandbox.sandbox_id, template_arn)
        assert forwarder.handler(subscription_payload(sandbox.sandbox_id, line), None) == {
            "accepted": 1,
            "rejected": 0,
        }
        assert forwarder.handler(subscription_payload(sandbox.sandbox_id, forged), None) == {
            "accepted": 0,
            "rejected": 1,
        }
        recorded = events.get_events(sandbox_id=sandbox.sandbox_id)
        assert [record.type for record in recorded] == [KILLED_TYPE]

        deliverer = load_lambda("handlers.deliverer")
        monkeypatch.setattr(deliverer, "_sender", LoopbackSender(receiver.port))
        result = deliverer.handler({"Records": stream_records(aws_session, table)}, LambdaContext())
        assert result == {"batchItemFailures": []}
        assert len(receiver.deliveries) == 1
        delivery = receiver.deliveries[0]
        expected = base64.b64encode(
            hashlib.sha256(secret_value.encode() + delivery.body).digest()
        ).decode()
        assert delivery.headers["e2b-signature"] == expected.rstrip("=")
        assert delivery.headers["e2b-webhook-id"] == webhook.webhook_id
        assert json.loads(delivery.body)["sandbox_id"] == sandbox.sandbox_id
        events.delete_webhook(webhook.webhook_id)
    finally:
        secrets.destroy(secret_name)
        events.destroy()
