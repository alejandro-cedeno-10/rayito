"""Claridad de coste del panel: cada clase y método público de `Quotas`,
`UsageCost` y `Fleet` (y sus gemelas async) dice en su docstring si es
gratuito o de pago, y `resume` avisa de que vuelve a facturar cómputo."""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from rayito import AsyncFleet, AsyncQuotas, AsyncUsageCost, Fleet, Quotas, UsageCost

from .fake_dashboard import FakeCostSource, FakePlane, FakeQuotaSource, applied_defaults, list_item

pytestmark = pytest.mark.usefixtures("clean_catalog")

COST_BLOCK_MARKER = "Coste y activación"
FREE_WORD = "Gratuita"
PAID_WORD = "De pago"
CLASSES = (Quotas, UsageCost, Fleet, AsyncQuotas, AsyncUsageCost, AsyncFleet)
FREE_METHODS = (
    (Quotas, "report"),
    (UsageCost, "estimate"),
    (Fleet, "sandboxes"),
    (Fleet, "images"),
    (Fleet, "image_versions"),
    (Fleet, "plan_bulk"),
    (AsyncQuotas, "report"),
    (AsyncUsageCost, "estimate"),
    (AsyncFleet, "sandboxes"),
    (AsyncFleet, "images"),
    (AsyncFleet, "image_versions"),
    (AsyncFleet, "plan_bulk"),
)
PAID_METHODS = (
    (UsageCost, "billed"),
    (AsyncUsageCost, "billed"),
    (Fleet, "events"),
    (AsyncFleet, "events"),
)
ACTION_METHODS = ((Fleet, "run_bulk"), (AsyncFleet, "run_bulk"))


def doc(owner: Any, name: str | None = None) -> str:
    target = owner if name is None else getattr(owner, name)
    return " ".join((inspect.getdoc(target) or "").split())


@pytest.mark.parametrize("cls", CLASSES)
def test_every_public_class_carries_the_cost_block(cls: type) -> None:
    assert COST_BLOCK_MARKER in doc(cls)


@pytest.mark.parametrize(("cls", "name"), FREE_METHODS)
def test_the_free_methods_say_they_are_free(cls: type, name: str) -> None:
    assert FREE_WORD in doc(cls, name)


@pytest.mark.parametrize(("cls", "name"), PAID_METHODS)
def test_the_billable_methods_say_they_are_billable(cls: type, name: str) -> None:
    assert PAID_WORD in doc(cls, name)


def test_billed_documents_the_price_and_that_refresh_always_pays() -> None:
    text = doc(UsageCost, "billed")
    assert "$0,01" in text
    assert "refresh=True" in text


def test_the_cost_class_docs_warn_about_the_per_instance_cache() -> None:
    for cls in (UsageCost, AsyncUsageCost):
        text = doc(cls)
        assert "cada instancia" in text
        assert "COST_CACHE_MAX_ENTRIES" in text or "instancia nueva" in text


@pytest.mark.parametrize(("cls", "name"), ACTION_METHODS)
def test_run_bulk_documents_that_resume_bills_compute_again(cls: type, name: str) -> None:
    text = doc(cls, name)
    assert "resume" in text
    assert "facturar cómputo" in text


@pytest.mark.parametrize("cls", (Fleet, AsyncFleet))
def test_the_fleet_cost_block_lists_every_action(cls: type) -> None:
    text = doc(cls)
    for fragment in (
        "`kill`",
        "`suspend`",
        "`resume`",
        "$0,00155/GB",
        "$0,0038/GB",
        "$0,08/GB-mes",
    ):
        assert fragment in text


async def test_the_async_estimate_and_report_never_touch_cost_explorer() -> None:
    plane = FakePlane([list_item(1)])
    cost_source = FakeCostSource(None)  # type: ignore[arg-type]
    await AsyncQuotas(control_plane=plane, source=FakeQuotaSource(applied_defaults())).report()
    await AsyncFleet(control_plane=plane).sandboxes()
    await AsyncUsageCost(control_plane=plane, source=cost_source).estimate()
    assert cost_source.queries == []
