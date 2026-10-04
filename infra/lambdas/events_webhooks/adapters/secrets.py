"""`boto3` Secrets Manager adapter (`ports.SecretReader`). Cached per Lambda
execution environment (module-level instance), not per invocation, because
Secrets Manager bills per API call; but only for `SECRET_CACHE_TTL_SECONDS`,
so a rotated or revoked webhook secret stops signing deliveries within that
time even in a container that stays warm for hours. `invalidate` drops one
entry at once (the deliverer calls it when a receiver answers 401/403).
Which secret to read is the caller's business: the forwarder reads the
stack key named by its own environment, the deliverer each webhook's secret
named by its row.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, Final

#: Longest a rotated secret keeps being used by a warm container: five
#: minutes, at one extra `GetSecretValue` per secret and container in that
#: time (documented as the rotation delay in the events guide).
SECRET_CACHE_TTL_SECONDS: Final = 300.0


class SecretsManagerReader:
    def __init__(self, client: Any, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._client = client
        self._clock = clock
        self._cache: dict[str, tuple[bytes, float]] = {}

    def read(self, secret_id: str) -> bytes:
        cached = self._cache.get(secret_id)
        now = self._clock()
        if cached is not None and now - cached[1] < SECRET_CACHE_TTL_SECONDS:
            return cached[0]
        response = self._client.get_secret_value(SecretId=secret_id)
        value = response.get("SecretString")
        raw: bytes = value.encode("utf-8") if value is not None else bytes(response["SecretBinary"])
        self._cache[secret_id] = (raw, now)
        return raw

    def invalidate(self, secret_id: str) -> None:
        self._cache.pop(secret_id, None)
