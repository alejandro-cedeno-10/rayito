"""CloudWatch Logs subscription handler: verifies the MAC on every
`rayito.event.v1` line and writes it, idempotently, to the events table.
Never raises past the handler boundary — a malformed or forged line is
counted and dropped, not a Lambda failure (a failure would make CloudWatch
Logs retry the whole batch, including the lines that *did* verify).
"""

from __future__ import annotations

import base64
import gzip
import json
import os
from typing import Any

import boto3

from adapters.dynamodb import DynamoDbStore
from adapters.secrets import SecretsManagerReader
from domain.event import LINE_TOKEN
from domain.forwarding import Accepted, decide

_DYNAMODB_TABLE_ENV = "EVENTS_TABLE_NAME"
_STACK_KEY_SECRET_ENV = "STACK_KEY_SECRET_ID"

_store: DynamoDbStore | None = None
_secrets: SecretsManagerReader | None = None


def _store_singleton() -> DynamoDbStore:
    global _store
    if _store is None:
        table = boto3.resource("dynamodb").Table(os.environ[_DYNAMODB_TABLE_ENV])
        _store = DynamoDbStore(table)
    return _store


def _secrets_singleton() -> SecretsManagerReader:
    global _secrets
    if _secrets is None:
        _secrets = SecretsManagerReader(
            boto3.client("secretsmanager"),
            stack_key_secret_id=os.environ[_STACK_KEY_SECRET_ENV],
        )
    return _secrets


def handler(event: dict[str, Any], _context: object) -> dict[str, int]:
    record = json.loads(gzip.decompress(base64.b64decode(event["awslogs"]["data"])))
    log_stream = record.get("logStream", "")
    stack_key = _secrets_singleton().stack_key()
    store = _store_singleton()
    accepted = 0
    rejected = 0
    for log_event in record.get("logEvents", []):
        message = log_event.get("message", "")
        if not message.startswith(LINE_TOKEN):
            continue  # ordinary agent log line, not an event line
        decision = decide(log_stream=log_stream, message=message, stack_key=stack_key)
        if isinstance(decision, Accepted):
            store.put_event_if_absent(decision.event)
            store.mark_sandbox_state(decision.event.sandbox_id, decision.event)
            accepted += 1
        else:
            rejected += 1
    return {"accepted": accepted, "rejected": rejected}
