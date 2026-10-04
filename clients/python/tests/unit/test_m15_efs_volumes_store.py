"""`VolumeStore`/`AsyncVolumeStore` (`m15-efs-volumes`, ADR-018,
experimental) sobre un cliente `efs` falso: construcción sin llamadas,
idempotencia de `create`, `get`/`list`/`destroy`."""

from __future__ import annotations

from typing import Any, cast

import pytest

from rayito._volumes._base import LIST_VISIBILITY_BUDGET_SECONDS, LIST_VISIBILITY_POLL_SECONDS
from rayito._volumes._store import VolumeStore
from rayito._volumes._store_async import AsyncVolumeStore
from rayito.exceptions import VolumeNotFoundException

from .fake_efs import FakeEfsApi, SpySession

FILE_SYSTEM_ID = "fs-0123abcd"
OTHER_FILE_SYSTEM_ID = "fs-99999999"


def store_with(api: FakeEfsApi, *, file_system_id: str = FILE_SYSTEM_ID) -> VolumeStore:
    return VolumeStore(file_system_id=file_system_id, session=cast(Any, SpySession(api=api)))


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


def test_create_is_idempotent_even_though_a_repeated_client_token_raises() -> None:
    """EFS answers a reused `ClientToken` with `AccessPointAlreadyExists`
    (409), never the access point directly; `create()` must still look
    idempotent to the caller by falling back to `get(name)`."""
    api = FakeEfsApi()
    store = store_with(api)
    store.create("datos-agente-7")
    assert "create_access_point" in api.calls
    api.calls.clear()

    second = store.create("datos-agente-7")
    assert second.name == "datos-agente-7"
    assert len(api.access_points) == 1
    assert api.calls == ["create_access_point", "describe_access_points"]


class FakeClock:
    """Reloj y espera falsos para el reintento de `create` (Q125)."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def lagging_store(api: FakeEfsApi, clock: FakeClock) -> VolumeStore:
    store = store_with(api)
    store._clock = clock.clock
    store._sleep = clock.sleep
    return store


def test_create_waits_for_an_existing_access_point_to_be_listed() -> None:
    """Q125: right after `CreateAccessPoint`, `DescribeAccessPoints` may not
    list the access point yet; a repeated `create()` (token already spent)
    keeps polling `get` instead of failing with `VolumeNotFoundException`."""
    api = FakeEfsApi()
    clock = FakeClock()
    store = lagging_store(api, clock)
    first = store.create("datos-agente-7")
    api.unlisted.add(first.access_point_id)
    listed_after_polls = 3

    def list_after_a_few_polls(seconds: float) -> None:
        clock.sleep(seconds)
        if len(clock.sleeps) == listed_after_polls:
            api.unlisted.clear()

    store._sleep = list_after_a_few_polls
    second = store.create("datos-agente-7")
    assert second.access_point_id == first.access_point_id
    assert clock.sleeps == [LIST_VISIBILITY_POLL_SECONDS] * listed_after_polls


def test_create_gives_up_once_the_listing_budget_is_spent() -> None:
    api = FakeEfsApi()
    clock = FakeClock()
    store = lagging_store(api, clock)
    first = store.create("datos-agente-7")
    api.unlisted.add(first.access_point_id)
    with pytest.raises(VolumeNotFoundException):
        store.create("datos-agente-7")
    assert clock.now >= LIST_VISIBILITY_BUDGET_SECONDS


def test_destroy_of_a_stale_listed_access_point_returns_false() -> None:
    """Q125: a just-deleted access point stays listed for a few seconds;
    destroying it again is `False`, never `VolumeNotFoundException`."""
    api = FakeEfsApi()
    store = store_with(api)
    volume = store.create("datos-agente-7")
    api.stale[volume.access_point_id] = api.access_points.pop(volume.access_point_id)
    assert store.destroy("datos-agente-7") is False
    assert api.calls[-1] == "delete_access_point"


def test_the_same_name_on_two_file_systems_does_not_share_a_client_token() -> None:
    api = FakeEfsApi()
    first_store = store_with(api, file_system_id=FILE_SYSTEM_ID)
    second_store = store_with(api, file_system_id=OTHER_FILE_SYSTEM_ID)
    first = first_store.create("datos-agente-7")
    second = second_store.create("datos-agente-7")
    assert first.access_point_id != second.access_point_id
    assert len(api.access_points) == 2


def test_recreating_a_destroyed_name_does_not_reuse_a_spent_token() -> None:
    api = FakeEfsApi()
    store = store_with(api)
    first = store.create("datos-agente-7")
    assert store.destroy("datos-agente-7") is True
    second = store.create("datos-agente-7")
    assert second.access_point_id != first.access_point_id


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
