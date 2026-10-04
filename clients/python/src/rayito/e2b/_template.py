"""`Template`/`AsyncTemplate` de E2B (m15-templates): un subtipo delgado de
`rayito.Template`/`AsyncTemplate` — hereda el DSL sin copiarlo — que adapta
la firma de build de E2B (`build(template, alias, cpu_count, memory_mb,
skip_cache, on_build_logs, **opts)`) a la nativa
(`build(template, name, bucket=..., memory_mb=..., force=...)`) y añade los
cuatro métodos de etiquetado que 0.6 no implementa
(`alias_exists`/`assign_tags`/`remove_tags`/`get_tags`): lanzan
`UnimplementedError` por la tabla D14, nunca `AttributeError` (la regla de
todo este paquete: ver `_unimplemented.py`).

`bucket`, `region` y `session` llegan de la llamada o del cliente
`E2B(region=..., session=..., bucket=...)` (`_bound_params`, como
`Secret`); sin `bucket` en ninguno de los dos, `InvalidArgumentException`
nombra la opción. `memory_mb` se redondea al tamaño soportado siguiente
con `RayitoCompatWarning`; `cpu_count` no se puede fijar (la CPU sale de la
memoria, Q87) y avisa si se da.

`from_gcp_registry`, los registries con usuario/contraseña y `apt_install`
ya lanzan `UnimplementedError` desde el DSL nativo (`_templates/_dsl.py`);
el shim no necesita repetirlos.
"""

from __future__ import annotations

import warnings
from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, ClassVar, Final, NoReturn

from rayito import AsyncTemplate as _NativeAsyncTemplate
from rayito import Template as _NativeTemplate
from rayito._limits import SUPPORTED_MEMORY_MIB
from rayito.e2b._connection import ignored_param_warnings, reject_unknown_params
from rayito.e2b._unimplemented import unimplemented
from rayito.exceptions import InvalidArgumentException, RayitoCompatWarning

if TYPE_CHECKING:
    from rayito._templates._models import BuildHandle, BuildInfo

#: Opciones de Rayito que `Template.build` acepta como keyword además de
#: las de E2B; `bucket`/`region`/`session` también pueden venir del
#: cliente `E2B(...)`.
RAYITO_BUILD_OPTIONS: Final = frozenset(
    {"bucket", "region", "session", "timeout", "base_image_version", "context_dir", "force"}
)
#: Las que un `E2B(...)` vincula a `client.Template`/`client.AsyncTemplate`.
BOUND_BUILD_OPTIONS: Final = ("bucket", "region", "session")
CPU_COUNT_IGNORED: Final = (
    "cpu_count ignorado: Lambda MicroVMs deriva la CPU de memory_mb (Q87, "
    "AWS_API_NOTES.md §4); sube memory_mb para tener más CPU"
)


def resolve_memory_mb(memory_mb: int | None) -> int | None:
    """El tamaño soportado (`_limits.SUPPORTED_MEMORY_MIB`, RES-1) igual o
    inmediatamente mayor que `memory_mb`, con `RayitoCompatWarning` si hubo
    que redondear; `None` deja el default nativo. Por encima del máximo,
    `InvalidArgumentException`."""
    if memory_mb is None:
        return None
    for supported in SUPPORTED_MEMORY_MIB:
        if memory_mb <= supported:
            if memory_mb != supported:
                warnings.warn(
                    f"memory_mb={memory_mb} redondeado a {supported} (tamaños soportados: "
                    f"{', '.join(map(str, SUPPORTED_MEMORY_MIB))})",
                    RayitoCompatWarning,
                    stacklevel=4,
                )
            return supported
    raise InvalidArgumentException(
        f"memory_mb={memory_mb} supera el máximo soportado ({SUPPORTED_MEMORY_MIB[-1]} MiB, RES-1)"
    )


def _native_build_kwargs(
    bound: Mapping[str, Any],
    *,
    cpu_count: int | None,
    memory_mb: int | None,
    skip_cache: bool,
    opts: Mapping[str, Any],
    call: str,
) -> dict[str, Any]:
    """Traduce una llamada E2B a los kwargs de `rayito.Template.build`."""
    rayito = {key: value for key, value in opts.items() if key in RAYITO_BUILD_OPTIONS}
    api_params = {key: value for key, value in opts.items() if key not in RAYITO_BUILD_OPTIONS}
    reject_unknown_params(api_params, call=call)
    for message in ignored_param_warnings(api_params):
        warnings.warn(message, RayitoCompatWarning, stacklevel=4)
    if cpu_count is not None:
        warnings.warn(CPU_COUNT_IGNORED, RayitoCompatWarning, stacklevel=4)
    merged = {**{key: bound[key] for key in BOUND_BUILD_OPTIONS if key in bound}, **rayito}
    if merged.get("bucket") is None:
        raise InvalidArgumentException(
            f"Template.{call}: falta el bucket de artefactos; pasa bucket=... o crea el cliente "
            "con E2B(bucket=...)"
        )
    resolved_memory = resolve_memory_mb(memory_mb)
    if resolved_memory is not None:
        merged["memory_mb"] = resolved_memory
    if skip_cache:
        merged["force"] = True
    return merged


