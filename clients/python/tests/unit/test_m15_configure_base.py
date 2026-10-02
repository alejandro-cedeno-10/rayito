"""`rayito._configure_base`: presencia de `Health.features` (agente pre-0.6
frente a 0.6 con todo en stub), la traducción de `SectionCode` a excepción,
y el ejecutor compartido de `configure_sections` (`require_capabilities`,
`build_configure_request`, `check_configure_response`) que `create()`/
`take()` corren tras el primer `Health` (ver `sandbox_sync/main.py`,
`sandbox_async/main.py`)."""

from __future__ import annotations

import pytest

from rayito._configure_base import (
    AgentFeatures,
    ConfigureSection,
    agent_features_from_health,
    build_configure_request,
    check_configure_response,
    require_capabilities,
    require_configure_support,
    section_error,
)
from rayito._s3_mounts import S3Mount, plan_s3_mounts
from rayito.exceptions import MountException, SandboxException, UnimplementedError
from rayito.v1 import configure_pb2, features_pb2, health_pb2, s3_mounts_pb2


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


def _s3_mounts_section() -> ConfigureSection:
    section = plan_s3_mounts({"/mnt/data": S3Mount(bucket="team-data")})
    assert section is not None
    return section


def test_require_capabilities_is_a_no_op_with_no_sections() -> None:
    # The off-by-default contract: a `FeaturePlan()` with nothing configured
    # never even looks at `features`.
    require_capabilities((), AgentFeatures())


def test_require_capabilities_raises_naming_the_section_when_its_flag_is_false() -> None:
    section = _s3_mounts_section()
    with pytest.raises(UnimplementedError) as excinfo:
        require_capabilities((section,), AgentFeatures(configure=True, s3_mounts=False))
    assert excinfo.value.feature == "s3_mounts"


def test_require_capabilities_passes_when_the_flag_is_true() -> None:
    section = _s3_mounts_section()
    require_capabilities((section,), AgentFeatures(configure=True, s3_mounts=True))


def test_build_configure_request_fills_every_section_with_a_fresh_request_id() -> None:
    section = _s3_mounts_section()
    first = build_configure_request((section,))
    second = build_configure_request((section,))
    assert first.request_id != second.request_id
    assert len(first.s3_mounts.mounts) == 1
    assert first.s3_mounts.mounts[0].mount_path == "/mnt/data"


def test_build_configure_request_is_empty_with_no_sections() -> None:
    request = build_configure_request(())
    assert request.s3_mounts == s3_mounts_pb2.S3MountsConfig()


def test_check_configure_response_raises_the_sections_own_exception() -> None:
    section = _s3_mounts_section()
    response = configure_pb2.ConfigureResponse(
        results=[
            configure_pb2.SectionResult(
                section=configure_pb2.CONFIG_SECTION_S3_MOUNTS,
                code=configure_pb2.SECTION_CODE_FAILED,
                error_class="iam_denied",
            )
        ]
    )
    with pytest.raises(MountException) as excinfo:
        check_configure_response(response, (section,))
    assert excinfo.value.code == "iam_denied"


def test_check_configure_response_ignores_a_result_for_a_section_not_sent() -> None:
    # Defensive only: `rayd` only ever answers for a section the request
    # carried, but a mismatch here must never raise an opaque KeyError.
    response = configure_pb2.ConfigureResponse(
        results=[
            configure_pb2.SectionResult(
                section=configure_pb2.CONFIG_SECTION_EFS_VOLUMES,
                code=configure_pb2.SECTION_CODE_FAILED,
            )
        ]
    )
    check_configure_response(response, ())
