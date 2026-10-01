"""`_feature_options.plan_features`'s `events=` branch: validated before any
AWS call, requires `logging="cloudwatch"`, and every other branch stays
unaffected by the added `logging=` parameter.
"""

from __future__ import annotations

import pytest

from rayito import Sandbox
from rayito._feature_options import FeatureOptions, plan_features
from rayito.exceptions import InvalidArgumentException, UnimplementedError


def test_events_without_cloudwatch_logging_is_invalid_argument() -> None:
    with pytest.raises(InvalidArgumentException, match="cloudwatch"):
        plan_features(FeatureOptions(events=object()), logging=None)


def test_events_with_disabled_logging_is_also_invalid_argument() -> None:
    with pytest.raises(InvalidArgumentException):
        plan_features(FeatureOptions(events=object()), logging="disabled")


def test_events_with_cloudwatch_logging_does_not_raise() -> None:
    plan_features(FeatureOptions(events=object()), logging="cloudwatch")


def test_no_events_option_ignores_logging_entirely() -> None:
    # Esto era el comportamiento exacto antes de este cambio: ninguna otra
    # rama debe verse afectada por el nuevo parámetro `logging=`.
    plan_features(FeatureOptions(), logging=None)


def test_other_branches_still_raise_unimplemented() -> None:
    with pytest.raises(UnimplementedError):
        plan_features(FeatureOptions(mounts={}), logging="cloudwatch")


def test_events_without_cloudwatch_logging_rejects_before_any_control_plane() -> None:
    # Mirrors `test_m15_create_kwargs.py`'s table for the other six options:
    # `events=` without `logging="cloudwatch"` fails before
    # `resolve_control_plane` (and so before any AWS call), just with
    # `InvalidArgumentException` instead of `UnimplementedError` now that it
    # left that stub list.
    with pytest.raises(InvalidArgumentException):
        Sandbox.create("rayito-base", events=object())
