"""CloudWatch Logs subscription handler: verifies the MAC on every
`rayito.event.v1` line and writes it, idempotently, to the events table.

A malformed, forged or stale line is counted and dropped, never a Lambda
failure: CloudWatch Logs invokes this function asynchronously, and a failed
invocation is retried and then discarded as a whole batch, genuine lines
included. `decide` never raises; if it ever did, that one line is counted
under `REASON_INTERNAL_ERROR` and the batch goes on.

A failed table write is different (DynamoDB unavailable, not the line's
fault): every other line is still processed, and only then does the
invocation fail, so Lambda's asynchronous retries run it again (the writes
are idempotent) and, once they are exhausted, the batch lands in the
`ForwarderFailuresQueue` OnFailure destination instead of disappearing
(`infra/events-webhooks.yaml`).
"""

from __future__ import annotations

import base64
import gzip
import json
import os
import time
from collections import Counter
from collections.abc import Callable
from typing import Any, Final

import boto3
from adapters.dynamodb import DynamoDbStore
from adapters.secrets import SecretsManagerReader
from domain.event import LINE_TOKEN
from domain.forwarding import Accepted, Decision, Rejected, decide

EVENTS_TABLE_ENV: Final = "EVENTS_TABLE_NAME"
STACK_KEY_SECRET_ENV: Final = "STACK_KEY_SECRET_ID"
#: Every environment variable this handler reads; `ForwarderFunction` in
#: `infra/events-webhooks.yaml` must declare each (pinned by
#: `tests/test_template_env.py`).
REQUIRED_ENV: Final = (EVENTS_TABLE_ENV, STACK_KEY_SECRET_ENV)

#: Closed reason for a line `decide` failed on unexpectedly (a bug, never
#: the line's content): counted like any other rejection.
REASON_INTERNAL_ERROR: Final = "internal_error"
_MILLIS_PER_SECOND: Final = 1000

_store: DynamoDbStore | None = None
_secrets: SecretsManagerReader | None = None
#: The forwarder's own wall clock (seconds), the reference for an event's
#: freshness; replaced in tests.
_clock: Callable[[], float] = time.time


class ForwarderWriteFailed(RuntimeError):
    """At least one verified line could not be written; raised only after
    every line of the batch was processed, so the asynchronous retry (and,
    after it, the OnFailure queue) gets the batch without losing the lines
    that were written."""


def _store_singleton() -> DynamoDbStore:
    global _store
    if _store is None:
        table = boto3.resource("dynamodb").Table(os.environ[EVENTS_TABLE_ENV])
        _store = DynamoDbStore(table)
    return _store


def _secrets_singleton() -> SecretsManagerReader:
    global _secrets
    if _secrets is None:
        _secrets = SecretsManagerReader(boto3.client("secretsmanager"))
    return _secrets


def handler(event: dict[str, Any], _context: object) -> dict[str, int]:
    record = json.loads(gzip.decompress(base64.b64decode(event["awslogs"]["data"])))
    log_stream = record.get("logStream", "")
    stack_key = _secrets_singleton().read(os.environ[STACK_KEY_SECRET_ENV])
    store = _store_singleton()
    now_ms = int(_clock() * _MILLIS_PER_SECOND)
    accepted = 0
    failed_writes = 0
    rejected: Counter[str] = Counter()
    for log_event in record.get("logEvents", []):
        message = log_event.get("message", "")
        if not isinstance(message, str) or not message.startswith(LINE_TOKEN):
            continue  # ordinary agent log line, not an event line
        decision = _decide_line(log_stream, message, stack_key, now_ms)
        if not isinstance(decision, Accepted):
            rejected[decision.reason] += 1
            continue
        try:
            # A duplicate line (CloudWatch Logs redelivery) changes nothing:
            # only a newly written event may move the sandbox's state.
            if store.put_event_if_absent(decision.event):
                store.record_sandbox_state(decision.event)
        except Exception:
            failed_writes += 1
            continue
        accepted += 1
    _log_summary(accepted, rejected, failed_writes)
    if failed_writes:
        raise ForwarderWriteFailed(f"{failed_writes} escrituras fallidas")
    return {"accepted": accepted, "rejected": rejected.total()}


def _decide_line(log_stream: str, message: str, stack_key: bytes, now_ms: int) -> Decision:
    """`decide` for one line, with the guarantee that a bug in it costs
    that line only, never the batch."""
    try:
        return decide(log_stream=log_stream, message=message, stack_key=stack_key, now_ms=now_ms)
    except Exception:
        return Rejected(REASON_INTERNAL_ERROR)


def _log_summary(accepted: int, rejected: Counter[str], failed_writes: int) -> None:
    """One structured line per invocation: the counts and the closed
    `REASON_*` strings only — never the line, the sandbox id or the MAC.
    The return value of a CloudWatch Logs-invoked Lambda is not logged
    anywhere, so without this a forged or malformed line is invisible
    (AWS_API_NOTES.md §25, Q107)."""
    print(
        json.dumps(
            {
                "forwarded": accepted,
                "failed_writes": failed_writes,
                "rejected": rejected.total(),
                "rejected_by_reason": dict(rejected),
            },
            sort_keys=True,
        )
    )
