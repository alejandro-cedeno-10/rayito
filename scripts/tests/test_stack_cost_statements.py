"""El texto de coste de los componentes del panel es el mismo en Python y en
TypeScript: este test fija el fichero de referencia contra Python y el test de
TypeScript (`stack-cost-statements.test.ts`) lo fija contra el suyo."""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

import gen_stack_cost_statements as generator


def test_the_reference_file_matches_the_python_components() -> None:
    assert generator.OUTPUT.read_text(encoding="utf-8") == generator.render()


def test_every_dashboard_component_states_what_it_costs() -> None:
    for name, statement in generator.cost_statements().items():
        assert statement["creates"], name
        assert statement["idleMonthly"], name
        assert statement["source"], name
