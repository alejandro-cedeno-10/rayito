"""Delivery signatures: `e2b-signature` is base64 (no padding) of
`sha256(secret + payload)`, byte-compatible with E2B; `rayito-signature`
is a timestamped HMAC bound to the webhook id."""

from __future__ import annotations

import base64
import hashlib
import hmac

from domain.signature import (
    HEADER_DELIVERY_ID,
    HEADER_RAYITO_SIGNATURE,
    HEADER_SIGNATURE,
    HEADER_SIGNATURE_VERSION,
    HEADER_WEBHOOK_ID,
    sign_delivery,
)

T = 1_790_000_000


def test_signature_matches_sha256_of_secret_plus_payload() -> None:
    secret = b"the-webhook-secret"
    payload = b'{"event_id":"evt-1"}'
    signed = sign_delivery(webhook_id="wh-1", secret=secret, payload=payload, timestamp=T)
    expected_bytes = hashlib.sha256(secret + payload).digest()
    expected = base64.b64encode(expected_bytes).decode("ascii").rstrip("=")
    assert signed.headers[HEADER_SIGNATURE] == expected
    assert "=" not in signed.headers[HEADER_SIGNATURE]


def test_headers_carry_the_e2b_names() -> None:
    signed = sign_delivery(webhook_id="wh-1", secret=b"s", payload=b"{}", timestamp=T)
    assert signed.headers[HEADER_WEBHOOK_ID] == "wh-1"
    assert signed.headers[HEADER_SIGNATURE_VERSION] == "v1"
    assert len(signed.headers[HEADER_DELIVERY_ID]) > 0
    assert signed.body == b"{}"


def test_two_attempts_get_different_delivery_ids() -> None:
    first = sign_delivery(webhook_id="wh-1", secret=b"s", payload=b"{}", timestamp=T)
    second = sign_delivery(webhook_id="wh-1", secret=b"s", payload=b"{}", timestamp=T)
    assert first.headers[HEADER_DELIVERY_ID] != second.headers[HEADER_DELIVERY_ID]


def test_rayito_signature_is_a_timestamped_hmac_bound_to_the_webhook() -> None:
    secret, payload = b"the-webhook-secret", b'{"event_id":"evt-1"}'
    signed = sign_delivery(webhook_id="wh-1", secret=secret, payload=payload, timestamp=T)
    expected = hmac.new(secret, f"{T}.wh-1.".encode() + payload, hashlib.sha256).hexdigest()
    assert signed.headers[HEADER_RAYITO_SIGNATURE] == f"t={T},v1={expected}"


def test_rayito_signature_changes_with_the_time_and_the_webhook() -> None:
    def header(webhook_id: str, timestamp: int) -> str:
        signed = sign_delivery(
            webhook_id=webhook_id, secret=b"s", payload=b"{}", timestamp=timestamp
        )
        return signed.headers[HEADER_RAYITO_SIGNATURE].split(",v1=")[1]

    assert header("wh-1", T) != header("wh-1", T + 1)
    assert header("wh-1", T) != header("wh-2", T)
