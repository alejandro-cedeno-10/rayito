"""Punto de entrada de `python -m rayito.mcp` y del script `rayito-mcp`."""

from __future__ import annotations

from rayito.mcp._cli import main

if __name__ == "__main__":
    raise SystemExit(main())
