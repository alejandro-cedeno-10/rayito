"""`rayito.e2b.Volume`/`AsyncVolume` (`m15-efs-volumes`, ADR-018,
experimental): unbound raises `UnimplementedError("Volume")` on
construction and on every classmethod; bound to a `VolumeStore` via
`E2B(volume_store=...)`, the CRUD delegates to it; `AsyncVolume` raises
eagerly (never inside the coroutine it returns); content operations
(`read_file`/`write_file`/`make_dir`/`list_files`/`remove`/
`update_metadata`) are always `UnimplementedError("volume.content")`,
bound or not. `volume_id` is the logical name everywhere, so the E2B round
trip `destroy(vol.volume_id)` works; `Sandbox.create(volume_mounts=)` goes
through an I/O-free gate before any AWS call."""

from __future__ import annotations

import inspect
from typing import Any, cast

import pytest

from rayito import AsyncVolumeStore, VolumeStore
from rayito.e2b import E2B, AsyncSandbox, AsyncVolume, Sandbox, UnimplementedError, Volume
from rayito.e2b._volume import require_volume_mount_support
from rayito.exceptions import InvalidArgumentException

from .fake_efs import FakeEfsApi, SpySession

FILE_SYSTEM_ID = "fs-0123abcd"

CONTENT_METHODS = ("read_file", "write_file", "make_dir", "list_files", "remove", "update_metadata")


def store_with(api: FakeEfsApi) -> VolumeStore:
    return VolumeStore(file_system_id=FILE_SYSTEM_ID, session=cast(Any, SpySession(api=api)))


# --------------------------------------------------------------- unbound


def test_unbound_volume_raises_on_construction() -> None:
    with pytest.raises(UnimplementedError) as excinfo:
        Volume(volume_id="datos-agente-7")
    assert excinfo.value.feature == "Volume"


def test_unbound_volume_raises_on_every_classmethod() -> None:
    for call in (
        lambda: Volume.create("x"),
        lambda: Volume.connect("x"),
        lambda: Volume.get_info("x"),
        lambda: Volume.list(),
        lambda: Volume.destroy("x"),
    ):
        with pytest.raises(UnimplementedError):
            call()


def test_unbound_async_volume_raises_in_the_act_not_inside_the_coroutine() -> None:
    """Same contract as the rest of the shim's eager resources: calling the
    classmethod itself raises, never the coroutine it would have returned
    (no `await` ever needed to observe the error)."""
    with pytest.raises(UnimplementedError):
        AsyncVolume(volume_id="datos-agente-7")
    for call in (
        lambda: AsyncVolume.create("x"),
        lambda: AsyncVolume.connect("x"),
        lambda: AsyncVolume.get_info("x"),
        lambda: AsyncVolume.list(),
        lambda: AsyncVolume.destroy("x"),
    ):
        with pytest.raises(UnimplementedError):
            call()


@pytest.mark.parametrize("method", CONTENT_METHODS)
def test_content_ops_are_refused_unbound(method: str) -> None:
    instance = object.__new__(Volume)
    with pytest.raises(UnimplementedError) as excinfo:
        getattr(instance, method)()
    assert excinfo.value.feature == "volume.content"


# ----------------------------------------------------------------- bound


def test_client_volume_is_bound_per_client() -> None:
    first_api, second_api = FakeEfsApi(), FakeEfsApi()
    first = E2B(volume_store=store_with(first_api))
    second = E2B(volume_store=store_with(second_api))
    first.Volume.create("only-on-first")
    second.Volume.create("only-on-second")
    assert [v.get("_name") for v in first_api.access_points.values()] == ["only-on-first"]
    assert [v.get("_name") for v in second_api.access_points.values()] == ["only-on-second"]


def test_bound_volume_create_connect_list_destroy_delegate_to_the_store() -> None:
    api = FakeEfsApi()
    client = E2B(volume_store=store_with(api))
    created = client.Volume.create("datos-agente-7")
    assert created.name == "datos-agente-7"
    connected = client.Volume.connect("datos-agente-7")
    assert connected.volume_id == created.volume_id
    assert client.Volume.get_info("datos-agente-7").volume_id == created.volume_id
    assert [v.name for v in client.Volume.list()] == ["datos-agente-7"]
    assert client.Volume.destroy("datos-agente-7") is True
    assert client.Volume.destroy("datos-agente-7") is False


def test_volume_id_round_trips_through_connect_and_destroy() -> None:
    """The basic E2B round trip: `volume_id` is the logical name, the same
    identifier `connect`/`get_info`/`destroy` take; the access point id is
    kept apart."""
    api = FakeEfsApi()
    client = E2B(volume_store=store_with(api))
    vol = client.Volume.create("ws")
    assert vol.volume_id == "ws"
    assert vol.access_point_id is not None and vol.access_point_id.startswith("fsap-")
    assert client.Volume.connect(vol.volume_id).access_point_id == vol.access_point_id
    assert client.Volume.get_info(vol.volume_id).volume_id == vol.volume_id
    assert client.Volume.destroy(vol.volume_id) is True
    assert api.access_points == {}


