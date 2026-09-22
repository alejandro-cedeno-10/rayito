"""Servidor MCP de Rayito (`python -m rayito.mcp` / `rayito-mcp`).

Requiere el extra `rayito[mcp]` (el SDK oficial `mcp` 2.x); sin él la
importación falla con el comando de instalación en el mensaje.
"""

from __future__ import annotations

try:
    from rayito.mcp._cli import main
    from rayito.mcp._lease import SandboxLease
    from rayito.mcp._server import build_server
    from rayito.mcp._settings import McpSettings
except ModuleNotFoundError as exc:
    if exc.name != "mcp" and not str(exc.name).startswith("mcp."):
        raise
    raise ModuleNotFoundError(
        "rayito.mcp necesita el extra 'mcp': pip install \"rayito[mcp]\""
    ) from exc

__all__ = ["McpSettings", "SandboxLease", "build_server", "main"]
