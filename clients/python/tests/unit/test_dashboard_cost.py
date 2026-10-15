"""`UsageCost` y `AsyncUsageCost`: coste facturado de Cost Explorer con
caché local y estimación por sandbox, siempre rotulados. El adaptador de Cost
Explorer se prueba con `Stubber`: parámetros enviados, paginación (cada
página es una petición de pago) y errores por código."""

from __future__ import annotations

import asyncio
import inspect
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from botocore.stub import Stubber

from rayito._dashboard._aws import CostExplorerSource
from rayito._dashboard._cost import UsageCost
from rayito._dashboard._cost_async import AsyncUsageCost
from rayito._dashboard._domain import CostGroup, CostPeriod, CostQueryResult
from rayito.exceptions import AuthenticationException, DashboardException, InvalidArgumentException

from .conftest import IMAGE_ARN, FakeClock, no_retry_client
from .fake_dashboard import (
    NOW,
    FakeCostSource,
    FakePlane,
    list_item,
)

pytestmark = pytest.mark.usefixtures("clean_catalog")

QUERY_RESULT = CostQueryResult(
    (
        CostPeriod(
            date(2026, 10, 1),
            date(2026, 10, 14),
            True,
            (CostGroup("Lambda-MicroVM-vCPU-Second-ARM", 0.5, 100.0, "vCPU-Seconds"),),
        ),
    ),
    requests=1,
)


def usage_cost(source: FakeCostSource, clock: FakeClock | None = None, **extra: Any) -> UsageCost:
    return UsageCost(
        source=source,
        clock=lambda: NOW,
        monotonic=clock or FakeClock(),
        **extra,
    )


def test_billed_defaults_to_the_month_so_far_and_labels_the_result() -> None:
    source = FakeCostSource(QUERY_RESULT)
    cost = usage_cost(source).billed()

    assert source.queries == [(date(2026, 10, 1), date(2026, 10, 14), "MONTHLY", None)]
    assert cost.label == "billed"
    assert cost.total_usd == 0.5
    assert cost.partial is True
    assert cost.fetched_at == NOW
    assert cost.requests == 1


def test_billed_accepts_iso_strings_and_a_region_filter() -> None:
    source = FakeCostSource(QUERY_RESULT)
    usage_cost(source).billed(start="2026-09-01", end=date(2026, 10, 1), region="eu-west-1")
    assert source.queries == [(date(2026, 9, 1), date(2026, 10, 1), "MONTHLY", "eu-west-1")]


@pytest.mark.parametrize(
    ("start", "end"), [("2026-10-05", "2026-10-05"), ("2026-10-06", "2026-10-05"), ("no", None)]
)
def test_billed_rejects_an_empty_or_malformed_window_before_any_call(
    start: str, end: str | None
) -> None:
    source = FakeCostSource(QUERY_RESULT)
    with pytest.raises(InvalidArgumentException):
        usage_cost(source).billed(start=start, end=end)
    assert source.queries == []


def test_the_second_read_is_served_from_the_cache_until_the_ttl_expires() -> None:
    source = FakeCostSource(QUERY_RESULT)
    clock = FakeClock()
    cost = usage_cost(source, clock, cache_ttl_seconds=100)

    first = cost.billed()
    clock.advance(99)
    assert cost.billed() is first
    assert len(source.queries) == 1
    clock.advance(1)
    cost.billed()
    assert len(source.queries) == 2


def test_refresh_and_different_windows_bypass_the_cache() -> None:
    source = FakeCostSource(QUERY_RESULT)
    cost = usage_cost(source)
    cost.billed()
    cost.billed(refresh=True)
    cost.billed(granularity="DAILY")
    cost.billed(region="us-east-1")
    assert len(source.queries) == 4


def test_the_default_ttl_is_the_named_constant() -> None:
    from rayito._limits import COST_CACHE_TTL_SECONDS

    default = inspect.signature(UsageCost.__init__).parameters["cache_ttl_seconds"].default
    assert default == COST_CACHE_TTL_SECONDS


def test_estimate_is_labelled_and_only_prices_running_sandboxes() -> None:
    plane = FakePlane(
        [
            list_item(1, started_at=NOW - timedelta(hours=1)),
            list_item(2, state="SUSPENDED", started_at=NOW - timedelta(hours=5)),
        ],
        memory={(IMAGE_ARN, "1.0"): 2048},
    )
    source = FakeCostSource(QUERY_RESULT)

    estimate = usage_cost(source, control_plane=plane).estimate()

    assert estimate.label == "estimated"
    assert estimate.total_usd == pytest.approx(0.1261, abs=1e-6)
    running, suspended = estimate.items
    assert running.label == suspended.label == "estimated"
    assert suspended.cost_usd is None
    assert source.queries == []


