"""`rayito._configure_base`: presencia de `Health.features` (agente pre-0.6
frente a 0.6 con todo en stub) y la traducción de `SectionCode` a excepción."""

from __future__ import annotations

from rayito._configure_base import (
    AgentFeatures,
    agent_features_from_health,
    require_configure_support,
    section_error,
)
from rayito.exceptions import SandboxException, UnimplementedError
from rayito.v1 import features_pb2, health_pb2


def test_a_pre_0_6_agent_has_no_features_field() -> None:
    response = health_pb2.HealthResponse(agent_ready=True)
    assert agent_features_from_health(response) is None


def test_a_0_6_agent_with_every_slot_a_stub_reports_only_configure() -> None:
    response = health_pb2.HealthResponse(features=features_pb2.AgentFeatures(configure=True))
    features = agent_features_from_health(response)
    assert features == AgentFeatures(configure=True)


def test_require_configure_support_raises_for_a_pre_0_6_agent() -> None:
    try:
        require_configure_support(None, "events=")
    except UnimplementedError as exc:
        assert exc.feature == "events="
        assert "0.6.0" in exc.reason
    else:
        raise AssertionError("expected UnimplementedError")


def test_require_configure_support_passes_through_otherwise() -> None:
    features = AgentFeatures(configure=True)
    assert require_configure_support(features, "events=") is features


def test_section_error_is_none_for_applied_and_pending() -> None:
    assert section_error("events", "SECTION_CODE_APPLIED", "") is None
    assert section_error("events", "SECTION_CODE_PENDING", "") is None


def test_section_error_is_unimplemented_for_unsupported() -> None:
    error = section_error("s3_mounts", "SECTION_CODE_UNSUPPORTED", "")
    assert isinstance(error, UnimplementedError)


def test_section_error_is_a_sandbox_exception_otherwise() -> None:
    error = section_error("events", "SECTION_CODE_FAILED", "iam_denied")
    assert isinstance(error, SandboxException)
    assert "iam_denied" in str(error)
