"""`rayito.e2b.Volume`/`AsyncVolume` (`m15-efs-volumes`, ADR-018,
experimental): unbound raises `UnimplementedError("Volume")` on
construction and on every classmethod; bound to a `VolumeStore` via
`E2B(volume_store=...)`, the CRUD delegates to it; `AsyncVolume` raises
eagerly (never inside the coroutine it returns); content operations
(`read_file`/`write_file`/`make_dir`/`list_files`/`remove`/
`update_metadata`) are always `UnimplementedError("volume.read_file")`,
bound or not."""

from __future__ import annotations

import inspect
from typing import Any, cast

import pytest

from rayito import VolumeStore
from rayito.e2b import E2B, AsyncVolume, UnimplementedError, Volume
from rayito.e2b._volume import translate_volume_mounts_kwarg
from rayito.exceptions import InvalidArgumentException

from .fake_efs import FakeEfsApi, SpySession

FILE_SYSTEM_ID = "fs-0123abcd"

CONTENT_METHODS = ("read_file", "write_file", "make_dir", "list_files", "remove", "update_metadata")


def store_with(api: FakeEfsApi) -> VolumeStore:
    return VolumeStore(file_system_id=FILE_SYSTEM_ID, session=cast(Any, SpySession(api=api)))


# --------------------------------------------------------------- unbound


def test_unbound_volume_raises_on_construction() -> None:
    with pytest.raises(UnimplementedError) as excinfo:
        Volume(volume_id="fsap-0123abcd")
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
        AsyncVolume(volume_id="fsap-0123abcd")
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
    assert excinfo.value.feature == "volume.read_file"


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


@pytest.mark.parametrize("method", CONTENT_METHODS)
def test_content_ops_are_refused_even_when_bound(method: str) -> None:
    api = FakeEfsApi()
    client = E2B(volume_store=store_with(api))
    vol = client.Volume.create("datos-agente-7")
    with pytest.raises(UnimplementedError) as excinfo:
        getattr(vol, method)()
    assert excinfo.value.feature == "volume.read_file"


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
    """Finding review: without `volume_store=`, `Sandbox.create(volume_mounts=)`
    must fail the same way `client.Volume` does, not with the old generic
    `unimplemented("volume_mounts")`."""
    with pytest.raises(UnimplementedError) as excinfo:
        translate_volume_mounts_kwarg({"volume_mounts": {"/mnt/v": "x"}}, store=None)
    assert excinfo.value.feature == "Volume"


def test_volume_mounts_resolves_a_bound_volume_instance_without_any_aws_call() -> None:
    api = FakeEfsApi()
    store = store_with(api)
    client = E2B(volume_store=store)
    vol = client.Volume(volume_id="fsap-0123abcd", name="datos-agente-7")
    resolved = translate_volume_mounts_kwarg({"volume_mounts": {"/mnt/v": vol}}, store=store)
    assert api.calls == []
    assert resolved["volumes"]["/mnt/v"].access_point_id == "fsap-0123abcd"
    assert "volume_mounts" not in resolved


def test_volume_mounts_resolves_a_plain_name_through_the_store() -> None:
    api = FakeEfsApi()
    store = store_with(api)
    store.create("datos-agente-7")
    resolved = translate_volume_mounts_kwarg(
        {"volume_mounts": {"/mnt/v": "datos-agente-7"}}, store=store
    )
    assert resolved["volumes"]["/mnt/v"].name == "datos-agente-7"


def test_volume_mounts_rejects_an_unsupported_value_type() -> None:
    store = store_with(FakeEfsApi())
    with pytest.raises(InvalidArgumentException):
        translate_volume_mounts_kwarg({"volume_mounts": {"/mnt/v": 123}}, store=store)


def test_volume_mounts_absent_is_a_no_op() -> None:
    assert translate_volume_mounts_kwarg({"template": "x"}, store=None) == {"template": "x"}
