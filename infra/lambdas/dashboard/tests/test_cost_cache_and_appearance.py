"""La caché del coste facturado en DynamoDB y el tema leído de SSM."""

from __future__ import annotations

import json
from datetime import date

import _stack_path  # noqa: F401
import pytest
from dashboard_stack.appearance import LiveAppearance, ParameterUnavailable, load_appearance
from dashboard_stack.cost_cache import DynamoCostCache
from stack_fakes import FakeDynamo, FakeSsm

from rayito._dashboard._domain import CostGroup, CostPeriod, CostQueryResult

TTL = 86_400
RESULT = CostQueryResult(
    (
        CostPeriod(
            date(2026, 10, 1),
            date(2026, 10, 13),
            True,
            (CostGroup("Requests", 1.5, 3.0, "Requests"),),
        ),
    ),
    requests=1,
)
WINDOW = {
    "start": date(2026, 10, 1),
    "end": date(2026, 10, 13),
    "granularity": "DAILY",
    "region": "us-east-1",
}


class Source:
    def __init__(self) -> None:
        self.calls = 0

    def get_cost_and_usage(self, **_: object) -> CostQueryResult:
        self.calls += 1
        return RESULT


def test_a_window_is_billed_once_and_then_served_from_the_table() -> None:
    clock = [1_000.0]
    source, dynamo = Source(), FakeDynamo()
    cache = DynamoCostCache(source, dynamo, "cache", ttl_seconds=TTL, clock=lambda: clock[0])
    first = cache.get_cost_and_usage(**WINDOW)
    second = cache.get_cost_and_usage(**WINDOW)
    assert source.calls == 1
    assert second.periods == first.periods
    assert second.fetched_at == first.fetched_at
    clock[0] += TTL + 1
    cache.get_cost_and_usage(**WINDOW)
    assert source.calls == 2


def test_the_row_expires_through_the_table_ttl_attribute() -> None:
    dynamo = FakeDynamo()
    DynamoCostCache(
        Source(), dynamo, "cache", ttl_seconds=TTL, clock=lambda: 1_000.0
    ).get_cost_and_usage(**WINDOW)
    (row,) = dynamo.items.values()
    assert row["expires_at"]["N"] == str(1_000 + TTL)


def test_a_broken_cache_never_stops_the_cost_from_being_served() -> None:
    source = Source()
    cache = DynamoCostCache(source, FakeDynamo(fail=True), "cache")
    assert cache.get_cost_and_usage(**WINDOW).periods == RESULT.periods


THEME = json.dumps({"version": 1, "name": "mi-marca", "dark": {"primary": "#00b2a9"}})


def test_a_valid_theme_is_served_and_named() -> None:
    look = load_appearance(FakeSsm({"/t": THEME}), "/t", None)
    assert look.branding.name == "mi-marca"
    assert "--primary: #00b2a9" in look.css


def test_an_invalid_or_hostile_theme_falls_back_to_the_built_in_one() -> None:
    hostile = json.dumps({"version": 1, "dark": {"primary": "red; } body { display:none"}})
    for values in ({"/t": hostile}, {"/t": "not json"}):
        look = load_appearance(FakeSsm(values), "/t", None)
        assert look.theme is None and look.css == ""


def test_a_parameter_that_cannot_be_read_is_reported_so_the_caller_keeps_the_previous_look() -> (
    None
):
    with pytest.raises(ParameterUnavailable):
        load_appearance(FakeSsm({}), "/t", None)


REFRESH = 300.0


def test_the_theme_is_reread_after_the_refresh_interval_and_kept_when_ssm_fails() -> None:
    clock, ssm = [0.0], FakeSsm({"/t": THEME})
    live = LiveAppearance(
        lambda: load_appearance(ssm, "/t", None), clock=lambda: clock[0], refresh_seconds=REFRESH
    )
    assert live.branding.name == "mi-marca" and len(ssm.calls) == 1
    ssm.values["/t"] = json.dumps({"version": 1, "name": "otra"})
    assert live.branding.name == "mi-marca" and len(ssm.calls) == 1
    clock[0] = REFRESH
    assert live.branding.name == "otra" and "otra" in live.files["/theme.css"][0].decode()
    ssm.values.clear()
    clock[0] = 2 * REFRESH
    assert live.branding.name == "otra"


def test_the_shell_is_served_with_an_empty_theme_when_none_is_configured() -> None:
    live = LiveAppearance(lambda: load_appearance(FakeSsm(), None, None))
    assert live.files["/theme.css"][0] == b"" and "/" in live.files


def test_no_parameter_means_no_call_to_ssm() -> None:
    ssm = FakeSsm()
    assert load_appearance(ssm, None, None).theme is None
    assert ssm.calls == []


def test_a_logo_with_a_script_is_dropped() -> None:
    svg = '<svg xmlns="http://www.w3.org/2000/svg"><script>x()</script></svg>'
    assert load_appearance(FakeSsm({"/l": svg}), None, "/l").logo is None
    good = '<svg xmlns="http://www.w3.org/2000/svg"><rect width="1" height="1"/></svg>'
    assert load_appearance(FakeSsm({"/l": good}), None, "/l").logo is not None
