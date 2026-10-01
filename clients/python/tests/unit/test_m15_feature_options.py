"""`rayito._feature_options.plan_features`: con las siete opciones en
`None` no pasa nada; cualquiera puesta lanza `UnimplementedError` nombrando
su propio cambio OpenSpec, antes de construir ningún `FeaturePlan`."""

from __future__ import annotations

import pytest

from rayito._feature_options import FeatureOptions, FeaturePlan, plan_features
from rayito.exceptions import UnimplementedError


def test_with_everything_none_the_plan_is_empty() -> None:
    plan = plan_features(FeatureOptions())
    assert plan == FeaturePlan()
    assert plan.configure_sections == ()


@pytest.mark.parametrize(
    ("field", "value", "option_name", "change_slug"),
    [
        ("mounts", {"/mnt/d": object()}, "mounts=", "m15-s3-mounts"),
        ("volumes", {"/mnt/v": object()}, "volumes=", "m15-efs-volumes"),
        ("size", "4gb", "size=", "m15-sizes-catalog"),
        ("events", object(), "events=", "m15-events-webhooks"),
        ("telemetry", object(), "telemetry=", "m15-rayd-otlp"),
        ("gateways", {"anthropic": object()}, "gateways=", "m15-secrets-gateway"),
        ("domain", object(), "domain=", "m15-custom-domain"),
    ],
)
def test_each_option_raises_unimplemented_naming_its_own_change(
    field: str, value: object, option_name: str, change_slug: str
) -> None:
    options = FeatureOptions(**{field: value})  # type: ignore[arg-type]
    with pytest.raises(UnimplementedError, match=change_slug) as excinfo:
        plan_features(options)
    assert excinfo.value.feature == option_name


def test_feature_options_defaults_are_all_none() -> None:
    options = FeatureOptions()
    assert options.mounts is None
    assert options.volumes is None
    assert options.size is None
    assert options.events is None
    assert options.telemetry is None
    assert options.gateways is None
    assert options.domain is None
