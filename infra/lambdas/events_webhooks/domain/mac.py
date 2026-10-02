"""Key derivation and MAC check, mirrored from `rayd_core::lifecycle_events`
and from the SDK's own `_lifecycle_events/_keys.py`
(`clients/python/src/rayito/_lifecycle_events/_keys.py`). Vectors shared
across Rust, Python (SDK and this Lambda) and TypeScript live in
`testdata/lifecycle-events/mac-vectors.json`.
"""

from __future__ import annotations

import hashlib
import hmac

#: Matches `rayd_core::lifecycle_events::DOMAIN_SEPARATOR` and the SDK's own
#: constant exactly; changing it would silently break every already-pushed
#: `k_sbx` without changing a single byte on the wire.
DOMAIN_SEPARATOR = "rayito.events.v1|"


def derive_sandbox_key(stack_key: bytes, sandbox_id: str) -> bytes:
    """`k_sbx = HMAC-SHA256(stack_key, "rayito.events.v1|" + sandbox_id)` —
    the forwarder re-derives this from the stack-wide secret (Secrets
    Manager) rather than ever storing a per-sandbox key itself."""
    return hmac.new(
        stack_key, (DOMAIN_SEPARATOR + sandbox_id).encode("utf-8"), hashlib.sha256
    ).digest()


def verify_mac(key: bytes, payload: bytes, mac: bytes) -> bool:
    """Constant-time: a forged line that gets the sandbox id right but not
    the key must take exactly as long to reject as a line that gets
    everything wrong."""
    expected = hmac.new(key, payload, hashlib.sha256).digest()
    return hmac.compare_digest(expected, mac)
