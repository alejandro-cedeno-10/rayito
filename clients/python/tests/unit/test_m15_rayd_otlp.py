"""m15-rayd-otlp: `rayito._telemetry_export`'s domain (`_domain.py`) and
adapter (`_section.py`). No servidor real ni `boto3` de verdad: la
validación es pura y el bearer se resuelve por una `SecretCache` sobre el
Secrets Manager falso de M13a (`fake_secrets.py`).
"""

from __future__ import annotations

import pytest

from rayito._configure_base import AgentFeatures
from rayito._secrets import SecretCache
from rayito._telemetry_export import (
    MAX_INTERVAL_S,
    MIN_INTERVAL_S,
    OtlpAuth,
    TelemetryExport,
    TelemetryExportSection,
    TelemetryHealth,
    build_section,
    plan,
    require_telemetry_support,
    resolve_bearer_token,
)
from rayito.exceptions import (
    InvalidArgumentException,
    SecretException,
    SecretNotFoundException,
    UnimplementedError,
)
from rayito.v1 import configure_pb2, telemetry_export_pb2

from .fake_secrets import SENTINEL_NAME, SENTINEL_VALUE, FakeSecretsManager, SpySession


def cache_over(api: FakeSecretsManager) -> SecretCache:
    return SecretCache(region="us-east-1", session=SpySession(api=api))


# --------------------------------------------------------------- OtlpAuth


def test_execution_role_auth_has_no_secret_name() -> None:
    auth = OtlpAuth.execution_role()
    assert auth.kind == "execution_role"
    assert auth.secret_name is None


def test_bearer_auth_keeps_the_secret_name() -> None:
    auth = OtlpAuth.bearer("otlp-key")
    assert auth.kind == "bearer"
    assert auth.secret_name == "otlp-key"


def test_bearer_with_an_empty_secret_name_is_invalid() -> None:
    with pytest.raises(InvalidArgumentException):
        OtlpAuth.bearer("   ")


# ---------------------------------------------------------- TelemetryExport


def test_defaults_are_60s_rayito_service_and_execution_role() -> None:
    telemetry = TelemetryExport()
    assert telemetry.interval_s == 60
    assert telemetry.service_name == "rayito"
    assert telemetry.names == "rayito"
    assert telemetry.auth.kind == "execution_role"


@pytest.mark.parametrize("interval_s", [MIN_INTERVAL_S, MAX_INTERVAL_S, 60, 300])
def test_interval_bounds_are_inclusive(interval_s: int) -> None:
    TelemetryExport(interval_s=interval_s)  # does not raise


@pytest.mark.parametrize("interval_s", [0, MIN_INTERVAL_S - 1, MAX_INTERVAL_S + 1, 301, -1])
def test_an_interval_out_of_bounds_is_invalid(interval_s: int) -> None:
    with pytest.raises(InvalidArgumentException):
        TelemetryExport(interval_s=interval_s)


def test_a_blank_service_name_is_invalid() -> None:
    with pytest.raises(InvalidArgumentException):
        TelemetryExport(service_name="   ")


def test_an_unknown_name_style_is_invalid() -> None:
    with pytest.raises(InvalidArgumentException):
        TelemetryExport(names="e2b-but-misspelled")  # type: ignore[arg-type]


# ----------------------------------------------------------------- plan()


def test_plan_rejects_a_non_telemetry_export_value() -> None:
    with pytest.raises(InvalidArgumentException):
        plan(object(), image_variant=None)


def test_plan_defers_the_caps_check_for_an_unknown_image_variant() -> None:
    telemetry = TelemetryExport(auth=OtlpAuth.execution_role())
    assert plan(telemetry, image_variant=None) is telemetry


def test_plan_passes_bearer_auth_on_any_image_variant() -> None:
    telemetry = TelemetryExport(auth=OtlpAuth.bearer("otlp-key"))
    assert plan(telemetry, image_variant="base") is telemetry


def test_plan_requires_caps_for_execution_role_on_a_known_non_caps_variant() -> None:
    telemetry = TelemetryExport(auth=OtlpAuth.execution_role())
    with pytest.raises(UnimplementedError, match="base-caps"):
        plan(telemetry, image_variant="base")


def test_plan_accepts_execution_role_on_the_caps_variant() -> None:
    telemetry = TelemetryExport(auth=OtlpAuth.execution_role())
    assert plan(telemetry, image_variant="base-caps") is telemetry


# ----------------------------------------------------------- resolve_bearer_token


def test_execution_role_auth_never_touches_secrets_manager() -> None:
    api = FakeSecretsManager()
    assert resolve_bearer_token(TelemetryExport(), cache_over(api)) is None
    assert api.requests == []


def test_bearer_resolves_under_the_rayito_prefix_like_secrets() -> None:
    api = FakeSecretsManager()
    api.put("rayito/otlp-key", "sk-test")
    telemetry = TelemetryExport(auth=OtlpAuth.bearer("otlp-key"))
    assert resolve_bearer_token(telemetry, cache_over(api)) == "sk-test"
    assert api.requests == [("GetSecretValue", {"SecretId": "rayito/otlp-key"})]


