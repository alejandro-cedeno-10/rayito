"""`handlers.forwarder.handler` with fake ports, in exactly the environment
`ForwarderFunction` declares in `infra/events-webhooks.yaml`: a verified
line is admitted (its sandbox's events only move forward, and a
suspend/resume loop is rate limited) and then stored once; a duplicate or
late line after `killed` never reopens the sandbox. One bad
line never costs the batch: a poison line is counted and the genuine lines
around it are still stored, and a failed table write only fails the
invocation after every other line was written (so the asynchronous retry
and its OnFailure queue get it).
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
from domain.admission import RATE_BURST_EVENTS, REASON_RATE_LIMITED
from domain.mac import derive_sandbox_key
from handlers import forwarder

STACK_KEY = b"stack-wide-secret"
SANDBOX_ID = "sbx-0000000000000001"
LOG_STREAM = f"2026/10/02[1]{SANDBOX_ID}"
NOW_MS = 1_790_000_000_000
IMAGE_ARN = "arn:aws:lambda:us-east-1:123456789012:microvm-image:rayito-base"


def _event_id(label: str) -> str:
    """A `rayd`-shaped id (32 lowercase hex), stable per label."""
    return hashlib.sha256(label.encode()).hexdigest()[:32]


class _FakeSecretsClient:
    def __init__(self) -> None:
        self.requested_secret_ids: list[str] = []

    def get_secret_value(self, *, SecretId: str) -> dict[str, str]:
        self.requested_secret_ids.append(SecretId)
        return {"SecretString": STACK_KEY.decode("utf-8")}


def _line(*, event_id: str, kind: str, occurred_at_ms: int, generation: int = 0) -> str:
    event: dict[str, Any] = {
        "event_id": _event_id(event_id),
        "sandbox_id": SANDBOX_ID,
        "kind": kind,
        "generation": generation,
        "occurred_at_ms": NOW_MS + occurred_at_ms,
        "image_arn": IMAGE_ARN,
        "image_version": "1",
    }
    if kind == "killed":
        event["kill_reason"] = "request"
    return _signed(json.dumps(event).encode("utf-8"))


def _signed(payload: bytes) -> str:
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
    monkeypatch.setattr(forwarder, "_clock", lambda: NOW_MS / 1000)
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
    assert secrets.requested_secret_ids == [
        template_environment("ForwarderFunction")["STACK_KEY_SECRET_ID"]
    ]
    assert table.items[(f"STATE#{SANDBOX_ID}", "STATE")]["last_kind"] == "created"


def test_a_line_after_killed_never_reopens_the_sandbox(
    wired: tuple[FakeTable, _FakeSecretsClient],
) -> None:
    table, _secrets = wired
    forwarder.handler(
        _subscription_event(
            _line(event_id="evt-1", kind="created", occurred_at_ms=1),
            _line(event_id="evt-2", kind="killed", occurred_at_ms=5),
            _line(event_id="evt-3", kind="resumed", occurred_at_ms=9, generation=1),
        ),
        None,
    )
    assert table.items[(f"STATE#{SANDBOX_ID}", "STATE")]["last_kind"] == "killed"
    assert (f"EVENT#{SANDBOX_ID}", f"{NOW_MS + 9:020d}#{_event_id('evt-3')}") not in table.items


def test_a_duplicate_line_does_not_move_the_state(
    wired: tuple[FakeTable, _FakeSecretsClient],
) -> None:
    table, _secrets = wired
    paused = _line(event_id="evt-2", kind="paused", occurred_at_ms=5)
    forwarder.handler(_subscription_event(paused), None)
    forwarder.handler(
        _subscription_event(
            _line(event_id="evt-3", kind="resumed", occurred_at_ms=9, generation=1)
        ),
        None,
    )
    forwarder.handler(_subscription_event(paused), None)  # CloudWatch Logs redelivery
    assert table.items[(f"STATE#{SANDBOX_ID}", "STATE")]["last_kind"] == "resumed"


def test_logs_one_summary_line_with_counts_and_reasons_only(
    wired: tuple[FakeTable, _FakeSecretsClient], capsys: pytest.CaptureFixture[str]
) -> None:
    good = _line(event_id="evt-1", kind="created", occurred_at_ms=1)
    forged = good.rsplit(" ", 1)[0] + " " + _b64(b"\x00" * 32)
    result = forwarder.handler(_subscription_event(good, forged, "rayito.event.v1 garbage"), None)

    assert result == {"accepted": 1, "rejected": 2}
    (line,) = capsys.readouterr().out.splitlines()
    assert json.loads(line) == {
        "forwarded": 1,
        "failed_writes": 0,
        "rejected": 2,
        "rejected_by_reason": {"mac_invalid": 1, "malformed_line": 1},
    }
    assert SANDBOX_ID not in line


def test_a_poison_line_never_drops_the_genuine_lines_around_it(
    wired: tuple[FakeTable, _FakeSecretsClient],
) -> None:
    table, _secrets = wired
    poison = [
        f"rayito.event.v1 {_b64(payload)} {_b64(bytes(32))}"
        for payload in (b"[]", b'"x"', b'{"generation": 1e400}', b"[" * 100_000)
    ]
    signed_poison = [_signed(b"[]"), _signed(b"[" * 100_000)]
    result = forwarder.handler(
        _subscription_event(
            _line(event_id="evt-1", kind="created", occurred_at_ms=1),
            *poison,
            *signed_poison,
            _line(event_id="evt-2", kind="paused", occurred_at_ms=2),
        ),
        None,
    )
    assert result == {"accepted": 2, "rejected": len(poison) + len(signed_poison)}
    assert table.items[(f"STATE#{SANDBOX_ID}", "STATE")]["last_kind"] == "paused"


def test_an_unexpected_error_in_one_decision_costs_only_that_line(
    wired: tuple[FakeTable, _FakeSecretsClient],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    table, _secrets = wired
    real_decide = forwarder.decide
    broken = _line(event_id="evt-1", kind="created", occurred_at_ms=1)

    def flaky_decide(**kwargs: Any) -> Any:
        if kwargs["message"] == broken:
            raise RuntimeError("bug")
        return real_decide(**kwargs)

    monkeypatch.setattr(forwarder, "decide", flaky_decide)
    result = forwarder.handler(
        _subscription_event(broken, _line(event_id="evt-2", kind="paused", occurred_at_ms=2)),
        None,
    )
    assert result == {"accepted": 1, "rejected": 1}
    summary = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert summary["rejected_by_reason"] == {forwarder.REASON_INTERNAL_ERROR: 1}
    assert table.items[(f"STATE#{SANDBOX_ID}", "STATE")]["last_kind"] == "paused"


def test_a_failed_write_fails_the_invocation_only_after_the_other_lines(
    wired: tuple[FakeTable, _FakeSecretsClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    table, _secrets = wired
    real_put = table.put_item
    failing_id = _event_id("evt-1")

    def put_item(**kwargs: Any) -> None:
        if kwargs["Item"].get("event_id") == failing_id:
            raise RuntimeError("DynamoDB unavailable")
        real_put(**kwargs)

    monkeypatch.setattr(table, "put_item", put_item)
    with pytest.raises(forwarder.ForwarderWriteFailed):
        forwarder.handler(
            _subscription_event(
                _line(event_id="evt-1", kind="created", occurred_at_ms=1),
                _line(event_id="evt-2", kind="paused", occurred_at_ms=2),
            ),
            None,
        )
    assert table.items[(f"STATE#{SANDBOX_ID}", "STATE")]["last_kind"] == "paused"


def test_a_stale_line_is_counted_not_stored(
    wired: tuple[FakeTable, _FakeSecretsClient],
) -> None:
    table, _secrets = wired
    week_ms = 7 * 24 * 3600 * 1000
    result = forwarder.handler(
        _subscription_event(_line(event_id="evt-1", kind="created", occurred_at_ms=-week_ms)),
        None,
    )
    assert result == {"accepted": 0, "rejected": 1}
    assert table.items == {}


def test_a_forged_suspend_resume_loop_is_rate_limited_not_delivered(
    wired: tuple[FakeTable, _FakeSecretsClient], capsys: pytest.CaptureFixture[str]
) -> None:
    # Guest code can drive `rayd`'s `/suspend` and `/resume` hooks over
    # loopback: every resulting line carries a valid MAC. Only the burst
    # reaches the table (and therefore the deliverer); the rest is counted.
    table, _secrets = wired
    loop = [_line(event_id="evt-0", kind="created", occurred_at_ms=0)]
    for generation in range(RATE_BURST_EVENTS):
        loop.append(
            _line(event_id=f"p{generation}", kind="paused", occurred_at_ms=1, generation=generation)
        )
        loop.append(
            _line(
                event_id=f"r{generation}",
                kind="resumed",
                occurred_at_ms=1,
                generation=generation + 1,
            )
        )
    result = forwarder.handler(_subscription_event(*loop), None)

    assert result == {"accepted": 1 + RATE_BURST_EVENTS, "rejected": RATE_BURST_EVENTS}
    stored = [key for key in table.items if key[0] == f"EVENT#{SANDBOX_ID}"]
    assert len(stored) == 1 + RATE_BURST_EVENTS
    summary = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert summary["rejected_by_reason"] == {REASON_RATE_LIMITED: RATE_BURST_EVENTS}


def test_a_retry_after_a_failed_event_write_stores_the_event(
    wired: tuple[FakeTable, _FakeSecretsClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The state moves before the event row is written: the asynchronous
    # retry of the same line must still store it, not refuse it as a
    # repeated position.
    table, _secrets = wired
    created = _line(event_id="evt-1", kind="created", occurred_at_ms=1)
    real_put = table.put_item

    def failing_event_put(**kwargs: Any) -> None:
        if kwargs["Item"]["pk"].startswith("EVENT#"):
            raise RuntimeError("DynamoDB unavailable")
        real_put(**kwargs)

    monkeypatch.setattr(table, "put_item", failing_event_put)
    with pytest.raises(forwarder.ForwarderWriteFailed):
        forwarder.handler(_subscription_event(created), None)
    monkeypatch.setattr(table, "put_item", real_put)
    assert forwarder.handler(_subscription_event(created), None) == {"accepted": 1, "rejected": 0}
    assert any(pk == f"EVENT#{SANDBOX_ID}" for pk, _sk in table.items)
