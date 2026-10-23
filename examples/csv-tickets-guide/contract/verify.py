"""Contrato determinista del caso: recuento y media de horas por categoría.

Sólo biblioteca estándar, sin AWS ni pandas. Se usa de dos formas:

    python3 contract/verify.py     # recalcula desde data/tickets.csv
    import verify                  # los ejemplos usan compute, expected y compare

Sale con código 1 si el CSV no cumple el contrato. Las columnas del CSV
(`categoria`, `horas`) son datos del caso y se quedan en español.
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path
from typing import TypedDict, cast

ROOT = Path(__file__).resolve().parents[1]
CSV_PATH = ROOT / "data" / "tickets.csv"
EXPECTED_PATH = ROOT / "contract" / "expected.json"
CATEGORY_COLUMN = "categoria"
HOURS_COLUMN = "horas"


class CategorySummary(TypedDict):
    """Recuento y media de horas de una categoría."""

    tickets: int
    mean_hours: float


class Contract(TypedDict):
    """El resultado esperado, tal como está en `expected.json`."""

    total: int
    categories: dict[str, CategorySummary]
    mean_tolerance: float


Summary = dict[str, CategorySummary]


def compute(csv_text: str) -> Summary:
    """`{categoria: {"tickets": n, "mean_hours": m}}` a partir del texto del CSV."""
    groups: dict[str, list[float]] = {}
    for row in csv.DictReader(csv_text.splitlines()):
        groups.setdefault(row[CATEGORY_COLUMN], []).append(float(row[HOURS_COLUMN]))
    return {
        category: {"tickets": len(hours), "mean_hours": sum(hours) / len(hours)}
        for category, hours in sorted(groups.items())
    }


def expected() -> Contract:
    """Lee `contract/expected.json`."""
    return cast(Contract, json.loads(EXPECTED_PATH.read_text(encoding="utf-8")))


def compare(actual: Summary) -> list[str]:
    """Lista de diferencias frente a `expected.json` (vacía si cumple)."""
    contract = expected()
    tolerance = contract["mean_tolerance"]
    errors: list[str] = []
    if set(actual) != set(contract["categories"]):
        errors.append(f"categorías {sorted(actual)} != {sorted(contract['categories'])}")
    for category, want in contract["categories"].items():
        got = actual.get(category)
        if got is None:
            continue
        if int(got["tickets"]) != want["tickets"]:
            errors.append(f"{category}: {got['tickets']} tickets, se esperaban {want['tickets']}")
        if abs(float(got["mean_hours"]) - want["mean_hours"]) > tolerance:
            errors.append(f"{category}: media {got['mean_hours']:.4f} h, se esperaba {want['mean_hours']} h")
    total = sum(int(summary["tickets"]) for summary in actual.values())
    if total != contract["total"]:
        errors.append(f"total {total}, se esperaba {contract['total']}")
    return errors


def main() -> int:
    """Recalcula el resumen desde el CSV y lo compara con el contrato."""
    actual = compute(CSV_PATH.read_text(encoding="utf-8"))
    for category, summary in actual.items():
        print(f"{category:<12} tickets={summary['tickets']}  media_horas={summary['mean_hours']:.4f}")
    errors = compare(actual)
    if errors:
        print("NO cumple el contrato:", *errors, sep="\n  ")
        return 1
    print(f"cumple el contrato: {sum(summary['tickets'] for summary in actual.values())} tickets")
    return 0


if __name__ == "__main__":
    sys.exit(main())
