"""Fixture para `require_module`: un paquete que SÍ existe pero que, al
importarse, falla por una dependencia interna suya que falta (no por el
propio paquete). Sirve para comprobar que `require_module` no confunde ese
`ModuleNotFoundError` con "falta el extra"."""

import optional_broken_pkg.this_nested_dependency_does_not_exist  # type: ignore[import-not-found]  # noqa: F401
