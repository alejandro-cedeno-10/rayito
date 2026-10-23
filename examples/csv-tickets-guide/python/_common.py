"""Valores compartidos por los ejemplos de Python. Ningún secreto vive aquí.

`verify` (contract/verify.py) se importa después de añadir `contract/` a
`sys.path`, para que los scripts funcionen sin instalar nada más."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from rayito import Execution, Sandbox

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "contract"))

import verify  # noqa: E402

TEMPLATE = os.environ.get("RAYITO_TEMPLATE", "rayito-base")
METADATA = {"app": os.environ.get("RAYITO_APP_TAG", "tickets-demo")}
LOCAL_CSV = ROOT / "data" / "tickets.csv"
SANDBOX_CSV = "/home/user/tickets.csv"
CELL_TIMEOUT_SECONDS = 60.0
"""Plazo de cada celda del camino feliz; el CSV del caso tiene siete filas."""

__all__ = [
    "CELL_TIMEOUT_SECONDS",
    "LOCAL_CSV",
    "METADATA",
    "ROOT",
    "SANDBOX_CSV",
    "TEMPLATE",
    "CellFailedException",
    "run_cell",
    "verify",
]


class CellFailedException(RuntimeError):
    """Una celda que tenía que funcionar devolvió `execution.error`."""


def run_cell(sbx: Sandbox, code: str, step: str, *, timeout_seconds: float = CELL_TIMEOUT_SECONDS) -> Execution:
    """Ejecuta una celda y se detiene con un mensaje útil si el código falla.

    Un error del código del sandbox no lanza excepción en el SDK: llega como
    dato en `execution.error`. Aquí lo convertimos en excepción porque estas
    celdas forman parte del camino feliz."""
    execution = sbx.run_code(code, timeout=timeout_seconds)
    if execution.error:
        error = execution.error
        raise CellFailedException(f"falló el paso «{step}»: {error.name}: {error.value}")
    return execution
