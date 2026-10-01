"""`Template`/`AsyncTemplate` de E2B (m15-templates): la API Python de E2B
nombra sus métodos de build igual que la nativa de Rayito (`build`,
`build_in_background`, `get_build_status`, `exists`, `to_json`,
`to_dockerfile`), así que el shim es un subtipo delgado de
`rayito.Template`/`AsyncTemplate` — hereda el DSL y el pipeline de build
sin copiarlos — que sólo añade los cuatro métodos de etiquetado que 0.6 no
implementa (`alias_exists`/`assign_tags`/`remove_tags`/`get_tags`): lanzan
`UnimplementedError` por la tabla D14, nunca `AttributeError` (la regla de
todo este paquete: ver `_unimplemented.py`).

`from_gcp_registry`, los registries con usuario/contraseña y `apt_install`
ya lanzan `UnimplementedError` desde el DSL nativo (`_templates/_dsl.py`);
el shim no necesita repetirlos.
"""

from __future__ import annotations

from typing import Any, NoReturn

from rayito import AsyncTemplate as _NativeAsyncTemplate
from rayito import Template as _NativeTemplate
from rayito.e2b._unimplemented import unimplemented


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


class Template(_TemplateTagStubs, _NativeTemplate):
    """`e2b.Template`: el builder y `build()`/`build_in_background()`/
    `get_build_status()`/`exists()` son los de `rayito.Template`."""


class AsyncTemplate(_TemplateTagStubs, _NativeAsyncTemplate):
    """`e2b.AsyncTemplate`: igual que `Template`, con las cuatro
    operaciones de build en versión `async` (`rayito.AsyncTemplate`)."""
