"""The forwarder's accept/reject decision: MAC verification plus the
sandbox-id/log-stream cross-check (T22)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json

from domain.forwarding import (
    REASON_MAC_INVALID,
    REASON_MALFORMED,
    REASON_SANDBOX_MISMATCH,
    Accepted,
    Rejected,
    decide,
)
from domain.mac import derive_sandbox_key


def _compute_mac(key: bytes, payload: bytes) -> bytes:
    return hmac.new(key, payload, hashlib.sha256).digest()


STACK_KEY = b"stack-wide-secret"
SANDBOX_ID = "sbx-0000000000000001"


def _line(sandbox_id: str = SANDBOX_ID, *, key: bytes | None = None) -> str:
    payload = json.dumps(
        {
            "event_id": "evt-1",
            "sandbox_id": sandbox_id,
            "kind": "created",
            "generation": 0,
            "occurred_at_ms": 1,
            "image_arn": "arn:test",
            "image_version": "1",
        }
    ).encode("utf-8")
    sandbox_key = key if key is not None else derive_sandbox_key(STACK_KEY, sandbox_id)
    mac = _compute_mac(sandbox_key, payload)
    return f"rayito.event.v1 {_b64(payload)} {_b64(mac)}"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def test_a_valid_line_from_its_own_log_stream_is_accepted() -> None:
    decision = decide(
        log_stream=f"some-prefix/{SANDBOX_ID}/stream", message=_line(), stack_key=STACK_KEY
    )
    assert isinstance(decision, Accepted)
    assert decision.event.sandbox_id == SANDBOX_ID


def test_a_line_whose_sandbox_id_does_not_match_the_log_stream_is_rejected() -> None:
    decision = decide(
        log_stream="some-prefix/a-different-sandbox/stream", message=_line(), stack_key=STACK_KEY
    )
    assert isinstance(decision, Rejected)
    assert decision.reason == REASON_SANDBOX_MISMATCH


def test_a_line_with_a_forged_mac_is_rejected() -> None:
    forged_key = derive_sandbox_key(b"a-different-stack-key", SANDBOX_ID)
    decision = decide(
        log_stream=f"/{SANDBOX_ID}/stream", message=_line(key=forged_key), stack_key=STACK_KEY
    )
    assert isinstance(decision, Rejected)
    assert decision.reason == REASON_MAC_INVALID


def test_an_empty_sandbox_id_is_rejected_even_though_it_is_a_substring_of_anything() -> None:
    # `"" in log_stream` is `True` for every `log_stream`, including an
    # empty one — without an explicit check this would "match" anything.
    decision = decide(log_stream="any-log-stream-at-all", message=_line(sandbox_id=""), stack_key=STACK_KEY)
    assert isinstance(decision, Rejected)
    assert decision.reason == REASON_SANDBOX_MISMATCH


def test_a_malformed_line_is_rejected() -> None:
    decision = decide(log_stream=f"/{SANDBOX_ID}/", message="garbage", stack_key=STACK_KEY)
    assert isinstance(decision, Rejected)
    assert decision.reason == REASON_MALFORMED
