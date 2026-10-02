"""m15-rayd-otlp: `rayito._telemetry_export`'s domain (`_domain.py`) and
adapter (`_section.py`). No servidor real ni `boto3` de verdad: la
validación es pura y `resolve_bearer_token` sólo necesita un cliente
mínimo con la forma de `SecretsManagerClient.get_secret_value`.
"""

from __future__ import annotations

from typing import Any

import pytest

from rayito._telemetry_export import (
    MAX_INTERVAL_S,
    MIN_INTERVAL_S,
    OtlpAuth,
    TelemetryExport,
    TelemetryExportSection,
    TelemetryHealth,
    build_section,
    plan,
    resolve_bearer_token,
)
from rayito.exceptions import InvalidArgumentException, SecretException, UnimplementedError
from rayito.v1 import configure_pb2, telemetry_export_pb2


class FakeSecretsManagerClient:
    """El doble mínimo de `boto3.client("secretsmanager")`: sólo
    `get_secret_value`, con la forma exacta que usa `resolve_bearer_token`."""

    def __init__(self, response: dict[str, Any] | Exception) -> None:
        self._response = response
        self.calls: list[str] = []

    def get_secret_value(self, *, SecretId: str) -> dict[str, Any]:
        self.calls.append(SecretId)
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


class FakeSession:
    def __init__(self, client: FakeSecretsManagerClient) -> None:
        self._client = client

    def client(self, service_name: str, *, region_name: str | None = None) -> Any:
        assert service_name == "secretsmanager"
        return self._client


# --------------------------------------------------------------- OtlpAuth


def test_execution_role_auth_has_no_secret_name() -> None:
    auth = OtlpAuth.execution_role()
    assert auth.kind == "execution_role"
    assert auth.secret_name is None


def test_bearer_auth_keeps_the_secret_name() -> None:
    auth = OtlpAuth.bearer("rayito/otlp-key")
    assert auth.kind == "bearer"
    assert auth.secret_name == "rayito/otlp-key"


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
    telemetry = TelemetryExport(auth=OtlpAuth.bearer("rayito/otlp-key"))
    assert plan(telemetry, image_variant="base") is telemetry


def test_plan_requires_caps_for_execution_role_on_a_known_non_caps_variant() -> None:
    telemetry = TelemetryExport(auth=OtlpAuth.execution_role())
    with pytest.raises(UnimplementedError, match="base-caps"):
        plan(telemetry, image_variant="base")


def test_plan_accepts_execution_role_on_the_caps_variant() -> None:
    telemetry = TelemetryExport(auth=OtlpAuth.execution_role())
    assert plan(telemetry, image_variant="base-caps") is telemetry


# ----------------------------------------------------------- resolve_bearer_token


def test_resolve_bearer_token_returns_the_secret_string() -> None:
    client = FakeSecretsManagerClient({"SecretString": "sk-test"})
    value = resolve_bearer_token("rayito/otlp-key", region="us-east-1", session=FakeSession(client))
    assert value == "sk-test"
    assert client.calls == ["rayito/otlp-key"]


def test_resolve_bearer_token_rejects_an_empty_secret_string() -> None:
    client = FakeSecretsManagerClient({"SecretString": ""})
    with pytest.raises(SecretException):
        resolve_bearer_token("rayito/otlp-key", region="us-east-1", session=FakeSession(client))


def test_resolve_bearer_token_wraps_a_client_error() -> None:
    client = FakeSecretsManagerClient(RuntimeError("boom"))
    with pytest.raises(SecretException):
        resolve_bearer_token("rayito/otlp-key", region="us-east-1", session=FakeSession(client))


def test_resolve_bearer_token_error_never_repeats_the_secret_name() -> None:
    client = FakeSecretsManagerClient(RuntimeError("boom"))
    try:
        resolve_bearer_token(
            "rayito/super-secret-name", region="us-east-1", session=FakeSession(client)
        )
    except SecretException as exc:
        assert "rayito/super-secret-name" not in str(exc)
    else:
        pytest.fail("expected a SecretException")


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
    telemetry = TelemetryExport(auth=OtlpAuth.bearer("rayito/otlp-key"))
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


def test_build_section_resolves_bearer_only_when_auth_is_bearer() -> None:
    telemetry = TelemetryExport(auth=OtlpAuth.execution_role())
    section = build_section(
        telemetry,
        image_arn="arn:aws:lambda:us-east-1:123456789012:microvm-image/rayito-base-caps",
        image_version="1",
        image_memory_mib=2048,
        region="us-east-1",
        session=None,
    )
    assert section.bearer_token is None


def test_build_section_resolves_the_bearer_token() -> None:
    client = FakeSecretsManagerClient({"SecretString": "sk-test"})
    telemetry = TelemetryExport(auth=OtlpAuth.bearer("rayito/otlp-key"))
    section = build_section(
        telemetry,
        image_arn="arn:aws:lambda:us-east-1:123456789012:microvm-image/rayito-base",
        image_version="1",
        image_memory_mib=2048,
        region="us-east-1",
        session=FakeSession(client),
    )
    assert section.bearer_token == "sk-test"


# --------------------------------------------------------------- TelemetryHealth


def test_telemetry_health_defaults_to_all_zero() -> None:
    health = TelemetryHealth()
    assert health.exported == 0
    assert health.dropped == 0
    assert health.last_error_class is None
