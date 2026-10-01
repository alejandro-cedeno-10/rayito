"""`VolumeStore`/`AsyncVolumeStore` (`m15-efs-volumes`, ADR-018,
experimental) sobre un cliente `efs` falso: construcción sin llamadas,
idempotencia de `create`, `get`/`list`/`destroy`."""

from __future__ import annotations

from typing import Any, cast

import pytest

from rayito._volumes._store import VolumeStore
from rayito._volumes._store_async import AsyncVolumeStore
from rayito.exceptions import VolumeNotFoundException

from .fake_efs import FakeEfsApi, SpySession

FILE_SYSTEM_ID = "fs-0123abcd"


def store_with(api: FakeEfsApi) -> VolumeStore:
    return VolumeStore(file_system_id=FILE_SYSTEM_ID, session=cast(Any, SpySession(api=api)))


def test_construction_never_builds_a_client() -> None:
    spy = SpySession()
    VolumeStore(file_system_id=FILE_SYSTEM_ID, session=cast(Any, spy))
    assert spy.built == []


def test_create_is_idempotent_by_name() -> None:
    api = FakeEfsApi()
    store = store_with(api)
    first = store.create("datos-agente-7")
    second = store.create("datos-agente-7")
    assert second.access_point_id == first.access_point_id
    assert len(api.access_points) == 1


def test_get_finds_a_volume_by_name() -> None:
    api = FakeEfsApi()
    store = store_with(api)
    store.create("datos-agente-7")
    found = store.get("datos-agente-7")
    assert found.name == "datos-agente-7"


def test_get_raises_not_found_for_a_missing_name() -> None:
    store = store_with(FakeEfsApi())
    with pytest.raises(VolumeNotFoundException):
        store.get("no-existe")


def test_list_returns_every_tagged_access_point() -> None:
    api = FakeEfsApi()
    store = store_with(api)
    store.create("a")
    store.create("b")
    names = sorted(v.name for v in store.list() if v.name is not None)
    assert names == ["a", "b"]


def test_destroy_returns_false_for_a_volume_that_never_existed() -> None:
    store = store_with(FakeEfsApi())
    assert store.destroy("ghost") is False


def test_destroy_returns_true_and_removes_the_access_point() -> None:
    api = FakeEfsApi()
    store = store_with(api)
    store.create("datos-agente-7")
    assert store.destroy("datos-agente-7") is True
    assert len(api.access_points) == 0


@pytest.mark.asyncio
async def test_async_store_delegates_to_the_sync_one() -> None:
    api = FakeEfsApi()
    store = AsyncVolumeStore(file_system_id=FILE_SYSTEM_ID, session=cast(Any, SpySession(api=api)))
    created = await store.create("datos-agente-7")
    assert (await store.get("datos-agente-7")).access_point_id == created.access_point_id
    assert await store.destroy("datos-agente-7") is True
