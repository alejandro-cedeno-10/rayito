"""`e2b.Volume`/`AsyncVolume` (`m15-efs-volumes`, ADR-018, experimental):
CRUD real sobre un `VolumeStore` que el cliente `E2B(volume_store=...)`
configura; sin configurar, cada acceso (construir, o llamar cualquier
método o classmethod) lanza `unimplemented("Volume")`, nunca
`AttributeError` ni `TypeError` — el mismo contrato que el resto de
recursos sin primitiva del shim (`_unimplemented.py`).

Las operaciones de contenido (`read_file`, `write_file`, `make_dir`,
`list`, `remove`) no tienen plano de datos propio fuera de un MicroVM
(`SPEC.md` §4): siempre son `UnimplementedError`, configurado o no.
"""

from __future__ import annotations

import asyncio
import builtins
from collections.abc import Coroutine, Mapping
from typing import Any, ClassVar, NoReturn

from rayito._volumes import EfsVolume, VolumeStore
from rayito.e2b._unimplemented import unimplemented
from rayito.exceptions import InvalidArgumentException


class Volume:
    """`e2b.Volume` sobre `VolumeStore` (síncrono). El identificador que usa
    Rayito aquí (`volume_id`, y el parámetro de `connect`/`get_info`/
    `destroy`) es el **nombre lógico** del volumen, no el `AccessPointId` de
    AWS: `VolumeStore` indexa por nombre. `volume_id` de una instancia ya
    creada sigue llevando el `AccessPointId` real, informativo."""

    _bound_store: ClassVar[VolumeStore | None] = None

    def __new__(cls, *args: Any, **kwargs: Any) -> Volume:
        if cls._bound_store is None:
            raise unimplemented("Volume")
        return super().__new__(cls)

    def __init__(self, *, volume_id: str, name: str | None = None) -> None:
        self.volume_id = volume_id
        self.name = name

    @classmethod
    def _require_store(cls) -> VolumeStore:
        if cls._bound_store is None:
            raise unimplemented("Volume")
        return cls._bound_store

    @classmethod
    def create(cls, name: str, **_opts: Any) -> Volume:
        """`VolumeStore.create(name)`; `CreateAccessPoint`, idempotente."""
        created = cls._require_store().create(name)
        return cls(volume_id=created.access_point_id, name=created.name)

    @classmethod
    def connect(cls, volume_id: str, **_opts: Any) -> Volume:
        """Busca el volumen por nombre lógico (ver la nota de la clase)."""
        found = cls._require_store().get(volume_id)
        return cls(volume_id=found.access_point_id, name=found.name)

    @classmethod
    def get_info(cls, volume_id: str, **_opts: Any) -> Volume:
        return cls.connect(volume_id)

    @classmethod
    def list(cls, **_opts: Any) -> builtins.list[Volume]:
        store = cls._require_store()
        return [cls(volume_id=v.access_point_id, name=v.name) for v in store.list() if v.name]

    @classmethod
    def destroy(cls, volume_id: str, **_opts: Any) -> bool:
        """`DeleteAccessPoint`; el directorio en sí no se borra."""
        return cls._require_store().destroy(volume_id)

    def read_file(self, *args: Any, **kwargs: Any) -> NoReturn:
        raise unimplemented("volume.read_file")

    write_file = read_file
    make_dir = read_file
    list_files = read_file
    remove = read_file
    update_metadata = read_file


