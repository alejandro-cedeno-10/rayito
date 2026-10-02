"""`handlers.forwarder.handler` with fake ports, in exactly the environment
`ForwarderFunction` declares in `infra/events-webhooks.yaml`: a verified
line is stored once, and only a newly stored event moves the sandbox's
state (a duplicate or late line after `killed` never reopens it).
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import hmac
import json
from typing import Any

import pytest
from conftest import FakeDynamoResource, FakeTable, template_environment
from domain.mac import derive_sandbox_key
from handlers import forwarder

STACK_KEY = b"stack-wide-secret"
SANDBOX_ID = "sbx-0000000000000001"
LOG_STREAM = f"2026/10/02[1]{SANDBOX_ID}"


class _FakeSecretsClient:
    def __init__(self) -> None:
        self.requested_secret_ids: list[str] = []

    def get_secret_value(self, *, SecretId: str) -> dict[str, str]:
        self.requested_secret_ids.append(SecretId)
        return {"SecretString": STACK_KEY.decode("utf-8")}


def _line(*, event_id: str, kind: str, occurred_at_ms: int) -> str:
    event: dict[str, Any] = {
        "event_id": event_id,
        "sandbox_id": SANDBOX_ID,
        "kind": kind,
        "generation": 0,
        "occurred_at_ms": occurred_at_ms,
        "image_arn": "arn:test",
        "image_version": "1",
    }
    if kind == "killed":
        event["kill_reason"] = "request"
    payload = json.dumps(event).encode("utf-8")
    mac = hmac.new(derive_sandbox_key(STACK_KEY, SANDBOX_ID), payload, hashlib.sha256).digest()
    return f"rayito.event.v1 {_b64(payload)} {_b64(mac)}"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _subscription_event(*messages: str) -> dict[str, Any]:
    record = {
        "logStream": LOG_STREAM,
        "logEvents": [{"message": message} for message in messages],
    }
    data = base64.b64encode(gzip.compress(json.dumps(record).encode("utf-8"))).decode()
    return {"awslogs": {"data": data}}


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> tuple[FakeTable, _FakeSecretsClient]:
    environment = template_environment("ForwarderFunction")
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    table, secrets = FakeTable(), _FakeSecretsClient()
    monkeypatch.setattr(forwarder, "_store", None)
    monkeypatch.setattr(forwarder, "_secrets", None)
    monkeypatch.setattr(forwarder.boto3, "resource", lambda _name: FakeDynamoResource(table))
    monkeypatch.setattr(forwarder.boto3, "client", lambda _name: secrets)
    return table, secrets


def test_reads_the_stack_key_the_template_names(
    wired: tuple[FakeTable, _FakeSecretsClient],
) -> None:
    table, secrets = wired
    result = forwarder.handler(
        _subscription_event(_line(event_id="evt-1", kind="created", occurred_at_ms=1)), None
    )
    assert result == {"accepted": 1, "rejected": 0}
    assert secrets.requested_secret_ids == [template_environment("ForwarderFunction")[
        "STACK_KEY_SECRET_ID"
    ]]
    assert table.items[(f"STATE#{SANDBOX_ID}", "STATE")]["last_kind"] == "created"


def test_a_line_after_killed_never_reopens_the_sandbox(
    wired: tuple[FakeTable, _FakeSecretsClient],
) -> None:
    table, _secrets = wired
    forwarder.handler(
        _subscription_event(
            _line(event_id="evt-1", kind="created", occurred_at_ms=1),
            _line(event_id="evt-2", kind="killed", occurred_at_ms=5),
            _line(event_id="evt-3", kind="resumed", occurred_at_ms=9),
        ),
        None,
    )
    assert table.items[(f"STATE#{SANDBOX_ID}", "STATE")]["last_kind"] == "killed"


def test_a_duplicate_line_does_not_move_the_state(
    wired: tuple[FakeTable, _FakeSecretsClient],
) -> None:
    table, _secrets = wired
    paused = _line(event_id="evt-2", kind="paused", occurred_at_ms=5)
    forwarder.handler(_subscription_event(paused), None)
    forwarder.handler(
        _subscription_event(_line(event_id="evt-3", kind="resumed", occurred_at_ms=9)), None
    )
    forwarder.handler(_subscription_event(paused), None)  # CloudWatch Logs redelivery
    assert table.items[(f"STATE#{SANDBOX_ID}", "STATE")]["last_kind"] == "resumed"
