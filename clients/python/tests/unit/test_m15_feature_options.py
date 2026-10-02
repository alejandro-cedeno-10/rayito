"""`rayito._feature_options.plan_features`: con las siete opciones en
`None` no pasa nada; `mounts=` (`m15-s3-mounts`, la primera en dejar de ser
un stub) construye una `S3MountsSection` real; `gateways=`
(m15-secrets-gateway) un `GatewaySectionFactory` (ver
`test_gateways_builds_a_configure_section_factory`); `size=` tampoco es ya
un stub (m15-sizes-catalog), pero no produce ninguna sección: su propia
resolución se prueba en `test_m15_sizes_catalog_domain.py` y
`test_m15_sizes_catalog_create.py`, no aquí. Las otras, puestas a algo
distinto de `None`, siguen lanzando `UnimplementedError` nombrando su propio
cambio OpenSpec, antes de construir ningún `FeaturePlan`."""

from __future__ import annotations

import pytest

from rayito._feature_options import FeatureOptions, FeaturePlan, plan_features
from rayito._s3_mounts import S3Mount, S3MountsSection
from rayito._secret_gateway import GatewaySectionFactory, SecretGateway
from rayito.exceptions import UnimplementedError


def test_with_everything_none_the_plan_is_empty() -> None:
    plan = plan_features(FeatureOptions())
    assert plan == FeaturePlan()
    assert plan.configure_sections == ()


def test_mounts_builds_a_real_section_instead_of_raising() -> None:
    mounts = {"/mnt/data": S3Mount(bucket="team-data")}
    plan = plan_features(FeatureOptions(mounts=mounts), image_variant="base-caps")
    assert len(plan.configure_sections) == 1
    section = plan.configure_sections[0]
    assert isinstance(section, S3MountsSection)
    assert dict(section.mounts) == mounts


def test_mounts_on_a_non_caps_image_variant_raises_before_run_microvm() -> None:
    mounts = {"/mnt/data": S3Mount(bucket="team-data")}
    with pytest.raises(UnimplementedError, match="base-caps") as excinfo:
        plan_features(FeatureOptions(mounts=mounts), image_variant="base")
    assert excinfo.value.feature == "mounts="


def test_mounts_with_an_unknown_image_variant_is_not_rejected_here() -> None:
    # `image_variant=None` (an opaque ARN or an unrecognized name): the
    # decision is deferred to the agent's own `Health.features`, never
    # rejected by `require_caps_for` itself.
    mounts = {"/mnt/data": S3Mount(bucket="team-data")}
    plan = plan_features(FeatureOptions(mounts=mounts), image_variant=None)
    assert len(plan.configure_sections) == 1


@pytest.mark.parametrize(
    ("field", "value", "option_name", "change_slug"),
    [
        ("volumes", {"/mnt/v": object()}, "volumes=", "m15-efs-volumes"),
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


def test_size_no_longer_raises_and_produces_no_configure_section() -> None:
    """m15-sizes-catalog: `size=` no es un ajuste de `ConfigureSandbox` (no
    hay nada que aplicar dentro del guest), así que `plan_features` no
    lanza y el plan sigue sin secciones; la resolución real vive en
    `_sizing.resolve_size` y se ejecuta en `create()`, no aquí."""
    plan = plan_features(FeatureOptions(size="4gb"))
    assert plan.configure_sections == ()
