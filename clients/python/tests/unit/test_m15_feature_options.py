"""`rayito._feature_options.plan_features`: con las siete opciones en
`None` no pasa nada. Seis siguen siendo stubs: cualquier valor puesto lanza
`UnimplementedError` nombrando su propio cambio OpenSpec, antes de
construir ningún `FeaturePlan`. `telemetry=` (m15-rayd-otlp) es la primera
función real: valida de verdad (ver `test_each_real_telemetry_value_*`
abajo) en vez de lanzar `UnimplementedError` incondicionalmente."""

from __future__ import annotations

import pytest

from rayito._feature_options import FeatureOptions, FeaturePlan, plan_features
from rayito._telemetry_export import OtlpAuth, TelemetryExport
from rayito.exceptions import InvalidArgumentException, UnimplementedError


def test_with_everything_none_the_plan_is_empty() -> None:
    plan = plan_features(FeatureOptions())
    assert plan == FeaturePlan()
    assert plan.configure_sections == ()
    assert plan.telemetry is None


@pytest.mark.parametrize(
    ("field", "value", "option_name", "change_slug"),
    [
        ("mounts", {"/mnt/d": object()}, "mounts=", "m15-s3-mounts"),
        ("volumes", {"/mnt/v": object()}, "volumes=", "m15-efs-volumes"),
        ("size", "4gb", "size=", "m15-sizes-catalog"),
        ("events", object(), "events=", "m15-events-webhooks"),
        ("gateways", {"anthropic": object()}, "gateways=", "m15-secrets-gateway"),
        ("domain", object(), "domain=", "m15-custom-domain"),
    ],
)
def test_each_stub_option_raises_unimplemented_naming_its_own_change(
    field: str, value: object, option_name: str, change_slug: str
) -> None:
    options = FeatureOptions(**{field: value})  # type: ignore[arg-type]
    with pytest.raises(UnimplementedError, match=change_slug) as excinfo:
        plan_features(options)
    assert excinfo.value.feature == option_name


def test_a_garbage_telemetry_value_is_invalid_argument_not_unimplemented() -> None:
    """`telemetry=` ya valida de verdad: algo que no es un `TelemetryExport`
    es `InvalidArgumentException` (un error del llamante), nunca
    `UnimplementedError` (que implicaría que la función entera sigue sin
    construirse)."""
    with pytest.raises(InvalidArgumentException):
        plan_features(FeatureOptions(telemetry=object()))


def test_a_real_telemetry_value_with_an_unknown_image_variant_is_accepted() -> None:
    plan = plan_features(
        FeatureOptions(telemetry=TelemetryExport(auth=OtlpAuth.bearer("otlp-key"))),
        image_variant=None,
    )
    assert isinstance(plan.telemetry, TelemetryExport)


def test_a_real_telemetry_value_with_execution_role_on_a_non_caps_image_is_unimplemented() -> None:
    with pytest.raises(UnimplementedError):
        plan_features(
            FeatureOptions(telemetry=TelemetryExport(auth=OtlpAuth.execution_role())),
            image_variant="base",
        )


def test_feature_options_defaults_are_all_none() -> None:
    options = FeatureOptions()
    assert options.mounts is None
    assert options.volumes is None
    assert options.size is None
    assert options.events is None
    assert options.telemetry is None
    assert options.gateways is None
    assert options.domain is None
