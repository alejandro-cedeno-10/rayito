"""`e2b.Volume`/`AsyncVolume` (`m15-efs-volumes`, ADR-018, experimental):
CRUD real sobre un `VolumeStore` que el cliente `E2B(volume_store=...)`
configura; sin configurar, cada acceso (construir, o llamar cualquier
método o classmethod) lanza `unimplemented("Volume")`, nunca
`AttributeError` ni `TypeError` — el mismo contrato que el resto de
recursos sin primitiva del shim (`_unimplemented.py`).

Un solo identificador: `volume_id` es el **nombre lógico** del volumen,
el mismo que reciben `connect`/`get_info`/`destroy` (`VolumeStore` indexa
por nombre), así que `Volume.destroy(vol.volume_id)` borra el volumen que
`Volume.create` devolvió. El `AccessPointId` de AWS va aparte, en
`access_point_id`.

Las operaciones de contenido (`read_file`, `write_file`, `make_dir`,
`list_files`, `remove`, `update_metadata`) no tienen plano de datos propio
fuera de un MicroVM (`SPEC.md` §4): siempre son
`UnimplementedError("volume.content")`, configurado o no.
"""

from __future__ import annotations

import asyncio
import builtins
from collections.abc import Callable, Coroutine, Mapping
from typing import Any, ClassVar, NoReturn, Self

from rayito._role_policy import resolve_image_variant
from rayito._volumes import EfsVolume, VolumeStore
from rayito._volumes._domain import validate_volume_name
from rayito._volumes._section import require_volume_mounts
from rayito.e2b._unimplemented import unimplemented
from rayito.exceptions import InvalidArgumentException

#: La clave de `UNIMPLEMENTED_REASONS` de toda operación de contenido.
VOLUME_CONTENT_FEATURE = "volume.content"


def require_sync_store(store: object) -> VolumeStore | None:
    """`E2B(volume_store=)` sólo acepta el `VolumeStore` síncrono: `Volume`
    lo llama directamente y `AsyncVolume` en un hilo (`asyncio.to_thread`),
    así que un `AsyncVolumeStore` (cuyos métodos devuelven corrutinas)
    rompería a los dos en silencio."""
    if store is None or isinstance(store, VolumeStore):
        return store
    raise InvalidArgumentException(
        "E2B(volume_store=) espera un VolumeStore (síncrono; AsyncVolume lo usa en un "
        f"hilo), se recibió {type(store).__name__}"
    )


def refuse_content(*args: Any, **kwargs: Any) -> NoReturn:
    raise unimplemented(VOLUME_CONTENT_FEATURE)


class _VolumeBase:
    """Lo que comparten `Volume` y `AsyncVolume`: el guard del store ligado,
    la identidad (`volume_id` = nombre lógico) y el rechazo del contenido."""

    _bound_store: ClassVar[VolumeStore | None] = None

    def __new__(cls, *args: Any, **kwargs: Any) -> Self:
        if cls._bound_store is None:
            raise unimplemented("Volume")
        return super().__new__(cls)

    def __init__(self, *, volume_id: str, access_point_id: str | None = None) -> None:
        self.volume_id = volume_id
        self.name = volume_id
        self.access_point_id = access_point_id

    @classmethod
    def _require_store(cls) -> VolumeStore:
        if cls._bound_store is None:
            raise unimplemented("Volume")
        return cls._bound_store

    @classmethod
    def _from_efs(cls, volume: EfsVolume) -> Self:
        # `VolumeStore.create`/`get` siempre fijan el nombre; `list` filtra
        # los access points sin él antes de llegar aquí.
        if volume.name is None:
            raise InvalidArgumentException("el access point no tiene nombre de volumen Rayito")
        return cls(volume_id=volume.name, access_point_id=volume.access_point_id)

    read_file = staticmethod(refuse_content)
    write_file = staticmethod(refuse_content)
    make_dir = staticmethod(refuse_content)
    list_files = staticmethod(refuse_content)
    remove = staticmethod(refuse_content)
    update_metadata = staticmethod(refuse_content)


