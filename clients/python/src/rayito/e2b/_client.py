"""`e2b.E2B`: un cliente que fija opciones de conexión para todas sus
llamadas. `client.Sandbox` y `client.AsyncSandbox` son subclases del shim
con una copia de solo lectura de esas opciones en `_bound_params`; cada
classmethod (y cada instancia creada desde ellas) las mezcla bajo los
kwargs de la llamada con la regla de E2B."""

from __future__ import annotations

import warnings
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, NoReturn, TypeVar, Unpack

from rayito.e2b._async import AsyncSandbox
from rayito.e2b._connection import ApiParams, ignored_param_warnings, split_api_params
from rayito.e2b._sync import Sandbox
from rayito.e2b._unimplemented import unimplemented
from rayito.e2b.exceptions import RayitoCompatWarning

BoundClass = TypeVar("BoundClass", bound=type)


def bind_class(cls: BoundClass, params: Mapping[str, Any]) -> BoundClass:
    """Una subclase de `cls` con una copia de `params` como `_bound_params`:
    cambiar después el dict del llamador no altera el vínculo."""
    namespace = {"_bound_params": MappingProxyType(dict(params)), "__module__": cls.__module__}
    return type(cls.__name__, (cls,), namespace)  # type: ignore[return-value]


class E2B:
    """`E2B(region=..., control_plane=..., headers=...)`: `client.Sandbox` y
    `client.AsyncSandbox` usan esas opciones salvo que la llamada dé otras
    (un `None` de la llamada cae al del cliente; `headers` de la llamada
    sustituyen a las del cliente). Los `ApiParams` ignorados avisan una sola
    vez, aquí. `Template`, `Volume` y `Secret` (y sus `Async*`) son
    `UnimplementedError`."""

    def __init__(
        self,
        *,
        region: str | None = None,
        session: Any | None = None,
        control_plane: Any | None = None,
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
        self.Sandbox: type[Sandbox] = bind_class(Sandbox, bound)
        self.AsyncSandbox: type[AsyncSandbox] = bind_class(AsyncSandbox, bound)

    @property
    def Template(self) -> NoReturn:
        raise unimplemented("Template")

    @property
    def AsyncTemplate(self) -> NoReturn:
        raise unimplemented("Template")

    @property
    def Volume(self) -> NoReturn:
        raise unimplemented("Volume")

    @property
    def AsyncVolume(self) -> NoReturn:
        raise unimplemented("Volume")

    @property
    def Secret(self) -> NoReturn:
        raise unimplemented("Secret")

    @property
    def AsyncSecret(self) -> NoReturn:
        raise unimplemented("Secret")
