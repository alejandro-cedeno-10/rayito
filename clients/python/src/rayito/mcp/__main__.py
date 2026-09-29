"""Punto de entrada de `python -m rayito.mcp` y del script `rayito-mcp`.

Sin el extra `rayito[mcp]` instalado, importar el paquete falla con un
`ModuleNotFoundError`; aquí se atrapa (como en `rayito.cli.__main__`) para
imprimir un aviso de una línea con el comando de instalación y salir sin
traza, en vez de dejar que el `ModuleNotFoundError` suba sin capturar.
"""

from __future__ import annotations

import sys

MISSING_EXTRA_MESSAGE = (
    "rayito-mcp: el servidor MCP necesita el extra: "
    'uv pip install "rayito[mcp]" (o pip install "rayito[mcp]")'
)


def missing_mcp(exc: ModuleNotFoundError) -> bool:
    return exc.name is not None and (exc.name == "mcp" or exc.name.startswith("mcp."))


def main() -> int:
    try:
        from rayito.mcp._cli import main as run_server
    except ModuleNotFoundError as exc:
        if not missing_mcp(exc):
            raise
        print(MISSING_EXTRA_MESSAGE, file=sys.stderr)
        return 2
    return run_server()


if __name__ == "__main__":
    sys.exit(main())
