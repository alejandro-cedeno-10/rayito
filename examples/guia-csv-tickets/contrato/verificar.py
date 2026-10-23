"""Contrato determinista del caso: recuento y media de horas por categoría.

Sólo biblioteca estándar, sin AWS ni pandas. Se usa de dos formas:

    python3 contrato/verificar.py            # recalcula desde datos/tickets.csv
    import verificar                         # los ejemplos usan calcular/comparar/esperado

Sale con código 1 si el CSV no cumple el contrato.
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CSV_PATH = ROOT / "datos" / "tickets.csv"
EXPECTED_PATH = ROOT / "contrato" / "esperado.json"


def calcular(csv_text: str) -> dict[str, dict[str, float]]:
    """{categoria: {"tickets": n, "media_horas": m}} a partir del texto del CSV."""
    grupos: dict[str, list[float]] = {}
    for fila in csv.DictReader(csv_text.splitlines()):
        grupos.setdefault(fila["categoria"], []).append(float(fila["horas"]))
    return {
        cat: {"tickets": len(horas), "media_horas": sum(horas) / len(horas)} for cat, horas in sorted(grupos.items())
    }


def esperado() -> dict:
    return json.loads(EXPECTED_PATH.read_text(encoding="utf-8"))


def comparar(obtenido: dict[str, dict[str, float]]) -> list[str]:
    """Lista de diferencias frente a esperado.json (vacía si cumple)."""
    ref = esperado()
    tol = ref["tolerancia_media"]
    errores: list[str] = []
    if set(obtenido) != set(ref["categorias"]):
        errores.append(f"categorías {sorted(obtenido)} != {sorted(ref['categorias'])}")
    for cat, valores in ref["categorias"].items():
        got = obtenido.get(cat)
        if got is None:
            continue
        if int(got["tickets"]) != valores["tickets"]:
            errores.append(f"{cat}: {got['tickets']} tickets, se esperaban {valores['tickets']}")
        if abs(float(got["media_horas"]) - valores["media_horas"]) > tol:
            errores.append(f"{cat}: media {got['media_horas']:.4f} h, se esperaba {valores['media_horas']} h")
    total = sum(int(v["tickets"]) for v in obtenido.values())
    if total != ref["total"]:
        errores.append(f"total {total}, se esperaba {ref['total']}")
    return errores


def main() -> int:
    obtenido = calcular(CSV_PATH.read_text(encoding="utf-8"))
    for cat, v in obtenido.items():
        print(f"{cat:<12} tickets={v['tickets']}  media_horas={v['media_horas']:.4f}")
    errores = comparar(obtenido)
    if errores:
        print("NO cumple el contrato:", *errores, sep="\n  ")
        return 1
    print(f"cumple el contrato: {sum(v['tickets'] for v in obtenido.values())} tickets")
    return 0


if __name__ == "__main__":
    sys.exit(main())