def test_bearer_reuses_the_cache_within_its_ttl() -> None:
    api = FakeSecretsManager()
    api.put("rayito/otlp-key", "sk-test")
    cache = cache_over(api)
    telemetry = TelemetryExport(auth=OtlpAuth.bearer("otlp-key"))
    resolve_bearer_token(telemetry, cache)
    resolve_bearer_token(telemetry, cache)
    assert api.count("GetSecretValue") == 1


def test_bearer_takes_a_full_arn_as_is() -> None:
    api = FakeSecretsManager()
    api.put("team/otlp-key", "sk-test")
    arn = api.secrets["team/otlp-key"].arn
    telemetry = TelemetryExport(auth=OtlpAuth.bearer(arn))
    assert resolve_bearer_token(telemetry, cache_over(api)) == "sk-test"


def test_a_missing_bearer_secret_is_translated_and_never_named() -> None:
    telemetry = TelemetryExport(auth=OtlpAuth.bearer(SENTINEL_NAME))
    with pytest.raises(SecretNotFoundException) as raised:
        resolve_bearer_token(telemetry, cache_over(FakeSecretsManager()))
    assert isinstance(raised.value, SecretException)
    assert SENTINEL_NAME not in str(raised.value)


# ------------------------------------------------------- TelemetryExportSection


def test_section_names_itself_telemetry_export() -> None:
    section = TelemetryExportSection(
        telemetry=TelemetryExport(),
        image_arn="arn:aws:lambda:us-east-1:123456789012:microvm-image/rayito-base",
        image_version="3",
        image_memory_mib=2048,
        bearer_token=None,
    )
    assert section.section == "telemetry_export"
    assert section.required_flag == "telemetry_export"


def test_fill_builds_an_execution_role_section_with_image_facts() -> None:
    telemetry = TelemetryExport(interval_s=90, service_name="agente", names="e2b")
    section = TelemetryExportSection(
        telemetry=telemetry,
        image_arn="arn:aws:lambda:us-east-1:123456789012:microvm-image/rayito-base-caps",
        image_version="7",
        image_memory_mib=4096,
        bearer_token=None,
    )
    request = configure_pb2.ConfigureRequest()
    section.fill(request)
    config = request.telemetry_export
    assert config.interval_s == 90
    assert config.service_name == "agente"
    assert config.names == telemetry_export_pb2.TELEMETRY_EXPORT_NAME_STYLE_E2B
    assert config.image_arn.endswith("rayito-base-caps")
    assert config.image_version == "7"
    assert config.image_memory_mib == 4096
    assert config.WhichOneof("auth") == "execution_role"


def test_fill_builds_a_bearer_section_with_the_resolved_token_never_the_secret_name() -> None:
    telemetry = TelemetryExport(auth=OtlpAuth.bearer("otlp-key"))
    section = TelemetryExportSection(
        telemetry=telemetry,
        image_arn="arn:aws:lambda:us-east-1:123456789012:microvm-image/rayito-base",
        image_version="1",
        image_memory_mib=2048,
        bearer_token="sk-resolved",
    )
    request = configure_pb2.ConfigureRequest()
    section.fill(request)
    config = request.telemetry_export
    assert config.WhichOneof("auth") == "bearer"
    assert config.bearer.token == "sk-resolved"


def test_build_section_carries_the_resolved_token_into_the_section() -> None:
    section = build_section(
        TelemetryExport(auth=OtlpAuth.bearer("otlp-key")),
        image_arn="arn:aws:lambda:us-east-1:123456789012:microvm-image/rayito-base",
        image_version="1",
        image_memory_mib=2048,
        bearer_token="sk-test",
    )
    assert section.bearer_token == "sk-test"


def test_the_section_repr_never_shows_the_bearer_token() -> None:
    section = build_section(
        TelemetryExport(auth=OtlpAuth.bearer("otlp-key")),
        image_arn="arn:aws:lambda:us-east-1:123456789012:microvm-image/rayito-base",
        image_version="1",
        image_memory_mib=2048,
        bearer_token=SENTINEL_VALUE,
    )
    assert SENTINEL_VALUE not in repr(section)
    assert SENTINEL_VALUE not in str(section)


# ------------------------------------------------------- require_telemetry_support


def test_a_0_6_agent_without_the_exporter_names_the_real_cause_and_the_image() -> None:
    with pytest.raises(UnimplementedError) as raised:
        require_telemetry_support(AgentFeatures(configure=True, telemetry_export=False))
    message = str(raised.value)
    assert "AWS_REGION" in message
    assert "rayd 0.6.0" in message
    assert "pendiente de medición" not in message


def test_an_agent_with_the_exporter_passes_the_gate() -> None:
    require_telemetry_support(AgentFeatures(configure=True, telemetry_export=True))


# --------------------------------------------------------------- TelemetryHealth


def test_telemetry_health_defaults_to_all_zero() -> None:
    health = TelemetryHealth()
    assert health.exported == 0
    assert health.dropped == 0
    assert health.last_error_class is None
