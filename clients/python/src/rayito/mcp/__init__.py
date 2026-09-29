"""Servidor MCP de Rayito (`python -m rayito.mcp` / `rayito-mcp`).

Requiere el extra `rayito[mcp]` (el SDK oficial `mcp` 2.x). Los cuatro
nombres del paquete se resuelven con `__getattr__` (PEP 562) en vez de un
`import` en el cuerpo del módulo: así, sin el extra instalado, `import
rayito.mcp` (y por tanto `python -m rayito.mcp` / el script `rayito-mcp`,
que importan `rayito.mcp` como paquete padre antes de ejecutar su propio
código) no falla al cargar el paquete. Sólo acceder a uno de estos nombres
sin el extra falla, con el comando de instalación en el mensaje.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rayito.mcp._cli import main
    from rayito.mcp._lease import SandboxLease
    from rayito.mcp._server import build_server
    from rayito.mcp._settings import McpSettings

__all__ = ["McpSettings", "SandboxLease", "build_server", "main"]

_EXPORTS: dict[str, tuple[str, str]] = {
    "McpSettings": ("rayito.mcp._settings", "McpSettings"),
    "SandboxLease": ("rayito.mcp._lease", "SandboxLease"),
    "build_server": ("rayito.mcp._server", "build_server"),
    "main": ("rayito.mcp._cli", "main"),
}


def __getattr__(name: str) -> object:
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attr_name = target
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name != "mcp" and not str(exc.name).startswith("mcp."):
            raise
        raise ModuleNotFoundError(
            "rayito.mcp necesita el extra 'mcp': pip install \"rayito[mcp]\"", name="mcp"
        ) from exc
    return getattr(module, attr_name)


def __dir__() -> list[str]:
    return sorted({*__all__, *globals()})
