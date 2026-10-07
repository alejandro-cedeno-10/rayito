"""SSRF classification (T22): loopback, private, link-local (incl. IMDS),
multicast, reserved and CGNAT are all blocked; a plain public address is not.
The addresses come from `testdata/lifecycle-events/ssrf-address-vectors.json`.
"""

from __future__ import annotations

import ipaddress
import json
from pathlib import Path

from domain.ssrf import first_safe_address, is_blocked

REPO_ROOT = Path(__file__).resolve().parents[4]
#: Shared with both SDKs' `register_webhook` check
#: (`is_blocked_webhook_address` / `isBlockedWebhookAddress`), so the client
#: and the deliverer can never disagree on a literal address.
VECTORS = json.loads(
    (REPO_ROOT / "testdata" / "lifecycle-events" / "ssrf-address-vectors.json").read_text(
        encoding="utf-8"
    )
)
BLOCKED_ADDRESSES: list[str] = VECTORS["blocked"]
ALLOWED_ADDRESSES: list[str] = VECTORS["allowed"]


def test_every_blocked_address_is_classified_as_blocked() -> None:
    for text in BLOCKED_ADDRESSES:
        assert is_blocked(ipaddress.ip_address(text)), text


def test_every_allowed_address_is_not_blocked() -> None:
    for text in ALLOWED_ADDRESSES:
        assert not is_blocked(ipaddress.ip_address(text)), text


def test_first_safe_address_skips_blocked_candidates() -> None:
    candidates = [ipaddress.ip_address("169.254.169.254"), ipaddress.ip_address("8.8.8.8")]
    assert first_safe_address(candidates) == ipaddress.ip_address("8.8.8.8")


def test_first_safe_address_is_none_when_everything_is_blocked() -> None:
    candidates = [ipaddress.ip_address("127.0.0.1"), ipaddress.ip_address("169.254.169.254")]
    assert first_safe_address(candidates) is None
