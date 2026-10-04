"""`rayito.e2b.Volume`/`AsyncVolume` (`m15-efs-volumes`, ADR-018,
experimental): unbound raises `UnimplementedError("Volume")` on
construction and on every classmethod; bound to a `VolumeStore` via
`E2B(volume_store=...)`, the CRUD delegates to it; `AsyncVolume` raises
eagerly (never inside the coroutine it returns); content operations
(`read_file`/`write_file`/`make_dir`/`list_files`/`remove`/
`update_metadata`) are always `UnimplementedError("volume.content")`,
bound or not. `volume_id` is the logical name everywhere, so the E2B round
trip `destroy(vol.volume_id)` works; `Sandbox.create(volume_mounts=)` goes
through an I/O-free gate (store, connector, paths, caps) and then launches
the native `volumes=` with only `E2B(volume_connector_arn=)` as egress."""

from __future__ import annotations

import inspect
from typing import Any, cast

import pytest

from rayito import AsyncSandbox as NativeAsyncSandbox
from rayito import AsyncVolumeStore, EfsVolume, VolumeStore
from rayito import Sandbox as NativeSandbox
from rayito.e2b import E2B, AsyncSandbox, AsyncVolume, Sandbox, UnimplementedError, Volume
from rayito.e2b._compat import map_create_kwargs
from rayito.e2b._volume import plan_volume_mounts, resolve_volume_mounts
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

#: Marcadores de documentación (cuenta ficticia).
CONNECTOR = "arn:aws:lambda:us-east-1:123456789012:network-connector:rayito-efs"
ROLE = "arn:aws:iam::123456789012:role/rayito-execution"


def plan(mounts: Any, **overrides: Any) -> Any:
    kwargs: dict[str, Any] = {
        "store": store_with(FakeEfsApi()),
        "connector_arn": CONNECTOR,
        "template": "rayito-base-caps-efs",
        "allow_internet_access": None,
        **overrides,
    }
    return plan_volume_mounts(mounts, **kwargs)


def test_volume_mounts_without_a_bound_store_is_unimplemented_volume() -> None:
    """Without `volume_store=`, `Sandbox.create(volume_mounts=)` fails the
    same way `client.Volume` does."""
    with pytest.raises(UnimplementedError) as excinfo:
        plan({"/mnt/v": "x"}, store=None)
    assert excinfo.value.feature == "Volume"


def test_volume_mounts_without_a_connector_names_the_client_option() -> None:
    with pytest.raises(InvalidArgumentException, match="volume_connector_arn"):
        plan({"/mnt/v": "x"}, connector_arn=None)


def test_volume_mounts_refuses_explicit_internet_access() -> None:
    """Q131: one egress connector per MicroVM."""
    with pytest.raises(InvalidArgumentException, match="allow_internet_access"):
        plan({"/mnt/v": "x"}, allow_internet_access=True)


def test_volume_mounts_plan_is_pure_and_keeps_names_unresolved() -> None:
    api = FakeEfsApi()
    planned = plan({"/mnt/v": "datos"}, store=store_with(api))
    assert dict(planned) == {"/mnt/v": "datos"}
    assert api.calls == []


def test_volume_mounts_checks_paths_then_caps() -> None:
    with pytest.raises(InvalidArgumentException):
        plan({"relative": "x"}, template="rayito-base")
    with pytest.raises(UnimplementedError, match="base-caps"):
        plan({"/mnt/v": "x"}, template="rayito-base")


@pytest.mark.parametrize("mounts", [{}, {"/mnt/v": 123}, {"/mnt/v": "no valid name!"}])
def test_volume_mounts_rejects_a_malformed_request(mounts: dict[str, Any]) -> None:
    with pytest.raises(InvalidArgumentException):
        plan(mounts)


