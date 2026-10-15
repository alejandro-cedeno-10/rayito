"""Coste cero del panel de gestión (ADR-026, decisión D10): sin llamar a un
método no hay ni un cliente de AWS ni una llamada; nada de Cost Explorer
(que cobra por petición) se toca salvo con `UsageCost.billed()`; y
`import rayito` no carga el panel."""

from __future__ import annotations

import subprocess
import sys
from typing import Any

import boto3
import pytest

import rayito
from rayito import AsyncFleet, AsyncQuotas, AsyncUsageCost, Fleet, Quotas, UsageCost

from .fake_dashboard import FakeCostSource, FakePlane, FakeQuotaSource, applied_defaults, list_item

pytestmark = pytest.mark.usefixtures("clean_catalog")


@pytest.fixture
def created_clients(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    services: list[str] = []
    original = boto3.session.Session.client

    def recording_client(self: Any, service_name: str, *args: Any, **kwargs: Any) -> Any:
        services.append(service_name)
        return original(self, service_name, *args, **kwargs)

    monkeypatch.setattr(boto3.session.Session, "client", recording_client)
    return services


def test_building_every_service_creates_no_aws_client(created_clients: list[str]) -> None:
    for service in (Quotas, UsageCost, Fleet, AsyncQuotas, AsyncUsageCost, AsyncFleet):
        service()
    assert created_clients == []


def test_reading_quotas_the_fleet_or_estimates_never_touches_cost_explorer() -> None:
    plane = FakePlane([list_item(1)])
    cost_source = FakeCostSource(None)  # type: ignore[arg-type]
    Quotas(control_plane=plane, source=FakeQuotaSource(applied_defaults())).report()
    Fleet(control_plane=plane).sandboxes()
    UsageCost(control_plane=plane, source=cost_source).estimate()
    assert cost_source.queries == []


def test_the_default_services_never_create_a_cost_explorer_client_unless_billed(
    created_clients: list[str],
) -> None:
    plane = FakePlane([list_item(1)])
    usage = UsageCost(control_plane=plane)
    usage.estimate()
    assert "ce" not in created_clients
    Quotas(control_plane=plane, source=FakeQuotaSource(applied_defaults())).report()
    assert "ce" not in created_clients


def test_no_action_runs_by_default() -> None:
    plane = FakePlane([list_item(1)])
    service = Fleet(control_plane=plane)
    with pytest.raises(rayito.InvalidArgumentException):
        service.run_bulk("kill", ["microvm-1"])
    assert [name for name in plane.operations() if name.endswith("_microvm")] == []


def test_import_rayito_does_not_load_the_dashboard() -> None:
    probe = (
        "import json, sys, rayito; "
        "print(json.dumps(sorted(m for m in sys.modules if m.startswith('rayito._dashboard'))))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "[]"
