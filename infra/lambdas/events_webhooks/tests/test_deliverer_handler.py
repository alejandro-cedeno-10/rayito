"""`handlers.deliverer.handler` with fake ports, in exactly the environment
`DelivererFunction` declares in `infra/events-webhooks.yaml` (regression for
the `KeyError` on `STACK_KEY_SECRET_ID` that failed every batch): claim /
finish never loses a delivery, only 5xx and transport errors are retried,
and the invocation's remaining time bounds every attempt and backoff.
One webhook never blocks the others: a URL the sender cannot parse is a
permanent failure of that webhook, and any unexpected error is contained to
the webhook it happened on.
"""

from __future__ import annotations

import http.client
from typing import Any

import pytest
from botocore.exceptions import ClientError
from conftest import FakeContext, FakeDynamoResource, FakeTable, template_environment
from handlers import deliverer

#: Comfortably more than three attempts and their backoff need.
_PLENTY_OF_TIME_MS = 60_000


class _FakeSecretsClient:
    def __init__(self, *, missing: bool = False) -> None:
        self.requested_secret_ids: list[str] = []
        self._missing = missing

    def get_secret_value(self, *, SecretId: str) -> dict[str, str]:
        self.requested_secret_ids.append(SecretId)
        if self._missing:
            error = {"Error": {"Code": "ResourceNotFoundException", "Message": "not found"}}
            raise ClientError(error, "GetSecretValue")  # type: ignore[arg-type]
        return {"SecretString": "webhook-secret-value"}


