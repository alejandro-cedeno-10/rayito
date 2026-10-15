"""Los topes del coste facturado: ventanas y regiones acotadas, y un
presupuesto diario de consultas de pago que ningún usuario puede saltarse
cambiando `start`, `end` o `region`."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import _stack_path  # noqa: F401
import pytest
from botocore.exceptions import ClientError
from dashboard_stack.cost_cache import DynamoCostCache
from dashboard_stack.cost_guard import (
    BudgetedCostSource,
    CostWindowGuard,
    DynamoMissBudget,
)
from stack_fakes import FakeDynamo

from rayito._dashboard._api._router import ApiError
from rayito._dashboard._domain import CostQueryResult
from rayito._limits import DASHBOARD_STACK_COST_LOOKBACK_MONTHS
from rayito.exceptions import InvalidArgumentException

NOW = datetime(2026, 10, 13, 12, tzinfo=UTC)
BUDGET = 3
REGION = "us-east-1"
FIRST_OF_MONTH = date(2026, 10, 1)
TOMORROW = date(2026, 10, 14)


class Source:
    def __init__(self) -> None:
        self.calls: list[tuple[date, date, str | None]] = []

    def get_cost_and_usage(
        self, *, start: date, end: date, granularity: str, region: str | None
    ) -> CostQueryResult:
        self.calls.append((start, end, region))
        return CostQueryResult(periods=(), requests=1)


def guard(source: Source) -> CostWindowGuard:
    return CostWindowGuard(source, clock=lambda: NOW)


def query(window: CostWindowGuard, start: date, end: date, region: str | None = None) -> None:
    window.get_cost_and_usage(start=start, end=end, granularity="MONTHLY", region=region)


def months_ago(count: int) -> date:
    index = NOW.year * 12 + NOW.month - 1 - count
    return date(index // 12, index % 12 + 1, 1)


def test_the_default_window_and_whole_months_are_accepted() -> None:
    source = Source()
    window = guard(source)
    query(window, FIRST_OF_MONTH, TOMORROW)
    query(window, months_ago(2), months_ago(1), REGION)
    query(window, months_ago(DASHBOARD_STACK_COST_LOOKBACK_MONTHS - 1), FIRST_OF_MONTH)
    assert len(source.calls) == 3


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (date(2026, 10, 2), TOMORROW),
        (date(2026, 10, 1), date(2026, 10, 10)),
        (FIRST_OF_MONTH, FIRST_OF_MONTH),
        (FIRST_OF_MONTH, TOMORROW + timedelta(days=1)),
        (months_ago(DASHBOARD_STACK_COST_LOOKBACK_MONTHS), FIRST_OF_MONTH),
        (date(2020, 1, 1), date(2020, 2, 1)),
    ],
)
def test_a_window_that_is_not_month_aligned_or_too_old_never_reaches_cost_explorer(
    start: date, end: date
) -> None:
    source = Source()
    with pytest.raises(InvalidArgumentException):
        query(guard(source), start, end)
    assert source.calls == []


def test_an_unknown_region_never_reaches_cost_explorer() -> None:
    source = Source()
    with pytest.raises(InvalidArgumentException):
        query(guard(source), FIRST_OF_MONTH, TOMORROW, "x" * 40)
    assert source.calls == []


def budgeted(source: Source, dynamo: FakeDynamo) -> DynamoCostCache:
    budget = DynamoMissBudget(
        dynamo, "cache", misses_per_day=BUDGET, clock=lambda: NOW, expiry_clock=lambda: 1_000.0
    )
    return DynamoCostCache(BudgetedCostSource(source, budget), dynamo, "cache")


def test_once_the_daily_budget_is_used_up_new_windows_do_not_reach_cost_explorer() -> None:
    source, dynamo = Source(), FakeDynamo()
    cache = budgeted(source, dynamo)
    windows = [(months_ago(n), months_ago(n - 1) if n else TOMORROW) for n in range(BUDGET + 2)]
    for start, end in windows[:BUDGET]:
        cache.get_cost_and_usage(start=start, end=end, granularity="MONTHLY", region=None)
    with pytest.raises(ApiError) as refused:
        cache.get_cost_and_usage(
            start=windows[BUDGET][0], end=windows[BUDGET][1], granularity="MONTHLY", region=None
        )
    assert refused.value.code == "aws_throttled"
    assert len(source.calls) == BUDGET


def test_a_cached_window_still_answers_after_the_budget_is_used_up() -> None:
    source, dynamo = Source(), FakeDynamo()
    cache = budgeted(source, dynamo)
    for n in range(BUDGET):
        cache.get_cost_and_usage(
            start=months_ago(n),
            end=months_ago(n - 1) if n else TOMORROW,
            granularity="MONTHLY",
            region=None,
        )
    again = cache.get_cost_and_usage(
        start=months_ago(0), end=TOMORROW, granularity="MONTHLY", region=None
    )
    assert again.requests == 1
    assert len(source.calls) == BUDGET


def test_the_budget_counts_per_utc_day() -> None:
    dynamo = FakeDynamo()
    clock = [NOW]
    budget = DynamoMissBudget(dynamo, "cache", misses_per_day=1, clock=lambda: clock[0])
    budget.spend()
    with pytest.raises(ApiError):
        budget.spend()
    clock[0] = NOW + timedelta(days=1)
    budget.spend()


def test_if_the_budget_cannot_be_checked_nothing_is_billed() -> None:
    class Broken:
        def update_item(self, **_: object) -> None:
            raise ClientError({"Error": {"Code": "ProvisionedThroughputExceededException"}}, "x")

    source = Source()
    budget = DynamoMissBudget(Broken(), "cache", clock=lambda: NOW)
    with pytest.raises(ApiError) as refused:
        BudgetedCostSource(source, budget).get_cost_and_usage(
            start=FIRST_OF_MONTH, end=TOMORROW, granularity="MONTHLY", region=None
        )
    assert refused.value.code == "aws_error"
    assert source.calls == []
