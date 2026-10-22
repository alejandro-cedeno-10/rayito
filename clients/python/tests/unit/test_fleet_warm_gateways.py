"""Una plaza aparcada abre las pasarelas del proyecto (ADR-034 D6, design
D4): la flota reconectada en otro proceso, al abrir una plaza que nadie
sirvió, aplica `gateways` con el mismo camino que `SandboxPool.take` antes de
arrancar el host; si no puede, la sustituye. Sobre dobles, sin AWS."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from rayito._agent._fleet import AgentFleet
from rayito._agent._fleet_async import AsyncAgentFleet
from rayito._pool_base import gateway_sections
from rayito.exceptions import SandboxException
from rayito.sandbox_async.pool import apply_gateways as apply_gateways_async
from rayito.sandbox_sync.pool import apply_gateways
from tests.unit.fake_agent_fleet import FLEET_TOKEN, FleetFlavor
from tests.unit.fake_agent_project import project


@pytest.fixture(params=["sync", "async"])
def flavor(request: pytest.FixtureRequest) -> FleetFlavor:
    return FleetFlavor(request.param)


def reconnect(flavor: FleetFlavor) -> Any:
    fleet_class: Any = AsyncAgentFleet if flavor.asynchronous else AgentFleet
    factory = flavor.backend.async_factory if flavor.asynchronous else flavor.backend.factory
    return flavor.call(
        fleet_class.connect(
            "support-bot",
            access_token=FLEET_TOKEN,
            project=project(),
            directory=flavor.classes_directory(),
            transport_factory=factory,
            service=flavor.service(project()),
            clock=flavor.clock,
            sleep=flavor.clock.asleep if flavor.asynchronous else flavor.clock.sleep,
        )
    )


def test_a_parked_slot_gets_the_project_gateways_before_the_host_starts(
    flavor: FleetFlavor,
) -> None:
    parked = flavor.classes.add(served=False, state="SUSPENDED")
    fleet = reconnect(flavor)

    result = flavor.call(fleet.invoke("hola"))

    assert result.text == "respuesta a hola"
    assert len(parked.configured) == 1
    assert len(parked.configured[0]) == len(gateway_sections(dict(project().gateways)))
    assert parked.served


def test_a_served_vm_is_not_configured_again(flavor: FleetFlavor) -> None:
    served = flavor.classes.add()
    fleet = reconnect(flavor)

    flavor.call(fleet.invoke("hola"))

    assert served.configured == []


def test_a_parked_slot_without_gateways_is_replaced(flavor: FleetFlavor) -> None:
    parked = flavor.classes.add(served=False, state="SUSPENDED")
    parked.configure_error = SandboxException("ConfigureSandbox falló")
    fleet = reconnect(flavor)

    result = flavor.call(fleet.invoke("hola"))

    assert result.text == "respuesta a hola"
    assert parked.state == "TERMINATED"
    assert parked.sandbox_id not in fleet.sandbox_ids
    assert len(flavor.classes.created) == 1


class RecordingSandbox:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def _apply_configure_sections(
        self, planned: Any, *, timeout: float, terminate_on_failure: bool
    ) -> None:
        self.calls.append(
            {"sections": tuple(planned), "timeout": timeout, "terminate": terminate_on_failure}
        )


class AsyncRecordingSandbox(RecordingSandbox):
    async def _apply_configure_sections(  # type: ignore[override]
        self, planned: Any, *, timeout: float, terminate_on_failure: bool
    ) -> None:
        RecordingSandbox._apply_configure_sections(
            self, planned, timeout=timeout, terminate_on_failure=terminate_on_failure
        )


def test_apply_gateways_keeps_the_sandbox_on_failure_in_both_surfaces() -> None:
    gateways = dict(project().gateways)
    sync_sandbox = RecordingSandbox()
    async_sandbox = AsyncRecordingSandbox()

    apply_gateways(sync_sandbox, gateways, timeout=5.0)  # type: ignore[arg-type]
    asyncio.run(apply_gateways_async(async_sandbox, gateways, timeout=5.0))  # type: ignore[arg-type]

    for sandbox in (sync_sandbox, async_sandbox):
        assert sandbox.calls == [
            {"sections": gateway_sections(gateways), "timeout": 5.0, "terminate": False}
        ]


def test_no_gateways_plan_no_section() -> None:
    assert gateway_sections(None) == ()