class _FakeSender:
    """Answers each `post` with the next of `outcomes` (an int status or an
    exception to raise), repeating the last one; `context` (when given)
    is charged `seconds_per_post` per call."""

    def __init__(
        self,
        *outcomes: int | Exception,
        context: FakeContext | None = None,
        seconds_per_post: float = 0.0,
    ) -> None:
        self._outcomes = list(outcomes) or [200]
        self._context = context
        self._seconds_per_post = seconds_per_post
        self.posts: list[tuple[str, float]] = []

    def post(self, url: str, headers: dict[str, str], body: bytes, *, timeout: float) -> int:
        del headers, body
        self.posts.append((url, timeout))
        if self._context is not None:
            self._context.spend(self._seconds_per_post)
        outcome = self._outcomes[min(len(self.posts), len(self._outcomes)) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _webhook_item(webhook_id: str = "wh-1") -> dict[str, Any]:
    return {
        "pk": "WEBHOOK",
        "sk": webhook_id,
        "url": f"https://example.test/{webhook_id}",
        "secret_name": f"rayito/webhooks/{webhook_id}",
        "types": ["sandbox.lifecycle.killed"],
    }


def _record(event_id: str = "evt-1", sequence_number: str = "100") -> dict[str, Any]:
    return {
        "eventName": "INSERT",
        "dynamodb": {
            "SequenceNumber": sequence_number,
            "NewImage": {
                "pk": {"S": "EVENT#sbx-1"},
                "event_id": {"S": event_id},
                "sandbox_id": {"S": "sbx-1"},
                "kind": {"S": "killed"},
                "kill_reason": {"S": "request"},
                "generation": {"N": "0"},
                "occurred_at_ms": {"N": "1"},
                "image_arn": {"S": "arn:test"},
                "image_version": {"S": "1"},
            },
        },
    }


@pytest.fixture
def table(monkeypatch: pytest.MonkeyPatch) -> FakeTable:
    """Wires the handler to a fresh fake table in the template's own
    environment (and nothing else: no `STACK_KEY_SECRET_ID`)."""
    for name in ("EVENTS_TABLE_NAME", "STACK_KEY_SECRET_ID"):
        monkeypatch.delenv(name, raising=False)
    for name, value in template_environment("DelivererFunction").items():
        monkeypatch.setenv(name, value)
    fake_table = FakeTable()
    fake_table.put_item(Item=_webhook_item())
    monkeypatch.setattr(deliverer, "_store", None)
    monkeypatch.setattr(deliverer, "_secrets", None)
    monkeypatch.setattr(deliverer.boto3, "resource", lambda _name: FakeDynamoResource(fake_table))
    monkeypatch.setattr(deliverer.time, "sleep", lambda _seconds: None)
    return fake_table


def _use(monkeypatch: pytest.MonkeyPatch, sender: _FakeSender, secrets: _FakeSecretsClient) -> None:
    monkeypatch.setattr(deliverer, "_sender", sender)
    monkeypatch.setattr(deliverer.boto3, "client", lambda _name: secrets)


def _status(table: FakeTable, event_id: str = "evt-1", webhook_id: str = "wh-1") -> str:
    return str(table.items[(f"DELIVERY#sbx-1#{event_id}", webhook_id)]["delivery_status"])


def test_delivers_with_only_the_template_environment(
    table: FakeTable, monkeypatch: pytest.MonkeyPatch
) -> None:
    sender, secrets = _FakeSender(200), _FakeSecretsClient()
    _use(monkeypatch, sender, secrets)

    result = deliverer.handler({"Records": [_record()]}, FakeContext(_PLENTY_OF_TIME_MS))

    assert result == {"batchItemFailures": []}
    assert [url for url, _ in sender.posts] == ["https://example.test/wh-1"]
    assert secrets.requested_secret_ids == ["rayito/webhooks/wh-1"]
    assert _status(table) == "delivered"


def test_a_delivered_pair_is_skipped_on_redelivery(
    table: FakeTable, monkeypatch: pytest.MonkeyPatch
) -> None:
    sender = _FakeSender(200)
    _use(monkeypatch, sender, _FakeSecretsClient())
    event = {"Records": [_record()]}
    deliverer.handler(event, FakeContext(_PLENTY_OF_TIME_MS))
    deliverer.handler(event, FakeContext(_PLENTY_OF_TIME_MS))
    assert len(sender.posts) == 1


def test_a_failed_delivery_is_attempted_again_by_a_redrive(
    table: FakeTable, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use(monkeypatch, _FakeSender(500), _FakeSecretsClient())
    event = {"Records": [_record()]}
    deliverer.handler(event, FakeContext(_PLENTY_OF_TIME_MS))
    assert _status(table) == "failed"

    _use(monkeypatch, _FakeSender(200), _FakeSecretsClient())
    deliverer.handler(event, FakeContext(_PLENTY_OF_TIME_MS))
    assert _status(table) == "delivered"


def test_only_5xx_and_transport_errors_are_retried(
    table: FakeTable, monkeypatch: pytest.MonkeyPatch
) -> None:
    retried = _FakeSender(503, http.client.BadStatusLine("garbage"), 200)
    _use(monkeypatch, retried, _FakeSecretsClient())
    deliverer.handler({"Records": [_record("evt-1")]}, FakeContext(_PLENTY_OF_TIME_MS))
    assert len(retried.posts) == 3
    assert _status(table, "evt-1") == "delivered"

    rejected = _FakeSender(404)
    _use(monkeypatch, rejected, _FakeSecretsClient())
    deliverer.handler({"Records": [_record("evt-2")]}, FakeContext(_PLENTY_OF_TIME_MS))
    assert len(rejected.posts) == 1, "a 4xx is the receiver's answer, not a transient failure"
    assert _status(table, "evt-2") == "failed"


def test_a_missing_webhook_secret_fails_that_webhook_only(
    table: FakeTable, monkeypatch: pytest.MonkeyPatch
) -> None:
    table.put_item(Item=_webhook_item("wh-2"))
    sender = _FakeSender(200)
    _use(monkeypatch, sender, _FakeSecretsClient(missing=True))

    result = deliverer.handler({"Records": [_record()]}, FakeContext(_PLENTY_OF_TIME_MS))

    assert result == {"batchItemFailures": []}
    assert sender.posts == []
    assert _status(table, webhook_id="wh-1") == "failed"
    assert _status(table, webhook_id="wh-2") == "failed"


def test_each_attempt_timeout_is_bounded_by_the_remaining_time(
    table: FakeTable, monkeypatch: pytest.MonkeyPatch
) -> None:
    sender = _FakeSender(200)
    _use(monkeypatch, sender, _FakeSecretsClient())
    remaining_ms = 5_000
    deliverer.handler({"Records": [_record()]}, FakeContext(remaining_ms))
    ((_url, timeout),) = sender.posts
    assert timeout == remaining_ms / 1000 - deliverer.SAFETY_MARGIN_SECONDS


def test_running_out_of_time_reports_the_unfinished_record_and_keeps_it_retryable(
    table: FakeTable, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Each post burns a full attempt timeout: the first record exhausts the
    # budget mid-retry, so it (not the second) is the batch item failure,
    # and its pair is left `failed`, never `delivered`.
    context = FakeContext(int((deliverer.ATTEMPT_TIMEOUT_SECONDS + 4) * 1000))
    sender = _FakeSender(500, context=context, seconds_per_post=deliverer.ATTEMPT_TIMEOUT_SECONDS)
    _use(monkeypatch, sender, _FakeSecretsClient())
    event = {"Records": [_record("evt-1", "100"), _record("evt-2", "200")]}

    result = deliverer.handler(event, context)

    assert result == {"batchItemFailures": [{"itemIdentifier": "100"}]}
    assert _status(table, "evt-1") == "failed"
    assert ("DELIVERY#sbx-1#evt-2", "wh-1") not in table.items


def _two_webhooks(table: FakeTable, first_url: str) -> None:
    """`wh-0` (iterated first, `first_url`) and `wh-1` (a good URL)."""
    item = _webhook_item("wh-0")
    item["url"] = first_url
    table.put_item(Item=item)


class _RealUrlParsingSender(_FakeSender):
    """Fails exactly like `HttpsOnlySender` on an unparsable URL, then
    answers like `_FakeSender` for a well-formed one."""

    def post(self, url: str, headers: dict[str, str], body: bytes, *, timeout: float) -> int:
        from urllib.parse import urlsplit

        parts = urlsplit(url)
        _port = parts.port
        (parts.hostname or "").encode("idna")
        return super().post(url, headers, body, timeout=timeout)


@pytest.mark.parametrize(
    "bad_url",
    [
        "https://h:99999/",
        "https://h:abc/",
        "https://[::1/",
        "https://" + "a" * 64 + ".example/",
    ],
)
def test_an_unparsable_webhook_url_fails_only_that_webhook(
    table: FakeTable, monkeypatch: pytest.MonkeyPatch, bad_url: str
) -> None:
    _two_webhooks(table, bad_url)
    sender = _RealUrlParsingSender(200)
    _use(monkeypatch, sender, _FakeSecretsClient())

    result = deliverer.handler({"Records": [_record()]}, FakeContext(_PLENTY_OF_TIME_MS))

    assert result == {"batchItemFailures": []}
    assert _status(table, webhook_id="wh-0") == "failed"
    assert _status(table, webhook_id="wh-1") == "delivered"
    assert sum(1 for url, _ in sender.posts if url == bad_url) <= 1, "never retried"


def test_an_unexpected_error_on_one_webhook_does_not_fail_the_batch(
    table: FakeTable, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _two_webhooks(table, "https://example.test/wh-0")

    class _BrokenForOne(_FakeSender):
        def post(self, url: str, headers: dict[str, str], body: bytes, *, timeout: float) -> int:
            if url.endswith("/wh-0"):
                raise RuntimeError("bug")
            return super().post(url, headers, body, timeout=timeout)

    _use(monkeypatch, _BrokenForOne(200), _FakeSecretsClient())
    result = deliverer.handler({"Records": [_record()]}, FakeContext(_PLENTY_OF_TIME_MS))

    assert result == {"batchItemFailures": []}
    assert _status(table, webhook_id="wh-0") == "failed"
    assert _status(table, webhook_id="wh-1") == "delivered"
    assert '"delivery_failure_reason": "internal_error"' in capsys.readouterr().out
