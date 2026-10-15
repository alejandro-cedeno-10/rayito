"""`LifecycleEvents` contra AWS real, sólo lectura (espejo de
`clients/typescript/tests/e2e/lifecycle-events.e2e.test.ts`): sobre una pila
`events-webhooks` YA desplegada, `status()` la describe y `list_webhooks()` y
`get_events()` leen su tabla. Nunca despliega, registra ni borra nada y no
lanza ningún sandbox.

Necesita `RAYITO_E2E=1`, `RAYITO_TEMPLATE` y `RAYITO_E2E_EVENTS_STACK` (el
nombre de la pila desplegada); sin la última se salta. Nunca imprime ids,
URLs ni nombres de recursos: sólo estados y cuentas. Coste: una
`DescribeStacks` y dos consultas DynamoDB (< $0,00001).
"""

from __future__ import annotations

import os
from typing import Final

import pytest

from rayito import LifecycleEvents

from .conftest import E2ESettings

pytestmark = pytest.mark.e2e

STACK_VAR: Final = "RAYITO_E2E_EVENTS_STACK"
READY_STATES: Final = frozenset({"CREATE_COMPLETE", "UPDATE_COMPLETE", "UPDATE_ROLLBACK_COMPLETE"})
TABLE_OUTPUT: Final = "EventsTableName"
KEY_OUTPUT: Final = "StackKeySecretArn"
SAMPLE_EVENTS: Final = 1


@pytest.fixture
def events(e2e_settings: E2ESettings) -> LifecycleEvents:
    stack = os.environ.get(STACK_VAR)
    if not stack:
        pytest.skip(f"exporta {STACK_VAR}=<nombre de la pila events-webhooks desplegada>")
    return LifecycleEvents(stack_name=stack, region=e2e_settings.region)


def test_status_describes_the_deployed_stack_and_exposes_its_outputs(
    events: LifecycleEvents,
) -> None:
    status = events.status()
    assert status is not None
    assert status.state in READY_STATES
    assert {TABLE_OUTPUT, KEY_OUTPUT} <= set(status.outputs)


def test_list_webhooks_and_get_events_read_the_table_without_changing_it(
    events: LifecycleEvents,
) -> None:
    webhooks = events.list_webhooks()
    records = events.get_events(limit=SAMPLE_EVENTS)
    print(f"\n[lifecycle-events e2e] webhooks: {len(webhooks)}, eventos leídos: {len(records)}")
    assert isinstance(webhooks, list)
    assert len(records) <= SAMPLE_EVENTS
