"""The forwarder's accept/reject decision (T22): the MAC is verified with
the key of the sandbox the log stream names **before** the payload is
parsed, the payload must then be exactly what `rayd` emits, for that same
sandbox, and recent enough. `decide` never raises, whatever the line."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any

import pytest
from domain import schema
from domain.forwarding import (
    MAX_CLOCK_SKEW_MS,
    MAX_EVENT_AGE_MS,
    REASON_MAC_INVALID,
    REASON_MALFORMED,
    REASON_SANDBOX_MISMATCH,
    REASON_STALE,
    Accepted,
    Rejected,
    decide,
    stream_sandbox_id,
)
from domain.mac import derive_sandbox_key

STACK_KEY = b"stack-wide-secret"
SANDBOX_ID = "sbx-0000000000000001"
OTHER_SANDBOX_ID = "sbx-0000000000000002"
#: The measured stream shape, `YYYY/MM/DD[<imageVersion>]<microvmId>` (Q106).
OWN_STREAM = f"2026/10/02[1.0]{SANDBOX_ID}"
NOW_MS = 1_790_000_000_000
EVENT_ID = "0123456789abcdef0123456789abcdef"
IMAGE_ARN = "arn:aws:lambda:us-east-1:123456789012:microvm-image:rayito-base"
_DROP = object()


def _event(**overrides: Any) -> dict[str, Any]:
    event: dict[str, Any] = {
        "event_id": EVENT_ID,
        "sandbox_id": SANDBOX_ID,
        "kind": "created",
        "generation": 0,
        "occurred_at_ms": NOW_MS,
        "image_arn": IMAGE_ARN,
        "image_version": "1",
    }
    event.update(overrides)
    return {key: value for key, value in event.items() if value is not _DROP}


def _signed(payload: bytes, *, sandbox_id: str = SANDBOX_ID, key: bytes | None = None) -> str:
    sandbox_key = key if key is not None else derive_sandbox_key(STACK_KEY, sandbox_id)
    mac = hmac.new(sandbox_key, payload, hashlib.sha256).digest()
    return f"rayito.event.v1 {_b64(payload)} {_b64(mac)}"


def _line(*, sandbox_id: str = SANDBOX_ID, key: bytes | None = None, **overrides: Any) -> str:
    payload = json.dumps(_event(sandbox_id=sandbox_id, **overrides)).encode("utf-8")
    return _signed(payload, sandbox_id=sandbox_id, key=key)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _decide(message: str, log_stream: str = OWN_STREAM, now_ms: int = NOW_MS) -> Any:
    return decide(log_stream=log_stream, message=message, stack_key=STACK_KEY, now_ms=now_ms)


def _reason(message: str, log_stream: str = OWN_STREAM, now_ms: int = NOW_MS) -> str:
    decision = _decide(message, log_stream, now_ms)
    assert isinstance(decision, Rejected), decision
    return decision.reason


def test_a_valid_line_from_its_own_log_stream_is_accepted() -> None:
    decision = _decide(_line())
    assert isinstance(decision, Accepted)
    assert decision.event.sandbox_id == SANDBOX_ID


def test_a_killed_line_carries_the_request_reason() -> None:
    assert isinstance(_decide(_line(kind="killed", kill_reason="request")), Accepted)


def test_the_stream_names_the_key_so_another_sandboxs_stream_fails_the_mac() -> None:
    # A sandbox signing with its own k_sbx and writing into another
    # sandbox's stream: the forwarder derives the victim's key, never the
    # one the line claims.
    other_stream = f"2026/10/02[1.0]{OTHER_SANDBOX_ID}"
    assert _reason(_line(), other_stream) == REASON_MAC_INVALID


def test_a_payload_naming_another_sandbox_than_its_stream_is_rejected() -> None:
    payload = json.dumps(_event(sandbox_id=OTHER_SANDBOX_ID)).encode("utf-8")
    assert _reason(_signed(payload, sandbox_id=SANDBOX_ID)) == REASON_SANDBOX_MISMATCH


def test_the_sandbox_id_is_everything_after_the_last_bracket() -> None:
    assert stream_sandbox_id(OWN_STREAM) == SANDBOX_ID
    assert stream_sandbox_id(f"2026/10/02[{SANDBOX_ID}]other") == "other"
    for log_stream in ("2026/10/02[1.0]", "no-separator", ""):
        assert stream_sandbox_id(log_stream) is None
        assert _reason(_line(), log_stream) == REASON_SANDBOX_MISMATCH


def test_a_line_with_a_forged_mac_is_rejected() -> None:
    forged_key = derive_sandbox_key(b"a-different-stack-key", SANDBOX_ID)
    assert _reason(_line(key=forged_key)) == REASON_MAC_INVALID


def test_a_malformed_line_is_rejected() -> None:
    assert _reason("garbage") == REASON_MALFORMED


ZERO_MAC = _b64(b"\x00" * 32)

#: Payloads that made `decide` raise before the MAC was checked first
#: (TypeError, OverflowError, RecursionError): unsigned, they must now fail
#: on the MAC without ever being parsed.
POISON_PAYLOADS = (
    b"[]",
    b'"x"',
    json.dumps(_event(sandbox_id=5)).encode(),
    json.dumps(_event(generation={})).encode(),
    b'{"generation": 1e400}',
    b"[" * 100_000,
)


@pytest.mark.parametrize("payload", POISON_PAYLOADS)
def test_an_unauthenticated_poison_payload_fails_the_mac_and_never_raises(payload: bytes) -> None:
    unsigned = f"rayito.event.v1 {_b64(payload)} {ZERO_MAC}"
    assert _reason(unsigned) == REASON_MAC_INVALID


@pytest.mark.parametrize("payload", POISON_PAYLOADS)
def test_a_signed_poison_payload_is_malformed_and_never_raises(payload: bytes) -> None:
    assert _reason(_signed(payload)) in {REASON_MALFORMED, REASON_SANDBOX_MISMATCH}


@pytest.mark.parametrize(
    "overrides",
    [
        {"generation": 1.0},
        {"generation": True},
        {"generation": "1"},
        {"generation": -1},
        {"generation": 2**64},
        {"occurred_at_ms": float(NOW_MS)},
        {"event_id": 7},
        {"kind": None},
        {"kind": "rebooted"},
        {"image_version": ["1"]},
        {"image_arn": _DROP},
        {"image_arn": "arn:aws:lambda:us-east-1:123456789012:microvm-image:" + "x" * 600},
    ],
)
def test_fields_must_have_the_exact_wire_type(overrides: dict[str, Any]) -> None:
    assert _reason(_line(**overrides)) == REASON_MALFORMED


@pytest.mark.parametrize(
    "overrides",
    [
        {"event_id": "synthetic-" + "0" * 32},
        {"event_id": EVENT_ID.upper()},
        {"event_id": "evt-1"},
        {"kind": "killed", "kill_reason": "timeout"},
        {"kind": "killed", "kill_reason": "unknown"},
        {"kind": "killed"},
        {"kind": "created", "kill_reason": "request"},
        {"image_arn": "attacker-controlled text"},
        {"image_arn": "arn:aws:lambda:us-east-1:123456789012:function:rayito-base"},
    ],
)
def test_only_what_rayd_itself_emits_is_accepted(overrides: dict[str, Any]) -> None:
    assert _reason(_line(**overrides)) == REASON_MALFORMED


def test_an_event_older_than_the_window_is_stale() -> None:
    assert isinstance(_decide(_line(occurred_at_ms=NOW_MS - MAX_EVENT_AGE_MS)), Accepted)
    assert _reason(_line(occurred_at_ms=NOW_MS - MAX_EVENT_AGE_MS - 1)) == REASON_STALE


def test_an_event_from_too_far_in_the_future_is_stale() -> None:
    assert isinstance(_decide(_line(occurred_at_ms=NOW_MS + MAX_CLOCK_SKEW_MS)), Accepted)
    assert _reason(_line(occurred_at_ms=NOW_MS + MAX_CLOCK_SKEW_MS + 1)) == REASON_STALE


def test_a_replay_after_the_dedupe_ttl_is_refused_as_stale() -> None:
    line = _line()
    replayed_at = NOW_MS + schema.EVENT_TTL_SECONDS * 1000 + 1
    assert _reason(line, now_ms=replayed_at) == REASON_STALE


def test_the_freshness_window_is_well_inside_the_dedupe_ttl() -> None:
    assert MAX_EVENT_AGE_MS * 2 <= schema.EVENT_TTL_SECONDS * 1000
    assert MAX_EVENT_AGE_MS == 24 * 60 * 60 * 1000
    assert MAX_CLOCK_SKEW_MS == 5 * 60 * 1000
