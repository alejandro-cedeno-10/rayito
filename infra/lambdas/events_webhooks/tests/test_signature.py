"""E2B-compatible delivery signature: `e2b-signature` is base64 (no
padding) of `sha256(secret + payload)`, never an HMAC."""

from __future__ import annotations

import base64
import hashlib

from domain.signature import (
    HEADER_DELIVERY_ID,
    HEADER_SIGNATURE,
    HEADER_SIGNATURE_VERSION,
    HEADER_WEBHOOK_ID,
    sign_delivery,
)


def test_signature_matches_sha256_of_secret_plus_payload() -> None:
    secret = b"the-webhook-secret"
    payload = b'{"event_id":"evt-1"}'
    signed = sign_delivery(webhook_id="wh-1", secret=secret, payload=payload)
    expected_bytes = hashlib.sha256(secret + payload).digest()
    expected = base64.b64encode(expected_bytes).decode("ascii").rstrip("=")
    assert signed.headers[HEADER_SIGNATURE] == expected
    assert "=" not in signed.headers[HEADER_SIGNATURE]


def test_headers_carry_the_e2b_names() -> None:
    signed = sign_delivery(webhook_id="wh-1", secret=b"s", payload=b"{}")
    assert signed.headers[HEADER_WEBHOOK_ID] == "wh-1"
    assert signed.headers[HEADER_SIGNATURE_VERSION] == "v1"
    assert len(signed.headers[HEADER_DELIVERY_ID]) > 0
    assert signed.body == b"{}"


def test_two_attempts_get_different_delivery_ids() -> None:
    first = sign_delivery(webhook_id="wh-1", secret=b"s", payload=b"{}")
    second = sign_delivery(webhook_id="wh-1", secret=b"s", payload=b"{}")
    assert first.headers[HEADER_DELIVERY_ID] != second.headers[HEADER_DELIVERY_ID]