class Volume(_VolumeBase):
    """`e2b.Volume` sobre `VolumeStore` (síncrono)."""

    @classmethod
    def create(cls, name: str, **_opts: Any) -> Volume:
        """`VolumeStore.create(name)`; `CreateAccessPoint`, idempotente."""
        return cls._from_efs(cls._require_store().create(name))

    @classmethod
    def connect(cls, volume_id: str, **_opts: Any) -> Volume:
        return cls._from_efs(cls._require_store().get(volume_id))

    @classmethod
    def get_info(cls, volume_id: str, **_opts: Any) -> Volume:
        return cls.connect(volume_id)

    @classmethod
    def list(cls, **_opts: Any) -> builtins.list[Volume]:
        return [cls._from_efs(v) for v in cls._require_store().list() if v.name]

    @classmethod
    def destroy(cls, volume_id: str, **_opts: Any) -> bool:
        """`DeleteAccessPoint`; el directorio en sí no se borra."""
        return cls._require_store().destroy(volume_id)


class AsyncVolume(_VolumeBase):
    """`e2b.AsyncVolume`: la misma semántica que `Volume`, con corrutinas
    (el `VolumeStore` síncrono en un hilo). Sin volumen configurado, cada
    classmethod lanza **en el acto** (nunca dentro de la corrutina que
    devuelve): `AsyncVolume.create(...)` sin `await` ya lanzó, igual que el
    `Volume` síncrono y que `clients/typescript/src/e2b/volume.ts`."""

    @classmethod
    def create(cls, name: str, **_opts: Any) -> Coroutine[Any, Any, AsyncVolume]:
        return cls._in_thread(cls._require_store().create, name)

    @classmethod
    def connect(cls, volume_id: str, **_opts: Any) -> Coroutine[Any, Any, AsyncVolume]:
        return cls._in_thread(cls._require_store().get, volume_id)

    @classmethod
    def get_info(cls, volume_id: str, **_opts: Any) -> Coroutine[Any, Any, AsyncVolume]:
        return cls.connect(volume_id)

    @classmethod
    def list(cls, **_opts: Any) -> Coroutine[Any, Any, builtins.list[AsyncVolume]]:
        store = cls._require_store()
        return cls._list(store)

    @classmethod
    def destroy(cls, volume_id: str, **_opts: Any) -> Coroutine[Any, Any, bool]:
        store = cls._require_store()
        return asyncio.to_thread(store.destroy, volume_id)

    @classmethod
    async def _in_thread(cls, call: Callable[[str], EfsVolume], name: str) -> AsyncVolume:
        return cls._from_efs(await asyncio.to_thread(call, name))

    @classmethod
    async def _list(cls, store: VolumeStore) -> builtins.list[AsyncVolume]:
        volumes = await asyncio.to_thread(store.list)
        return [cls._from_efs(v) for v in volumes if v.name]


def require_volume_mount_support(
    volume_mounts: Mapping[str, Any], *, store: VolumeStore | None, template: str | None
) -> NoReturn:
    """`Sandbox.create(volume_mounts={path: Volume|nombre})`, sin I/O: la
    misma puerta que `volumes=` (`require_volume_mounts`), antes de
    cualquier llamada a AWS. Sin `volume_store=` en el cliente lanza
    `unimplemented("Volume")`, el mismo guard que `client.Volume`. Valida la
    forma (mapa no vacío; cada valor un `Volume`/`AsyncVolume` o un nombre
    válido) y nunca resuelve un nombre: mientras no haya montaje real,
    `require_volume_mounts` siempre termina en `UnimplementedError`, así que
    un `DescribeAccessPoints` sólo costaría una llamada sin cambiar el
    resultado. Cuando el montaje exista, `_launch` resolverá los nombres
    contra el store (en `asyncio.to_thread` en el shim asíncrono)."""
    if store is None:
        raise unimplemented("Volume")
    if not isinstance(volume_mounts, Mapping) or not volume_mounts:
        raise InvalidArgumentException(
            "volume_mounts espera un mapa no vacío {ruta: Volume|nombre}"
        )
    for value in volume_mounts.values():
        if isinstance(value, str):
            validate_volume_name(value)
        elif not isinstance(value, _VolumeBase):
            raise InvalidArgumentException(
                "volume_mounts espera un Volume o un nombre de texto, se recibió "
                f"{type(value).__name__}"
            )
    require_volume_mounts(
        volume_mounts.keys(),
        image_variant=resolve_image_variant(template),
        feature="volume_mounts",
    )
