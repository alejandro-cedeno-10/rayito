"""`SandboxPool.take(secrets=)` y `Sandbox.create(pool=, secrets=)`: las
plazas calientes nunca llevan secretos (ni en su lanzamiento ni en su
`SlotRecord`); se enlazan al sandbox que sale del pool, tras resolverlos."""

# Los fixtures del pool se importan de test_pool_sync: sus nombres vuelven
# como parámetros de los tests (así los resuelve pytest).
# ruff: noqa: F811

from __future__ import annotations

import warnings
from typing import Any, cast

import pytest

from rayito import RayitoCompatWarning, Sandbox, SecretCache, SecretRef, SecretStore
from rayito._secrets import relaunch_secrets
from rayito.exceptions import SecretNotFoundException

from .fake_control_plane import FakeControlPlane
from .fake_secrets import SENTINEL_NAME, SENTINEL_VALUE, FakeSecretsManager, SpySession
from .test_pool_sync import (  # noqa: F401
    PoolFactory,
    clock,
    make_pool,
    now,
    plane,
    sleeps,
    transport,
    wait_idle,
)


@pytest.fixture
def secret_api() -> FakeSecretsManager:
    api = FakeSecretsManager()
    api.put(f"rayito/{SENTINEL_NAME}", SENTINEL_VALUE)
    return api


@pytest.fixture
def cache(secret_api: FakeSecretsManager) -> SecretCache:
    return SecretCache(store=SecretStore(session=cast(Any, SpySession(api=secret_api))))


@pytest.fixture(autouse=True)
def quiet_visibility_warning() -> Any:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RayitoCompatWarning)
        yield


def test_create_with_pool_and_secrets_binds_on_take(
    make_pool: PoolFactory,
    plane: FakeControlPlane,
    cache: SecretCache,
    secret_api: FakeSecretsManager,
) -> None:
    pool = make_pool(size=1).start()
    wait_idle(pool, 1)
    sandbox = Sandbox.create(
        pool=pool, secrets={"OPENAI_API_KEY": SENTINEL_NAME}, secret_cache=cache
    )
    try:
        assert pool.stats().hits == 1
        for _ in range(3):
            assert f"OPENAI_API_KEY={SENTINEL_VALUE}" in sandbox.commands.run("env").stdout
        assert secret_api.count("GetSecretValue") == 1
    finally:
        sandbox.kill()
    for call in plane.calls:
        assert SENTINEL_VALUE not in repr(call)
        assert SENTINEL_NAME not in repr(call)


def test_take_with_secrets_never_touches_slot_records(
    make_pool: PoolFactory, cache: SecretCache
) -> None:
    pool = make_pool(size=2).start()
    wait_idle(pool, 2)
    sandbox = pool.take(secrets={"OPENAI_API_KEY": SENTINEL_NAME}, secret_cache=cache)
    try:
        assert f"OPENAI_API_KEY={SENTINEL_VALUE}" in sandbox.commands.run("env").stdout
        for record in pool._records.values():
            assert SENTINEL_VALUE not in repr(record)
            assert SENTINEL_NAME not in repr(record)
        assert SENTINEL_VALUE not in repr(pool.stats())
    finally:
        sandbox.kill()


def test_reincarnate_after_take_keeps_the_secrets_of_take(
    make_pool: PoolFactory, cache: SecretCache
) -> None:
    """Las referencias de `take(secrets=)` viven en el handle, que es de donde
    `reincarnate()` las toma (no de las opciones de lanzamiento de la plaza)."""
    pool = make_pool(size=1).start()
    wait_idle(pool, 1)
    sandbox = pool.take(secrets={"OPENAI_API_KEY": SENTINEL_NAME}, secret_cache=cache)
    try:
        assert relaunch_secrets(sandbox._secrets) == (
            {"OPENAI_API_KEY": SecretRef(SENTINEL_NAME)},
            cache,
        )
    finally:
        sandbox.kill()


def test_a_missing_secret_fails_before_claiming_a_slot(
    make_pool: PoolFactory, cache: SecretCache
) -> None:
    pool = make_pool(size=1).start()
    wait_idle(pool, 1)
    with pytest.raises(SecretNotFoundException):
        pool.take(secrets={"MISSING": "nope"}, secret_cache=cache)
    assert pool.stats().ready == 1
    assert pool.stats().hits == 0
