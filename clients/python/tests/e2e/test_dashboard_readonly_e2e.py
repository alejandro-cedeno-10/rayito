"""Panel de gestión (ADR-026) contra AWS real, de sólo lectura: `Quotas`,
`UsageCost` y `Fleet` leen la cuenta sin crear ni cambiar nada (sin sandboxes
nuevos, sin acciones). Necesita `RAYITO_E2E=1` y `RAYITO_TEMPLATE` como el resto
de los e2e; el coste facturado, que Cost Explorer cobra por petición, sólo se
lee con `RAYITO_E2E_BILLED_COST=1` (cada fallo de caché cuesta $0,01 en
us-east-1: una petición con la ventana por defecto).

Coste: $0 sin `RAYITO_E2E_BILLED_COST`; con ella, una petición de Cost Explorer
(~$0,01). Las lecturas de Service Quotas, `ListMicrovms`, `ListMicrovmImages`,
`ListMicrovmImageVersions` y `GetMicrovmImageVersion` no se facturan.
"""

from __future__ import annotations

import os

import pytest

from rayito import Fleet, Quotas, UsageCost
from rayito._aws import LambdaMicrovmsControlPlane
from rayito.exceptions import InvalidArgumentException

from .conftest import E2ESettings

pytestmark = pytest.mark.e2e

BILLED_COST_VAR = "RAYITO_E2E_BILLED_COST"
TERMINAL_STATE = "TERMINATED"
PLANNED_COUNT = 3


def test_quotas_report_reads_the_applied_quotas_and_the_memory_usage(
    control_plane: LambdaMicrovmsControlPlane,
) -> None:
    report = Quotas(region=control_plane.region, control_plane=control_plane).report()

    assert report.region == control_plane.region
    assert report.entries
    assert all(entry.increase_url.startswith("https://") for entry in report.entries)
    assert report.memory.allocated_gb >= 0
    assert 0 <= report.memory.unresolved <= report.memory.sandboxes
    assert report.memory.level in {"ok", "warning", "critical"}


def test_usage_cost_estimates_for_free(
    control_plane: LambdaMicrovmsControlPlane,
) -> None:
    cost = UsageCost(region=control_plane.region, control_plane=control_plane)

    estimate = cost.estimate()
    assert estimate.label == "estimated"
    assert estimate.total_usd >= 0
    live = {item.sandbox_id for item in control_plane.list_microvms()}
    assert {item.sandbox_id for item in estimate.items} <= live


@pytest.mark.skipif(
    os.environ.get(BILLED_COST_VAR) != "1",
    reason=f"el coste facturado cuesta por petición: exporta {BILLED_COST_VAR}=1",
)
def test_usage_cost_bills_only_behind_the_opt_in(
    control_plane: LambdaMicrovmsControlPlane,
) -> None:
    cost = UsageCost(region=control_plane.region, control_plane=control_plane)

    billed = cost.billed()
    assert billed.label == "billed"
    assert billed.requests >= 1
    assert billed.total_usd >= 0
    assert cost.billed() is billed


def test_fleet_lists_sandboxes_images_and_versions_and_refuses_actions_by_default(
    e2e_settings: E2ESettings,
    control_plane: LambdaMicrovmsControlPlane,
    template_arn: str,
) -> None:
    fleet = Fleet(region=control_plane.region, control_plane=control_plane)

    assert fleet.allow_actions is False
    sandboxes = fleet.sandboxes()
    assert all(view.state != TERMINAL_STATE for view in sandboxes)
    of_template = fleet.sandboxes(image=template_arn)
    assert {view.sandbox_id for view in of_template} <= {view.sandbox_id for view in sandboxes}

    images = fleet.images()
    assert template_arn in {image.image_arn for image in images}
    versions = fleet.image_versions(e2e_settings.template)
    assert versions
    assert fleet.plan_bulk("kill", PLANNED_COUNT).count == PLANNED_COUNT
    with pytest.raises(InvalidArgumentException):
        fleet.run_bulk("kill", [view.sandbox_id for view in of_template])