class AsyncVolume:
    """`e2b.AsyncVolume`: la misma semántica que `Volume`, con corrutinas
    (`AsyncVolumeStore` por debajo). Sin volumen configurado, cada
    classmethod lanza **en el acto** (nunca dentro de la corrutina que
    devuelve): `AsyncVolume.create(...)` sin `await` ya lanzó, igual que el
    `Volume` síncrono y que `clients/typescript/src/e2b/resources.ts`."""

    _bound_store: ClassVar[VolumeStore | None] = None

    def __new__(cls, *args: Any, **kwargs: Any) -> AsyncVolume:
        if cls._bound_store is None:
            raise unimplemented("Volume")
        return super().__new__(cls)

    def __init__(self, *, volume_id: str, name: str | None = None) -> None:
        self.volume_id = volume_id
        self.name = name

    @classmethod
    def _require_store(cls) -> VolumeStore:
        if cls._bound_store is None:
            raise unimplemented("Volume")
        return cls._bound_store

    @classmethod
    def create(cls, name: str, **_opts: Any) -> Coroutine[Any, Any, AsyncVolume]:
        store = cls._require_store()
        return cls._create(store, name)

    @classmethod
    async def _create(cls, store: VolumeStore, name: str) -> AsyncVolume:
        created = await asyncio.to_thread(store.create, name)
        return cls(volume_id=created.access_point_id, name=created.name)

    @classmethod
    def connect(cls, volume_id: str, **_opts: Any) -> Coroutine[Any, Any, AsyncVolume]:
        store = cls._require_store()
        return cls._connect(store, volume_id)

    @classmethod
    async def _connect(cls, store: VolumeStore, volume_id: str) -> AsyncVolume:
        found = await asyncio.to_thread(store.get, volume_id)
        return cls(volume_id=found.access_point_id, name=found.name)

    @classmethod
    def get_info(cls, volume_id: str, **_opts: Any) -> Coroutine[Any, Any, AsyncVolume]:
        store = cls._require_store()
        return cls._connect(store, volume_id)

    @classmethod
    def list(cls, **_opts: Any) -> Coroutine[Any, Any, builtins.list[AsyncVolume]]:
        store = cls._require_store()
        return cls._list(store)

    @classmethod
    async def _list(cls, store: VolumeStore) -> builtins.list[AsyncVolume]:
        volumes = await asyncio.to_thread(store.list)
        return [cls(volume_id=v.access_point_id, name=v.name) for v in volumes if v.name]

    @classmethod
    def destroy(cls, volume_id: str, **_opts: Any) -> Coroutine[Any, Any, bool]:
        store = cls._require_store()
        return asyncio.to_thread(store.destroy, volume_id)

    def read_file(self, *args: Any, **kwargs: Any) -> NoReturn:
        raise unimplemented("volume.read_file")

    write_file = read_file
    make_dir = read_file
    list_files = read_file
    remove = read_file
    update_metadata = read_file


def resolve_volume_mounts(
    volume_mounts: Mapping[str, Any], *, store: VolumeStore
) -> dict[str, EfsVolume]:
    """`Sandbox.create(volume_mounts={path: Volume|str})` → `volumes={path:
    EfsVolume}` (research doc §4.5 "Volume | name"): a bound `Volume`
    already carries its `access_point_id`/`name`, no AWS call; a plain
    string name is looked up with `store.get(name)` (one
    `DescribeAccessPoints`, the same the shim's own `Volume.connect` would
    make). Called from `_sync.py`/`_async.py`'s `_launch`, never from the
    I/O-free `_compat.py` table, precisely because the plain-string case
    does call AWS."""
    resolved: dict[str, EfsVolume] = {}
    for path, value in volume_mounts.items():
        if isinstance(value, (Volume, AsyncVolume)):
            resolved[path] = EfsVolume(
                file_system_id=store.file_system_id,
                access_point_id=value.volume_id,
                name=value.name,
                region=store.region,
            )
        elif isinstance(value, str):
            resolved[path] = store.get(value)
        else:
            raise InvalidArgumentException(
                "volume_mounts espera un Volume o un nombre de texto, se recibió "
                f"{type(value).__name__}"
            )
    return resolved


def translate_volume_mounts_kwarg(
    create_kwargs: Mapping[str, Any], *, store: VolumeStore | None
) -> dict[str, Any]:
    """`_sync.py`/`_async.py`'s `_launch`: una copia de `create_kwargs` con
    `volume_mounts` resuelto a `volumes` (o ausente, si no se pasó
    ninguno), antes de pasarla a la tabla pura `map_create_kwargs`. Sin
    `volume_store=` en el cliente, un `volume_mounts` no vacío sigue
    siendo `unimplemented("Volume")` — el mismo guard que `client.Volume`."""
    resolved = dict(create_kwargs)
    volume_mounts = resolved.pop("volume_mounts", None)
    if volume_mounts is not None:
        if store is None:
            raise unimplemented("Volume")
        resolved["volumes"] = resolve_volume_mounts(volume_mounts, store=store)
    return resolved
