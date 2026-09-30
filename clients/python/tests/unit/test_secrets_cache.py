"""`SecretCache`: nunca traer el secreto en cada llamada. TTL con reloj falso,
`refresh`/`invalidate`, una sola petición en vuelo por clave (hilos y
tareas), claves distintas por región y sesión, `ttl_seconds=0` rechazado,
los no encontrados sin cachear y ningún valor en `repr` ni en los logs."""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, cast

import pytest

from rayito import SecretCache, SecretRef, SecretStore
from rayito._secrets import shared_secret_cache
from rayito.exceptions import InvalidArgumentException, SecretNotFoundException

from .fake_secrets import SENTINEL_NAME, SENTINEL_VALUE, FakeSecretsManager, SpySession


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def cache_over(
    api: FakeSecretsManager, *, ttl: float = 300, region: str = "us-east-1"
) -> tuple[SecretCache, Clock]:
    cache = SecretCache(
        ttl_seconds=ttl, store=SecretStore(session=cast(Any, SpySession(api=api)), region=region)
    )
    clock = Clock()
    cache._clock = clock
    return cache, clock


@pytest.fixture
def api() -> FakeSecretsManager:
    fake = FakeSecretsManager()
    fake.put("rayito/openai", SENTINEL_VALUE)
    return fake


def test_n_reads_within_the_ttl_make_one_aws_call(api: FakeSecretsManager) -> None:
    cache, clock = cache_over(api)
    for _ in range(50):
        assert cache.get("openai") == SENTINEL_VALUE
        clock.now += 5
    assert api.count("GetSecretValue") == 1


def test_after_the_ttl_the_next_read_refetches(api: FakeSecretsManager) -> None:
    cache, clock = cache_over(api, ttl=300)
    cache.get("openai")
    clock.now += 299.9
    cache.get("openai")
    assert api.count("GetSecretValue") == 1
    clock.now += 0.2
    cache.get("openai")
    assert api.count("GetSecretValue") == 2


def test_refresh_forces_a_new_read_and_sees_the_new_value(api: FakeSecretsManager) -> None:
    cache, _ = cache_over(api)
    assert cache.get("openai") == SENTINEL_VALUE
    api.put("rayito/openai", "rotated", version_id="v2")
    assert cache.get("openai") == SENTINEL_VALUE
    cache.refresh("openai")
    assert api.count("GetSecretValue") == 2
    assert cache.get("openai") == "rotated"
    assert api.count("GetSecretValue") == 2


def test_invalidate_drops_one_or_all(api: FakeSecretsManager) -> None:
    api.put("rayito/other", "o")
    cache, _ = cache_over(api)
    cache.get("openai")
    cache.get("other")
    cache.invalidate("openai")
    cache.get("other")
    assert api.count("GetSecretValue") == 2
    cache.get("openai")
    assert api.count("GetSecretValue") == 3
    cache.invalidate()
    cache.get("openai")
    cache.get("other")
    assert api.count("GetSecretValue") == 5
    cache.refresh()
    assert api.count("GetSecretValue") == 5


def test_versions_are_separate_keys(api: FakeSecretsManager) -> None:
    api.put("rayito/openai", "v2-value", version_id="v2")
    cache, _ = cache_over(api)
    assert cache.get(SecretRef("openai", version_id="v1")) == SENTINEL_VALUE
    assert cache.get(SecretRef("openai", version_id="v2")) == "v2-value"
    assert cache.get("openai") == "v2-value"
    assert api.count("GetSecretValue") == 3
    cache.get(SecretRef("openai", version_stage="AWSCURRENT"))
    assert api.count("GetSecretValue") == 3


def test_ten_concurrent_threads_make_one_call(api: FakeSecretsManager) -> None:
    api.get_delay = 0.2
    cache, _ = cache_over(api)
    barrier = threading.Barrier(10)
    results: list[str] = []

    def reader() -> None:
        barrier.wait()
        results.append(cache.get("openai"))

    threads = [threading.Thread(target=reader) for _ in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results == [SENTINEL_VALUE] * 10
    assert api.count("GetSecretValue") == 1


def test_ten_concurrent_tasks_make_one_call(api: FakeSecretsManager) -> None:
    api.get_delay = 0.2
    cache, _ = cache_over(api)

    async def main() -> list[str]:
        return await asyncio.gather(*(cache.aget("openai") for _ in range(10)))

    assert asyncio.run(main()) == [SENTINEL_VALUE] * 10
    assert api.count("GetSecretValue") == 1


def test_different_sessions_and_regions_never_share_values(api: FakeSecretsManager) -> None:
    other = FakeSecretsManager()
    other.put("rayito/openai", "other-account")
    first = SpySession(api=api)
    second = SpySession(api=other)
    assert shared_secret_cache("us-east-1", cast(Any, first)) is shared_secret_cache(
        "us-east-1", cast(Any, first)
    )
    assert shared_secret_cache("us-east-1", cast(Any, first)) is not shared_secret_cache(
        "us-east-1", cast(Any, second)
    )
    assert shared_secret_cache("us-east-1", cast(Any, first)) is not shared_secret_cache(
        "eu-west-1", cast(Any, first)
    )
    assert shared_secret_cache("us-east-1", cast(Any, first)).get("openai") == SENTINEL_VALUE
    assert shared_secret_cache("us-east-1", cast(Any, second)).get("openai") == "other-account"
    key_a = cache_over(api, region="us-east-1")[0]._key(SecretRef("openai"))
    key_b = cache_over(api, region="eu-west-1")[0]._key(SecretRef("openai"))
    assert key_a != key_b


@pytest.mark.parametrize("ttl", [0, -1, 86_401, True, "300"])
def test_ttl_outside_1_to_86400_is_rejected(ttl: Any) -> None:
    with pytest.raises(InvalidArgumentException):
        SecretCache(ttl_seconds=ttl)


def test_store_and_client_options_are_exclusive() -> None:
    with pytest.raises(InvalidArgumentException):
        SecretCache(store=SecretStore(), region="us-east-1")


def test_not_found_is_never_cached(api: FakeSecretsManager) -> None:
    cache, _ = cache_over(api)
    with pytest.raises(SecretNotFoundException):
        cache.get(SENTINEL_NAME)
    with pytest.raises(SecretNotFoundException):
        cache.get(SENTINEL_NAME)
    assert api.count("GetSecretValue") == 2
    api.put(f"rayito/{SENTINEL_NAME}", "now-exists")
    assert cache.get(SENTINEL_NAME) == "now-exists"


def test_construction_makes_no_aws_call_and_builds_no_client() -> None:
    spy = SpySession()
    SecretCache(store=SecretStore(session=cast(Any, spy)))
    assert spy.built == []


def test_repr_str_and_logs_never_contain_the_value(
    api: FakeSecretsManager, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    cache, _ = cache_over(api)
    cache.get("openai")
    for rendered in (
        repr(cache),
        str(cache),
        repr(SecretRef("openai")),
        repr(list(cache._entries.values())),
    ):
        assert SENTINEL_VALUE not in rendered
    assert "***" in repr(cache)
    assert SENTINEL_VALUE not in caplog.text
