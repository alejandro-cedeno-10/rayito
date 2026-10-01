"""`boto3` Secrets Manager adapter. Cached per Lambda execution environment
(module-level dict), not per invocation: the stack key and webhook secrets
rotate on a human timescale, and Secrets Manager billing is per API call.
"""

from __future__ import annotations

from typing import Any


class SecretsManagerReader:
    def __init__(self, client: Any, *, stack_key_secret_id: str) -> None:
        self._client = client
        self._stack_key_secret_id = stack_key_secret_id
        self._cache: dict[str, bytes] = {}

    def stack_key(self) -> bytes:
        return self._get(self._stack_key_secret_id)

    def webhook_secret(self, secret_name: str) -> bytes:
        return self._get(secret_name)

    def _get(self, secret_id: str) -> bytes:
        cached = self._cache.get(secret_id)
        if cached is not None:
            return cached
        response = self._client.get_secret_value(SecretId=secret_id)
        value = response.get("SecretString")
        raw = value.encode("utf-8") if value is not None else response["SecretBinary"]
        self._cache[secret_id] = raw
        return raw
