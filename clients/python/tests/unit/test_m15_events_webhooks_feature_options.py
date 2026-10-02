"""`plan_features`' `events=` branch: the option must be a
`LifecycleEvents`/`AsyncLifecycleEvents` and `logging=` must reach
CloudWatch (validated through the same resolver `run-microvm` uses); with
both right it still raises `UnimplementedError` naming the missing
`ConfigureSandbox` send, before any control plane is resolved — accepting
it silently would leave the user paying for the stack with no events.
"""

from __future__ import annotations

import pytest

from rayito import AsyncLifecycleEvents, LifecycleEvents, Sandbox
from rayito._feature_options import EVENTS_CHANGE, FeatureOptions, plan_features
from rayito.exceptions import InvalidArgumentException, UnimplementedError


def test_events_must_be_a_lifecycle_events() -> None:
    with pytest.raises(InvalidArgumentException, match="LifecycleEvents"):
        plan_features(FeatureOptions(events=object()), logging="cloudwatch")


@pytest.mark.parametrize("logging", ["disabled", {"disabled": {}}])
def test_events_needs_logging_that_reaches_cloudwatch(logging: object) -> None:
    with pytest.raises(InvalidArgumentException, match="cloudwatch"):
        plan_features(FeatureOptions(events=LifecycleEvents()), logging=logging)


def test_an_invalid_logging_value_is_the_resolvers_own_error() -> None:
    with pytest.raises(InvalidArgumentException, match="logging debe ser"):
        plan_features(FeatureOptions(events=LifecycleEvents()), logging="syslog")


@pytest.mark.parametrize(
    "events", [LifecycleEvents(), AsyncLifecycleEvents()], ids=["sync", "async"]
)
@pytest.mark.parametrize(
    "logging", ["cloudwatch", {"cloudWatch": {"logGroup": "/custom/group"}}], ids=str
)
def test_a_valid_events_option_is_still_unimplemented_naming_what_is_missing(
    events: object, logging: object
) -> None:
    with pytest.raises(UnimplementedError) as raised:
        plan_features(FeatureOptions(events=events), logging=logging)
    assert EVENTS_CHANGE in str(raised.value)
    assert "ConfigureSandbox" in str(raised.value)


def test_no_events_option_ignores_logging_entirely() -> None:
    plan_features(FeatureOptions(), logging="disabled")


def test_create_rejects_events_before_any_control_plane() -> None:
    # No session, no region: reaching `resolve_control_plane` would fail
    # differently, so this proves the rejection comes first.
    with pytest.raises(UnimplementedError):
        Sandbox.create("rayito-base", events=LifecycleEvents(), logging="cloudwatch")
