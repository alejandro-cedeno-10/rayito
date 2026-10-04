"""`adapters.secrets.SecretsManagerReader`: cached per warm container, but
only for `SECRET_CACHE_TTL_SECONDS`, so a rotated or revoked webhook secret
stops signing deliveries within that time; `invalidate` forgets one entry
at once."""

from __future__ import annotations

from adapters.secrets import SECRET_CACHE_TTL_SECONDS, SecretsManagerReader


class _RotatingClient:
    def __init__(self) -> None:
        self.value = "old"
        self.calls = 0

    def get_secret_value(self, *, SecretId: str) -> dict[str, str]:
        del SecretId
        self.calls += 1
        return {"SecretString": self.value}


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_a_rotated_secret_is_picked_up_after_the_ttl() -> None:
    client, clock = _RotatingClient(), _Clock()
    reader = SecretsManagerReader(client, clock=clock)
    assert reader.read("rayito/webhooks/a") == b"old"
    client.value = "new"
    clock.now += SECRET_CACHE_TTL_SECONDS - 1
    assert reader.read("rayito/webhooks/a") == b"old"
    clock.now += 1
    assert reader.read("rayito/webhooks/a") == b"new"
    assert client.calls == 2


def test_invalidate_forces_a_fresh_read() -> None:
    client = _RotatingClient()
    reader = SecretsManagerReader(client, clock=_Clock())
    reader.read("rayito/webhooks/a")
    client.value = "new"
    reader.invalidate("rayito/webhooks/a")
    assert reader.read("rayito/webhooks/a") == b"new"


def test_secret_binary_is_returned_as_is() -> None:
    class _BinaryClient:
        def get_secret_value(self, *, SecretId: str) -> dict[str, bytes]:
            del SecretId
            return {"SecretBinary": b"\x00\x01"}

    assert SecretsManagerReader(_BinaryClient()).read("x") == b"\x00\x01"