class _TemplateTagStubs:
    """Los cuatro métodos de etiquetado de E2B que `create`/
    `update-microvm-image` no puede dar (sólo etiquetan la imagen entera,
    no una versión): mixin compartido por `Template` y `AsyncTemplate`."""

    def alias_exists(self, *_args: Any, **_kwargs: Any) -> NoReturn:
        raise unimplemented("Template.alias_exists")

    def assign_tags(self, *_args: Any, **_kwargs: Any) -> NoReturn:
        raise unimplemented("Template.assign_tags")

    def remove_tags(self, *_args: Any, **_kwargs: Any) -> NoReturn:
        raise unimplemented("Template.remove_tags")

    def get_tags(self, *_args: Any, **_kwargs: Any) -> NoReturn:
        raise unimplemented("Template.get_tags")


def _bound_region_session(bound: Mapping[str, Any], opts: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: opts.get(key, bound.get(key))
        for key in ("region", "session")
        if opts.get(key, bound.get(key)) is not None
    }


class Template(_TemplateTagStubs, _NativeTemplate):
    """`e2b.Template`: el DSL de `rayito.Template` con la firma de build de
    E2B."""

    _bound_params: ClassVar[Mapping[str, Any]] = MappingProxyType({})

    @classmethod
    def build(  # type: ignore[override]
        cls,
        template: _NativeTemplate,
        alias: str,
        cpu_count: int | None = None,
        memory_mb: int | None = None,
        skip_cache: bool = False,
        on_build_logs: Callable[[str], None] | None = None,
        **opts: Any,
    ) -> BuildInfo:
        kwargs = _native_build_kwargs(
            cls._bound_params,
            cpu_count=cpu_count,
            memory_mb=memory_mb,
            skip_cache=skip_cache,
            opts=opts,
            call="build",
        )
        return _NativeTemplate.build(template, alias, on_build_logs=on_build_logs, **kwargs)

    @classmethod
    def build_in_background(  # type: ignore[override]
        cls,
        template: _NativeTemplate,
        alias: str,
        cpu_count: int | None = None,
        memory_mb: int | None = None,
        skip_cache: bool = False,
        **opts: Any,
    ) -> BuildHandle:
        kwargs = _native_build_kwargs(
            cls._bound_params,
            cpu_count=cpu_count,
            memory_mb=memory_mb,
            skip_cache=skip_cache,
            opts=opts,
            call="build_in_background",
        )
        kwargs.pop("timeout", None)
        return _NativeTemplate.build_in_background(template, alias, **kwargs)

    @classmethod
    def exists(cls, name: str, **opts: Any) -> bool:
        return _NativeTemplate.exists(name, **_bound_region_session(cls._bound_params, opts))


class AsyncTemplate(_TemplateTagStubs, _NativeAsyncTemplate):
    """`e2b.AsyncTemplate`: igual que `Template`, con las operaciones de
    build en versión `async` (`rayito.AsyncTemplate`)."""

    _bound_params: ClassVar[Mapping[str, Any]] = MappingProxyType({})

    @classmethod
    async def build(  # type: ignore[override]
        cls,
        template: _NativeTemplate,
        alias: str,
        cpu_count: int | None = None,
        memory_mb: int | None = None,
        skip_cache: bool = False,
        on_build_logs: Callable[[str], None] | None = None,
        **opts: Any,
    ) -> BuildInfo:
        kwargs = _native_build_kwargs(
            cls._bound_params,
            cpu_count=cpu_count,
            memory_mb=memory_mb,
            skip_cache=skip_cache,
            opts=opts,
            call="build",
        )
        return await _NativeAsyncTemplate.build(
            template, alias, on_build_logs=on_build_logs, **kwargs
        )

    @classmethod
    async def build_in_background(  # type: ignore[override]
        cls,
        template: _NativeTemplate,
        alias: str,
        cpu_count: int | None = None,
        memory_mb: int | None = None,
        skip_cache: bool = False,
        **opts: Any,
    ) -> BuildHandle:
        kwargs = _native_build_kwargs(
            cls._bound_params,
            cpu_count=cpu_count,
            memory_mb=memory_mb,
            skip_cache=skip_cache,
            opts=opts,
            call="build_in_background",
        )
        kwargs.pop("timeout", None)
        return await _NativeAsyncTemplate.build_in_background(template, alias, **kwargs)

    @classmethod
    async def exists(cls, name: str, **opts: Any) -> bool:  # type: ignore[override]
        return await _NativeAsyncTemplate.exists(
            name, **_bound_region_session(cls._bound_params, opts)
        )