def test_async_usage_cost_matches_the_sync_one_and_its_signatures() -> None:
    source = FakeCostSource(QUERY_RESULT)
    asynchronous = AsyncUsageCost(source=source, clock=lambda: NOW)
    assert asyncio.run(asynchronous.billed()) == usage_cost(FakeCostSource(QUERY_RESULT)).billed()
    assert inspect.signature(AsyncUsageCost.__init__) == inspect.signature(UsageCost.__init__)
    for method in ("billed", "estimate"):
        assert list(inspect.signature(getattr(UsageCost, method)).parameters) == list(
            inspect.signature(getattr(AsyncUsageCost, method)).parameters
        )


def ce_response(
    *, token: str | None = None, usage_type: str = "Lambda-MicroVM-vCPU-Second-ARM"
) -> dict[str, Any]:
    response: dict[str, Any] = {
        "GroupDefinitions": [{"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
        "ResultsByTime": [
            {
                "TimePeriod": {"Start": "2026-10-01", "End": "2026-10-14"},
                "Total": {},
                "Groups": [
                    {
                        "Keys": [usage_type],
                        "Metrics": {
                            "UnblendedCost": {"Amount": "0.25", "Unit": "USD"},
                            "UsageQuantity": {"Amount": "50", "Unit": "vCPU-Seconds"},
                        },
                    }
                ],
                "Estimated": True,
            }
        ],
    }
    if token is not None:
        response["NextPageToken"] = token
    return response


def expected_params(*, region: str | None = None, token: str | None = None) -> dict[str, Any]:
    service = {"Dimensions": {"Key": "SERVICE", "Values": ["AWS Lambda"]}}
    params: dict[str, Any] = {
        "TimePeriod": {"Start": "2026-10-01", "End": "2026-10-14"},
        "Granularity": "MONTHLY",
        "Metrics": ["UnblendedCost", "UsageQuantity"],
        "Filter": service
        if region is None
        else {"And": [service, {"Dimensions": {"Key": "REGION", "Values": [region]}}]},
        "GroupBy": [{"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
    }
    if token is not None:
        params["NextPageToken"] = token
    return params


def test_cost_explorer_adapter_sends_the_verified_query_and_counts_each_page() -> None:
    client = no_retry_client("ce")
    with Stubber(client) as stub:
        stub.add_response("get_cost_and_usage", ce_response(token="next"), expected_params())
        stub.add_response("get_cost_and_usage", ce_response(), expected_params(token="next"))
        result = CostExplorerSource(lambda: client).get_cost_and_usage(
            start=date(2026, 10, 1), end=date(2026, 10, 14), granularity="MONTHLY", region=None
        )
        stub.assert_no_pending_responses()
    assert result.requests == 2
    assert len(result.periods) == 2
    group = result.periods[0].groups[0]
    assert (group.usage_type, group.cost_usd, group.quantity, group.unit) == (
        "Lambda-MicroVM-vCPU-Second-ARM",
        0.25,
        50.0,
        "vCPU-Seconds",
    )
    assert result.periods[0].estimated is True


def test_cost_explorer_adapter_filters_by_region() -> None:
    client = no_retry_client("ce")
    with Stubber(client) as stub:
        stub.add_response("get_cost_and_usage", ce_response(), expected_params(region="eu-west-1"))
        CostExplorerSource(lambda: client).get_cost_and_usage(
            start=date(2026, 10, 1),
            end=date(2026, 10, 14),
            granularity="MONTHLY",
            region="eu-west-1",
        )
        stub.assert_no_pending_responses()


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("AccessDeniedException", AuthenticationException),
        ("DataUnavailableException", DashboardException),
    ],
)
def test_cost_explorer_errors_map_by_code(code: str, expected: type[Exception]) -> None:
    client = no_retry_client("ce")
    with Stubber(client) as stub:
        stub.add_client_error(
            "get_cost_and_usage", service_error_code=code, service_message="cuenta secreta"
        )
        with pytest.raises(expected) as raised:
            CostExplorerSource(lambda: client).get_cost_and_usage(
                start=date(2026, 10, 1), end=date(2026, 10, 2), granularity="DAILY", region=None
            )
    assert "secreta" not in str(raised.value)
    assert getattr(raised.value, "aws_code", None) == code


def test_the_default_clock_is_utc() -> None:
    from rayito._dashboard._cost import utc_now

    assert utc_now().tzinfo is UTC
    assert isinstance(utc_now(), datetime)
