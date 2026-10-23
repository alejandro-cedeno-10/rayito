"""Valores compartidos por los ejemplos de Python. Ningún secreto vive aquí."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from rayito import Execution, Sandbox

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "contrato"))

import verificar  # noqa: E402  (contrato/verificar.py; va después de ajustar sys.path)

TEMPLATE = os.environ.get("RAYITO_TEMPLATE", "rayito-base")
METADATA = {"app": os.environ.get("RAYITO_APP_TAG", "tickets-demo")}
CSV_LOCAL = ROOT / "datos" / "tickets.csv"
CSV_SANDBOX = "/home/user/tickets.csv"

__all__ = ["CSV_LOCAL", "CSV_SANDBOX", "METADATA", "ROOT", "TEMPLATE", "CeldaFallida", "run_cell", "verificar"]


class CeldaFallida(RuntimeError):
    """Una celda que tenía que funcionar devolvió `execution.error`."""


def run_cell(sbx: Sandbox, code: str, paso: str, *, timeout: float = 60) -> Execution:
    """Ejecuta una celda y se detiene con un mensaje útil si el código falla.

    Un error del código del sandbox no lanza excepción en el SDK: llega como
    dato en `execution.error`. Aquí lo convertimos en excepción porque estas
    celdas forman parte del camino feliz."""
    execution = sbx.run_code(code, timeout=timeout)
    if execution.error:
        err = execution.error
        raise CeldaFallida(f"falló el paso «{paso}»: {err.name}: {err.value}")
    return execution