def test_map_create_kwargs_launches_only_through_the_volume_connector() -> None:
    store = store_with(FakeEfsApi())
    mapping = map_create_kwargs(
        "rayito-base-caps-efs",
        volume_mounts={"/mnt/v": "datos"},
        volume_store=store,
        volume_connector_arn=CONNECTOR,
    )
    assert mapping.native_kwargs["egress"] == [CONNECTOR]
    assert "allow_internet_access" not in mapping.native_kwargs
    assert dict(mapping.volume_mounts or {}) == {"/mnt/v": "datos"}
    denied = map_create_kwargs(
        "rayito-base-caps-efs",
        allow_internet_access=False,
        volume_mounts={"/mnt/v": "datos"},
        volume_store=store,
        volume_connector_arn=CONNECTOR,
    )
    assert denied.native_kwargs["allow_internet_access"] is False


def test_map_create_kwargs_without_volume_mounts_keeps_internet_egress() -> None:
    mapping = map_create_kwargs("rayito-base", volume_connector_arn=CONNECTOR)
    assert mapping.native_kwargs["egress"] == ["INTERNET_EGRESS"]
    assert mapping.volume_mounts is None


def test_resolve_volume_mounts_maps_volumes_and_resolves_names() -> None:
    api = FakeEfsApi()
    store = store_with(api)
    created = store.create("datos")
    client = E2B(volume_store=store, volume_connector_arn=CONNECTOR)
    bound = client.Volume(volume_id="otro", access_point_id="fsap-0456abcd")
    api.calls.clear()
    resolved = resolve_volume_mounts({"/mnt/a": "datos", "/mnt/b": bound}, store)
    assert resolved["/mnt/a"].access_point_id == created.access_point_id
    assert resolved["/mnt/b"] == EfsVolume(
        file_system_id=FILE_SYSTEM_ID,
        access_point_id="fsap-0456abcd",
        name="otro",
        region=store.region,
    )
    assert api.calls == ["describe_access_points"]


def test_sandbox_create_with_volume_mounts_launches_native_volumes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def native_create(**kwargs: Any) -> Any:
        captured.update(kwargs)
        raise RuntimeError("stop after mapping")

    monkeypatch.setattr(NativeSandbox, "create", staticmethod(native_create))
    store = store_with(FakeEfsApi())
    created = store.create("datos")
    client = E2B(volume_store=store, volume_connector_arn=CONNECTOR)
    with pytest.raises(RuntimeError, match="stop after mapping"):
        client.Sandbox.create(
            "rayito-base-caps-efs", volume_mounts={"/mnt/v": "datos"}, execution_role_arn=ROLE
        )
    assert captured["egress"] == [CONNECTOR]
    assert captured["volumes"]["/mnt/v"].access_point_id == created.access_point_id
    assert captured["execution_role_arn"] == ROLE


@pytest.mark.asyncio
async def test_async_sandbox_create_with_volume_mounts_launches_native_volumes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def native_create(**kwargs: Any) -> Any:
        captured.update(kwargs)
        raise RuntimeError("stop after mapping")

    monkeypatch.setattr(NativeAsyncSandbox, "create", staticmethod(native_create))
    store = store_with(FakeEfsApi())
    created = store.create("datos")
    client = E2B(volume_store=store, volume_connector_arn=CONNECTOR)
    with pytest.raises(RuntimeError, match="stop after mapping"):
        await client.AsyncSandbox.create(
            "rayito-base-caps-efs", volume_mounts={"/mnt/v": "datos"}, execution_role_arn=ROLE
        )
    assert captured["egress"] == [CONNECTOR]
    assert captured["volumes"]["/mnt/v"].access_point_id == created.access_point_id


def test_sandbox_create_without_a_connector_makes_no_aws_call() -> None:
    api = FakeEfsApi()
    client = E2B(volume_store=store_with(api))
    with pytest.raises(InvalidArgumentException, match="volume_connector_arn"):
        client.Sandbox.create("rayito-base-caps", volume_mounts={"/mnt/v": "datos"})
    assert api.calls == []


def test_unbound_sandbox_volume_mounts_is_unimplemented_volume() -> None:
    with pytest.raises(UnimplementedError) as excinfo:
        Sandbox.create(volume_mounts={"/mnt/v": "datos"})
    assert excinfo.value.feature == "Volume"
    assert AsyncSandbox._bound_volume_store is None
