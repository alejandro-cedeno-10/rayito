"""Derivación de `k_sbx` (M15, m15-events-webhooks): el SDK es el único
lado que ve `stack_key` (el secreto de `infra/events-webhooks.yaml`); `rayd`
sólo recibe `k_sbx`, ya derivado, vía `ConfigureSandbox`
(`LifecycleEventsConfig.sandbox_key`). Espejo exacto de
`rayd_core::lifecycle_events::DOMAIN_SEPARATOR` y de
`infra/lambdas/events_webhooks/domain/mac.py`; los vectores compartidos
viven en `testdata/lifecycle-events/mac-vectors.json`.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Final

DOMAIN_SEPARATOR: Final = "rayito.events.v1|"


def derive_sandbox_key(stack_key: bytes, sandbox_id: str) -> bytes:
    return hmac.new(
        stack_key, (DOMAIN_SEPARATOR + sandbox_id).encode("utf-8"), hashlib.sha256
    ).digest()
