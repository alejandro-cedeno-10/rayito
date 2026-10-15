"""Genera `testdata/stacks/cost-statements.json`: el texto de coste de los
componentes del panel (qué crea, coste en reposo y por uso, retirada, fuente y
descripción de los parámetros con coste), tal como lo declara Python. Es la
referencia que comprueba el test de paridad de ambos SDK, para que los textos
de coste de Python y de TypeScript no se separen.

Uso (desde `clients/python`):

    uv run python ../../scripts/gen_stack_cost_statements.py          # escribe
    uv run python ../../scripts/gen_stack_cost_statements.py --check  # comprueba
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from rayito._stacks._registry import component_by_name

REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT = REPO_ROOT / "testdata" / "stacks" / "cost-statements.json"
#: Los componentes del panel cuyo texto de coste se mantiene idéntico en ambos SDK.
DASHBOARD_COMPONENTS = (
    "dashboard",
    "dashboard-cloudwatch",
    "dashboard-read-role",
    "quota-alerts",
)
#: Su descripción cambia a propósito entre SDK (`artifact_bucket=` frente a
#: `artifactBucket`), así que no entra en la comparación.
SKIPPED_PARAMETERS = frozenset({"ArtifactBucket"})


def cost_statements() -> dict[str, Any]:
    document: dict[str, Any] = {}
    for name in DASHBOARD_COMPONENTS:
        component = component_by_name(name)
        assert component is not None, name
        document[name] = {
            "creates": list(component.cost.creates),
            "idleMonthly": component.cost.idle_monthly,
            "perUse": list(component.cost.per_use),
            "removal": component.cost.removal,
            "source": component.cost.source,
            "parameters": {
                parameter.name: parameter.description
                for parameter in component.parameters
                if parameter.name not in SKIPPED_PARAMETERS
            },
        }
    return document


def render() -> str:
    return json.dumps(cost_statements(), ensure_ascii=False, indent=2) + "\n"


def main(argv: list[str]) -> int:
    expected = render()
    if "--check" in argv:
        if OUTPUT.read_text(encoding="utf-8") != expected:
            print(f"{OUTPUT.name} desactualizado: ejecuta gen_stack_cost_statements.py")
            return 1
        return 0
    OUTPUT.write_text(expected, encoding="utf-8")
    print(f"gen_stack_cost_statements: escrito {OUTPUT.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
