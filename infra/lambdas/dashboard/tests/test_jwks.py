"""Las fuentes de verdad de Cognito con caché: el JWKS y los ids de cliente."""

from __future__ import annotations

import _stack_path  # noqa: F401
import pytest
from dashboard_stack.jwks import ClientIdCache, JwksCache, issuer_url, list_user_pool_client_ids
from dashboard_stack.tokens import AuthUnavailable
from stack_signing import KEY_ID, Signer

TTL = 100.0
MIN_REFRESH = 10.0


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class Fetch:
    def __init__(self, signer: Signer) -> None:
        self.body = signer.jwks()
        self.calls = 0

    def __call__(self) -> bytes:
        self.calls += 1
        return self.body


@pytest.fixture(scope="module")
def signer() -> Signer:
    return Signer()


def cache(fetch: Fetch, clock: Clock) -> JwksCache:
    return JwksCache(fetch, clock=clock, ttl_seconds=TTL, refresh_min_seconds=MIN_REFRESH)


def test_keys_are_fetched_once_and_kept_for_the_ttl(signer: Signer) -> None:
    fetch, clock = Fetch(signer), Clock()
    keys = cache(fetch, clock)
    assert keys.key(KEY_ID) is not None
    assert keys.key(KEY_ID) is not None
    assert fetch.calls == 1
    clock.now = TTL
    assert keys.key(KEY_ID) is not None
    assert fetch.calls == 2


def test_an_unknown_kid_refetches_at_most_once_per_minimum_interval(signer: Signer) -> None:
    fetch, clock = Fetch(signer), Clock()
    keys = cache(fetch, clock)
    assert keys.key("nope") is None
    assert keys.key("nope") is None
    assert fetch.calls == 1
    clock.now = MIN_REFRESH
    assert keys.key("nope") is None
    assert fetch.calls == 2


def test_an_unreachable_or_malformed_jwks_is_unavailable_not_unauthorized(signer: Signer) -> None:
    def broken() -> bytes:
        raise OSError("sin red")

    with pytest.raises(AuthUnavailable):
        JwksCache(broken).key(KEY_ID)
    with pytest.raises(AuthUnavailable):
        JwksCache(lambda: b"[]").key(KEY_ID)


def test_client_ids_are_cached_and_a_failure_is_unavailable() -> None:
    clock, calls = Clock(), []

    def listing() -> frozenset[str]:
        calls.append(1)
        return frozenset({"a"})

    ids = ClientIdCache(listing, clock=clock, ttl_seconds=TTL)
    assert ids.client_ids() == {"a"}
    assert ids.client_ids() == {"a"}
    assert len(calls) == 1

    def failing() -> frozenset[str]:
        raise RuntimeError("sin permiso")

    with pytest.raises(AuthUnavailable):
        ClientIdCache(failing).client_ids()


def test_the_pool_client_listing_follows_pagination_and_keeps_only_the_named_client() -> None:
    class Cognito:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def list_user_pool_clients(self, **params: object) -> dict[str, object]:
            self.calls.append(dict(params))
            if "NextToken" not in params:
                return {
                    "UserPoolClients": [{"ClientId": "a", "ClientName": "spa"}],
                    "NextToken": "t",
                }
            return {
                "UserPoolClients": [
                    {"ClientId": "b", "ClientName": "spa"},
                    {"ClientId": "other", "ClientName": "cli"},
                ]
            }

    cognito = Cognito()
    assert list_user_pool_client_ids(cognito, "us-east-1_Pool", "spa") == {"a", "b"}
    assert len(cognito.calls) == 2


def test_the_issuer_rejects_a_pool_id_that_is_not_a_pool_id() -> None:
    assert issuer_url("us-east-1", "us-east-1_AbC123").endswith("/us-east-1_AbC123")
    with pytest.raises(ValueError):
        issuer_url("us-east-1", "evil.example.com/../x")
