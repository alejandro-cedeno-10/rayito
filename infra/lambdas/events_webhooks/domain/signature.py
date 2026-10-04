"""E2B-compatible webhook delivery signature (§7.4): the deliverer signs
every request with the headers E2B's own webhook consumers already expect,
so a user's existing E2B webhook handler works unchanged against Rayito.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass
from typing import Final

SIGNATURE_VERSION: Final = "v1"

HEADER_WEBHOOK_ID: Final = "e2b-webhook-id"
HEADER_DELIVERY_ID: Final = "e2b-delivery-id"
HEADER_SIGNATURE_VERSION: Final = "e2b-signature-version"
HEADER_SIGNATURE: Final = "e2b-signature"


@dataclass(frozen=True)
class SignedRequest:
    headers: dict[str, str]
    body: bytes


def sign_delivery(*, webhook_id: str, secret: bytes, payload: bytes) -> SignedRequest:
    """`e2b-signature` is base64 (no padding) of `sha256(secret + payload)`
    — not an HMAC: this is the exact scheme E2B's own webhooks use, kept
    byte for byte so an existing E2B webhook verifier accepts it unchanged.
    `delivery_id` is a fresh random id per attempt (including retries), so a
    receiver's own dedupe-by-delivery-id never collapses three independent
    delivery attempts into one."""
    digest = hashlib.sha256(secret + payload).digest()
    signature = base64.b64encode(digest).decode("ascii").rstrip("=")
    delivery_id = secrets.token_hex(16)
    return SignedRequest(
        headers={
            HEADER_WEBHOOK_ID: webhook_id,
            HEADER_DELIVERY_ID: delivery_id,
            HEADER_SIGNATURE_VERSION: SIGNATURE_VERSION,
            HEADER_SIGNATURE: signature,
            "content-type": "application/json",
        },
        body=payload,
    )
