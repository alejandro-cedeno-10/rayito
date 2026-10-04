"""`e2b.E2B`: un cliente que fija opciones de conexión para todas sus
llamadas. `client.Sandbox` y `client.AsyncSandbox` son subclases del shim
con una copia de solo lectura de esas opciones en `_bound_params`; cada
classmethod (y cada instancia creada desde ellas) las mezcla bajo los
kwargs de la llamada con la regla de E2B."""

from __future__ import annotations

import warnings
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, TypeVar, Unpack

from rayito._index import DynamoDbIndex, validate_index
from rayito._volumes import VolumeStore
from rayito.e2b._async import AsyncSandbox
from rayito.e2b._connection import ApiParams, ignored_param_warnings, split_api_params
from rayito.e2b._secret import AsyncSecret, Secret
from rayito.e2b._sync import Sandbox
from rayito.e2b._template import AsyncTemplate, Template
from rayito.e2b._unimplemented import unimplemented
from rayito.e2b._volume import AsyncVolume, Volume, require_sync_store
from rayito.e2b.exceptions import RayitoCompatWarning

BoundClass = TypeVar("BoundClass", bound=type)


def bind_class(cls: BoundClass, params: Mapping[str, Any], **class_attrs: Any) -> BoundClass:
    """Una subclase de `cls` con una copia de `params` como `_bound_params`:
    cambiar después el dict del llamador no altera el vínculo. `class_attrs`
    fija además atributos de clase propios (`_bound_index`), que no se
    mezclan en cada llamada nativa como los `_bound_params`."""
    namespace = {
        "_bound_params": MappingProxyType(dict(params)),
        "__module__": cls.__module__,
        **class_attrs,
    }
    return type(cls.__name__, (cls,), namespace)  # type: ignore[return-value]


class E2B:
    """`E2B(region=..., control_plane=..., headers=...)`: `client.Sandbox` y
    `client.AsyncSandbox` usan esas opciones salvo que la llamada dé otras
    (un `None` de la llamada cae al del cliente; `headers` de la llamada
    sustituyen a las del cliente). Los `ApiParams` ignorados avisan una sola
    vez, aquí. `client.Secret`/`client.AsyncSecret` usan su `region` y su
    `session` (Secrets Manager en esa cuenta).

    `index=DynamoDbIndex(...)` (extensión de Rayito, `None` por defecto) lo
    usan `client.Sandbox.list` y `client.AsyncSandbox.list` cuando la
    llamada no pasa otro: `query.metadata` sobre sandboxes en pausa con
    `dynamodb:BatchGetItem` (ver `rayito.DynamoDbIndex`, "Coste y
    activación"). Ninguna otra llamada lo usa.

    `client.Template`/`client.AsyncTemplate` (m15-templates) usan su
    `region`, su `session` y `bucket=` (extensión de Rayito: el bucket de
    artefactos de `Template.build`, `None` por defecto; sin él cada
    `build` debe pasar `bucket=`). `client.Volume`/`client.AsyncVolume`
    (m15-efs-volumes, experimental) sólo funcionan con
    `volume_store=VolumeStore(...)` — sin él, `UnimplementedError`."""

    def __init__(
        self,
        *,
        region: str | None = None,
        session: Any | None = None,
        control_plane: Any | None = None,
        index: DynamoDbIndex | None = None,
        bucket: str | None = None,
        volume_store: VolumeStore | None = None,
        **api_params: Unpack[ApiParams],
    ) -> None:
        split_api_params(api_params, call="E2B")
        for message in ignored_param_warnings(api_params):
            warnings.warn(message, RayitoCompatWarning, stacklevel=2)
        applied = {
            key: value
            for key, value in api_params.items()
            if key in ("request_timeout", "retries", "headers", "proxy")
        }
        params = {"region": region, "session": session, "control_plane": control_plane, **applied}
        bound = {key: value for key, value in params.items() if value is not None}
        chosen_index = validate_index(index)
        volume_store = require_sync_store(volume_store)
        self.Sandbox: type[Sandbox] = bind_class(
            Sandbox, bound, _bound_index=chosen_index, _bound_volume_store=volume_store
        )
        self.AsyncSandbox: type[AsyncSandbox] = bind_class(
            AsyncSandbox, bound, _bound_index=chosen_index, _bound_volume_store=volume_store
        )
        secret_bound: dict[str, Any] = {
            key: bound[key] for key in ("region", "session") if key in bound
        }
        self.Secret: type[Secret] = bind_class(Secret, secret_bound)
        self.AsyncSecret: type[AsyncSecret] = bind_class(AsyncSecret, secret_bound)
        template_bound = {**secret_bound, **({"bucket": bucket} if bucket is not None else {})}
        self.Template: type[Template] = bind_class(Template, template_bound)
        self.AsyncTemplate: type[AsyncTemplate] = bind_class(AsyncTemplate, template_bound)
        self._volume_store = volume_store

    @property
    def Volume(self) -> type[Volume]:
        """`UnimplementedError("Volume")` sin `volume_store=` en el
        constructor; si no, una subclase de `Volume` ligada a él."""
        if self._volume_store is None:
            raise unimplemented("Volume")
        return bind_class(Volume, {}, _bound_store=self._volume_store)

    @property
    def AsyncVolume(self) -> type[AsyncVolume]:
        if self._volume_store is None:
            raise unimplemented("Volume")
        return bind_class(AsyncVolume, {}, _bound_store=self._volume_store)
