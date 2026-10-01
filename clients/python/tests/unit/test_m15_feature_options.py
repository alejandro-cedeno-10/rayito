"""`rayito._feature_options.plan_features`: con las siete opciones en
`None` no pasa nada; cualquiera que siga siendo un stub lanza
`UnimplementedError` nombrando su propio cambio OpenSpec, antes de
construir ningún `FeaturePlan`. `gateways=` (m15-secrets-gateway) ya no es
un stub: ver `test_gateways_builds_a_configure_section_factory`."""

from __future__ import annotations

import pytest

from rayito._feature_options import FeatureOptions, FeaturePlan, plan_features
from rayito._secret_gateway import GatewaySectionFactory, SecretGateway
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
        ("domain", object(), "domain=", "m15-custom-domain"),
    ],
)
def test_each_remaining_stub_option_raises_unimplemented_naming_its_own_change(
    field: str, value: object, option_name: str, change_slug: str
) -> None:
    options = FeatureOptions(**{field: value})  # type: ignore[arg-type]
    with pytest.raises(UnimplementedError, match=change_slug) as excinfo:
        plan_features(options)
    assert excinfo.value.feature == option_name


def test_gateways_builds_a_configure_section_factory_instead_of_raising() -> None:
    gateway = SecretGateway(
        upstream="https://api.anthropic.com",
        headers={"x-api-key": "anthropic"},
        allow=[("POST", "/v1/messages")],
    )
    plan = plan_features(FeatureOptions(gateways={"anthropic": gateway}))
    assert len(plan.configure_sections) == 1
    assert isinstance(plan.configure_sections[0], GatewaySectionFactory)


def test_gateways_with_an_invalid_mapping_raises_before_any_other_check() -> None:
    from rayito.exceptions import InvalidArgumentException

    with pytest.raises(InvalidArgumentException):
        plan_features(FeatureOptions(gateways={}))


def test_feature_options_defaults_are_all_none() -> None:
    options = FeatureOptions()
    assert options.mounts is None
    assert options.volumes is None
    assert options.size is None
    assert options.events is None
    assert options.telemetry is None
    assert options.gateways is None
    assert options.domain is None