@pytest.mark.asyncio
async def test_async_volume_id_round_trips_through_connect_and_destroy() -> None:
    api = FakeEfsApi()
    client = E2B(volume_store=store_with(api))
    vol = await client.AsyncVolume.create("ws")
    assert (await client.AsyncVolume.connect(vol.volume_id)).volume_id == "ws"
    assert await client.AsyncVolume.destroy(vol.volume_id) is True
    assert api.access_points == {}


def test_an_async_volume_store_is_rejected_by_the_client() -> None:
    """`E2B(volume_store=)` takes the sync `VolumeStore` only: `AsyncVolume`
    runs it in a thread, and an `AsyncVolumeStore` would hand it coroutines."""
    async_store = AsyncVolumeStore(file_system_id=FILE_SYSTEM_ID)
    with pytest.raises(InvalidArgumentException, match="VolumeStore"):
        E2B(volume_store=cast(Any, async_store))


@pytest.mark.parametrize("method", CONTENT_METHODS)
def test_content_ops_are_refused_even_when_bound(method: str) -> None:
    api = FakeEfsApi()
    client = E2B(volume_store=store_with(api))
    vol = client.Volume.create("datos-agente-7")
    with pytest.raises(UnimplementedError) as excinfo:
        getattr(vol, method)()
    assert excinfo.value.feature == "volume.content"


@pytest.mark.asyncio
async def test_bound_async_volume_delegates_to_the_store() -> None:
    api = FakeEfsApi()
    client = E2B(volume_store=store_with(api))
    created = await client.AsyncVolume.create("datos-agente-7")
    found = await client.AsyncVolume.connect("datos-agente-7")
    assert found.volume_id == created.volume_id
    assert await client.AsyncVolume.destroy("datos-agente-7") is True


def test_async_volume_create_is_a_real_coroutine_once_bound() -> None:
    client = E2B(volume_store=store_with(FakeEfsApi()))
    coro = client.AsyncVolume.create("x")
    assert inspect.iscoroutine(coro)
    coro.close()


# -------------------------------------------- Sandbox.create(volume_mounts=)


def test_volume_mounts_without_a_bound_store_is_unimplemented_volume() -> None:
    """Without `volume_store=`, `Sandbox.create(volume_mounts=)` fails the
    same way `client.Volume` does."""
    with pytest.raises(UnimplementedError) as excinfo:
        require_volume_mount_support({"/mnt/v": "x"}, store=None, template=None)
    assert excinfo.value.feature == "Volume"


@pytest.mark.parametrize("value", ["datos-agente-7", "volume"], ids=["plain-name", "bound-volume"])
def test_volume_mounts_never_calls_aws_and_ends_unimplemented(value: str) -> None:
    """A plain name is never resolved (`DescribeAccessPoints`) while no
    mounter exists: the gate always ends in `UnimplementedError`, so the
    call would cost an AWS request for nothing."""
    api = FakeEfsApi()
    store = store_with(api)
    mount: Any = value
    if value == "volume":
        mount = E2B(volume_store=store).Volume(volume_id="datos-agente-7")
    with pytest.raises(UnimplementedError) as excinfo:
        require_volume_mount_support({"/mnt/v": mount}, store=store, template="rayito-base-caps")
    assert excinfo.value.feature == "volume_mounts"
    assert api.calls == []


def test_volume_mounts_checks_paths_then_caps_before_the_final_unimplemented() -> None:
    store = store_with(FakeEfsApi())
    with pytest.raises(InvalidArgumentException):
        require_volume_mount_support({"relative": "x"}, store=store, template="rayito-base")
    with pytest.raises(UnimplementedError, match="base-caps"):
        require_volume_mount_support({"/mnt/v": "x"}, store=store, template="rayito-base")


@pytest.mark.parametrize("mounts", [{}, {"/mnt/v": 123}, {"/mnt/v": "no valid name!"}])
def test_volume_mounts_rejects_a_malformed_request(mounts: dict[str, Any]) -> None:
    store = store_with(FakeEfsApi())
    with pytest.raises(InvalidArgumentException):
        require_volume_mount_support(mounts, store=store, template="rayito-base-caps")


def test_sandbox_create_with_volume_mounts_makes_no_aws_call() -> None:
    api = FakeEfsApi()
    client = E2B(volume_store=store_with(api))
    with pytest.raises(UnimplementedError):
        client.Sandbox.create("rayito-base-caps", volume_mounts={"/mnt/v": "datos"})
    assert api.calls == []


@pytest.mark.asyncio
async def test_async_sandbox_create_with_volume_mounts_makes_no_aws_call() -> None:
    api = FakeEfsApi()
    client = E2B(volume_store=store_with(api))
    with pytest.raises(UnimplementedError):
        await client.AsyncSandbox.create("rayito-base-caps", volume_mounts={"/mnt/v": "datos"})
    assert api.calls == []


def test_unbound_sandbox_volume_mounts_is_unimplemented_volume() -> None:
    with pytest.raises(UnimplementedError) as excinfo:
        Sandbox.create(volume_mounts={"/mnt/v": "datos"})
    assert excinfo.value.feature == "Volume"
    assert AsyncSandbox._bound_volume_store is None
