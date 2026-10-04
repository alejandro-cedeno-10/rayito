"""Webhook delivery signatures (§7.4). Every request carries two:

- The E2B-compatible headers E2B's own webhook consumers already expect, so
  a user's existing E2B webhook handler works unchanged against Rayito.
  That scheme signs only the body, with no timestamp: a captured request
  verifies forever, and against any receiver sharing the secret.
- `rayito-signature: t=<unix seconds>,v1=<hex>`, an HMAC-SHA256 over
  `"<t>.<webhook_id>."` followed by the body. A receiver that checks it
  (and that `t` is recent) rejects a replay after its freshness window and
  a request replayed to another webhook. Unknown headers are ignored by
  E2B consumers, so sending it costs them nothing.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from typing import Final

SIGNATURE_VERSION: Final = "v1"

HEADER_WEBHOOK_ID: Final = "e2b-webhook-id"
HEADER_DELIVERY_ID: Final = "e2b-delivery-id"
HEADER_SIGNATURE_VERSION: Final = "e2b-signature-version"
HEADER_SIGNATURE: Final = "e2b-signature"
#: The timestamped HMAC (module doc); its format is pinned by the events
#: guide's receiver examples.
HEADER_RAYITO_SIGNATURE: Final = "rayito-signature"
RAYITO_SIGNATURE_VERSION: Final = "v1"


@dataclass(frozen=True)
class SignedRequest:
    headers: dict[str, str]
    body: bytes


def rayito_signature(*, webhook_id: str, secret: bytes, payload: bytes, timestamp: int) -> str:
    """`t=<timestamp>,v1=<hex HMAC-SHA256(secret, "<t>.<webhook_id>." + payload)>`."""
    signed = f"{timestamp}.{webhook_id}.".encode() + payload
    digest = hmac.new(secret, signed, hashlib.sha256).hexdigest()
    return f"t={timestamp},{RAYITO_SIGNATURE_VERSION}={digest}"


def sign_delivery(
    *, webhook_id: str, secret: bytes, payload: bytes, timestamp: int
) -> SignedRequest:
    """`e2b-signature` is base64 (no padding) of `sha256(secret + payload)`
    — not an HMAC: this is the exact scheme E2B's own webhooks use, kept
    byte for byte so an existing E2B webhook verifier accepts it unchanged.
    `delivery_id` is a fresh random id per attempt (including retries), so a
    receiver's own dedupe-by-delivery-id never collapses three independent
    delivery attempts into one. `timestamp` (Unix seconds, the deliverer's
    clock at this attempt) goes into `rayito-signature`."""
    digest = hashlib.sha256(secret + payload).digest()
    signature = base64.b64encode(digest).decode("ascii").rstrip("=")
    delivery_id = secrets.token_hex(16)
    return SignedRequest(
        headers={
            HEADER_WEBHOOK_ID: webhook_id,
            HEADER_DELIVERY_ID: delivery_id,
            HEADER_SIGNATURE_VERSION: SIGNATURE_VERSION,
            HEADER_SIGNATURE: signature,
            HEADER_RAYITO_SIGNATURE: rayito_signature(
                webhook_id=webhook_id, secret=secret, payload=payload, timestamp=timestamp
            ),
            "content-type": "application/json",
        },
        body=payload,
    )
