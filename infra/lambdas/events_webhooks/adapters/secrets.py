"""`boto3` Secrets Manager adapter (`ports.SecretReader`). Cached per Lambda
execution environment (module-level instance), not per invocation: the
stack key and webhook secrets rotate on a human timescale, and Secrets
Manager bills per API call. Which secret to read is the caller's business:
the forwarder reads the stack key named by its own environment, the
deliverer each webhook's secret named by its row.
"""

from __future__ import annotations

from typing import Any


class SecretsManagerReader:
    def __init__(self, client: Any) -> None:
        self._client = client
        self._cache: dict[str, bytes] = {}

    def read(self, secret_id: str) -> bytes:
        cached = self._cache.get(secret_id)
        if cached is not None:
            return cached
        response = self._client.get_secret_value(SecretId=secret_id)
        value = response.get("SecretString")
        raw: bytes = value.encode("utf-8") if value is not None else bytes(response["SecretBinary"])
        self._cache[secret_id] = raw
        return raw
