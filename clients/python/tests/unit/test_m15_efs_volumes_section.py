"""`volumes=` antes de lanzar (`plan_volumes`), la sección `efs_volumes` de
`ConfigureSandbox` (`EfsVolumesSection`) y la IP de mount target
(`_mount_targets`) — `m15-efs-volumes`, ADR-018, experimental. El camino
completo por `create()` está en `test_efs_volumes_create.py`."""

from __future__ import annotations

from typing import Any

import pytest

from rayito._configure_base import (
    AgentFeatures,
    CapabilityGate,
    SlowApplySection,
    configure_timeout_s,
    require_capabilities,
)
from rayito._limits import EFS_VOLUMES_MAX_PER_SANDBOX
from rayito._s3_mounts import plan_s3_mounts
from rayito._s3_mounts._domain import S3Mount
from rayito._volumes._domain import EfsVolume
from rayito._volumes._mount_targets import (
    MountTargetResolver,
    choose_mount_target_ip,
    needs_mount_targets,
    resolve_mount_targets,
)
from rayito._volumes._section import (
    VOLUME_APPLY_TIMEOUT_SECONDS,
    VPC_GUIDE,
    EfsVolumesSection,
    plan_volumes,
)
from rayito.exceptions import (
    InvalidArgumentException,
    RateLimitException,
    UnimplementedError,
    VolumeException,
    VolumeMountException,
)
from rayito.v1 import configure_pb2, efs_volumes_pb2

from .fake_efs import client_error

VOLUME = EfsVolume(file_system_id="fs-0123abcd", access_point_id="fsap-0123abcd")
OTHER_FS = EfsVolume(file_system_id="fs-0456abcd", access_point_id="fsap-0456abcd")
PINNED = EfsVolume(
    file_system_id="fs-0123abcd", access_point_id="fsap-0789abcd", mount_target_ip="10.0.9.9"
)
#: Marcadores de documentación (cuenta y región ficticias).
CONNECTOR = "arn:aws:lambda:us-east-1:123456789012:network-connector:rayito-efs"
ROLE = "arn:aws:iam::123456789012:role/rayito-execution"
INTERNET_ARN = (
    "arn:aws:lambda:us-east-1:aws:network-connector:aws-network-connector:INTERNET_EGRESS"
)


def plan(volumes: Any, **overrides: Any) -> Any:
    kwargs: dict[str, Any] = {
        "image_variant": "base-caps",
        "egress": [CONNECTOR],
        "execution_role_arn": ROLE,
        **overrides,
    }
    return plan_volumes(volumes, **kwargs)


# ------------------------------------------------------------ plan_volumes


def test_a_well_formed_request_is_planned_without_any_io() -> None:
    planned = plan({"/mnt/v": VOLUME})
    assert dict(planned.volumes) == {"/mnt/v": VOLUME}


def test_an_empty_map_is_invalid() -> None:
    with pytest.raises(InvalidArgumentException, match="vacío"):
        plan({})


def test_more_volumes_than_the_cap_are_invalid() -> None:
    volumes = {f"/mnt/v{i}": VOLUME for i in range(EFS_VOLUMES_MAX_PER_SANDBOX + 1)}
    with pytest.raises(InvalidArgumentException, match=str(EFS_VOLUMES_MAX_PER_SANDBOX)):
        plan(volumes)


def test_a_non_efs_volume_value_is_invalid() -> None:
    with pytest.raises(InvalidArgumentException, match="EfsVolume"):
        plan({"/mnt/v": object()})


def test_an_overlapping_path_is_invalid() -> None:
    with pytest.raises(InvalidArgumentException):
        plan({"/mnt/v": VOLUME, "/mnt/v/sub": VOLUME})


def test_a_non_caps_image_variant_is_rejected() -> None:
    with pytest.raises(UnimplementedError, match="base-caps"):
        plan({"/mnt/v": VOLUME}, image_variant="base")


@pytest.mark.parametrize("variant", ["base-caps", "base-caps-efs", None])
def test_caps_variants_and_unknown_images_pass(variant: str | None) -> None:
    plan({"/mnt/v": VOLUME}, image_variant=variant)


@pytest.mark.parametrize("egress", [None, []])
def test_a_volume_without_its_connector_is_rejected_naming_the_alternative(
    egress: list[str] | None,
) -> None:
    """Without `egress=` the MicroVM inherits INTERNET_EGRESS (Q60) and
    never reaches the mount target."""
    with pytest.raises(InvalidArgumentException, match="egress=") as caught:
        plan({"/mnt/v": VOLUME}, egress=egress)
    assert VPC_GUIDE in str(caught.value)


@pytest.mark.parametrize("internet", ["INTERNET_EGRESS", INTERNET_ARN])
def test_a_volume_with_internet_egress_is_rejected_before_launch(internet: str) -> None:
    """Q131: `egressNetworkConnectors=[INTERNET_EGRESS, <VPC>]` is a
    ValidationException; the SDK says so first and names the way out."""
    with pytest.raises(InvalidArgumentException, match="INTERNET_EGRESS") as caught:
        plan({"/mnt/v": VOLUME}, egress=[CONNECTOR, internet])
    message = str(caught.value)
    assert "NAT" in message and "transit gateway" in message


def test_a_volume_with_two_own_connectors_is_rejected() -> None:
    with pytest.raises(InvalidArgumentException, match="un solo conector"):
        plan({"/mnt/v": VOLUME}, egress=[CONNECTOR, CONNECTOR + "-b"])


@pytest.mark.parametrize("role", [None, ""])
def test_a_volume_without_an_execution_role_is_rejected(role: str | None) -> None:
    with pytest.raises(InvalidArgumentException, match="execution_role_arn"):
        plan({"/mnt/v": VOLUME}, execution_role_arn=role)


def test_the_checks_run_in_the_order_the_caller_can_fix_them() -> None:
    with pytest.raises(UnimplementedError, match="base-caps"):
        plan({"/mnt/v": VOLUME}, image_variant="base", egress=None, execution_role_arn=None)
    with pytest.raises(InvalidArgumentException, match="egress="):
        plan({"/mnt/v": VOLUME}, egress=None, execution_role_arn=None)


def test_an_efs_volume_rejects_a_malformed_mount_target_ip_without_echoing_it() -> None:
    with pytest.raises(InvalidArgumentException) as caught:
        EfsVolume(
            file_system_id="fs-0123abcd", access_point_id="fsap-0123abcd", mount_target_ip="nope"
        )
    assert "nope" not in str(caught.value)


# ---------------------------------------------------------- mount targets


class FakeMountTargets:
    def __init__(self, by_fs: dict[str, list[dict[str, Any]]], error: Exception | None = None):
        self.by_fs = by_fs
        self.error = error
        self.calls: list[str] = []

    def describe_mount_targets(self, **params: Any) -> dict[str, Any]:
        self.calls.append(params["FileSystemId"])
        assert set(params) == {"FileSystemId"}
        if self.error is not None:
            raise self.error
        return {"MountTargets": self.by_fs.get(params["FileSystemId"], [])}


def target(az: str, ip: str, state: str = "available", mt: str = "fsmt-1") -> dict[str, Any]:
    return {
        "MountTargetId": mt,
        "SubnetId": "subnet-0123456789abcdef0",
        "LifeCycleState": state,
        "IpAddress": ip,
        "AvailabilityZoneId": az,
    }


def test_the_first_available_mount_target_by_az_id_is_chosen() -> None:
    response = {
        "MountTargets": [
            target("use1-az6", "10.0.6.10"),
            target("use1-az1", "10.0.1.10", state="creating"),
            target("use1-az2", "10.0.2.10"),
        ]
    }
    assert choose_mount_target_ip(response) == "10.0.2.10"


def test_no_available_mount_target_chooses_nothing() -> None:
    response = {"MountTargets": [target("use1-az1", "10.0.1.10", state="deleting")]}
    assert choose_mount_target_ip(response) is None
    assert choose_mount_target_ip({}) is None


def test_the_resolver_calls_efs_once_per_file_system() -> None:
    api = FakeMountTargets(
        {"fs-0123abcd": [target("use1-az1", "10.0.1.10")], "fs-0456abcd": [target("a", "10.0.4.4")]}
    )
    volumes = {"/mnt/a": VOLUME, "/mnt/b": VOLUME, "/mnt/c": OTHER_FS, "/mnt/d": PINNED}
    resolved = resolve_mount_targets(volumes, MountTargetResolver(api))
    assert api.calls == ["fs-0123abcd", "fs-0456abcd"]
    assert resolved["/mnt/a"].mount_target_ip == "10.0.1.10"
    assert resolved["/mnt/b"].mount_target_ip == "10.0.1.10"
    assert resolved["/mnt/c"].mount_target_ip == "10.0.4.4"
    assert resolved["/mnt/d"] is PINNED


def test_volumes_that_all_carry_an_ip_need_no_lookup() -> None:
    assert not needs_mount_targets({"/mnt/d": PINNED})
    assert needs_mount_targets({"/mnt/d": PINNED, "/mnt/a": VOLUME})


def test_a_file_system_without_mount_targets_is_a_volume_error() -> None:
    with pytest.raises(VolumeException, match="mount target"):
        MountTargetResolver(FakeMountTargets({})).ip_for("fs-0123abcd")


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("FileSystemNotFound", VolumeException),
        ("AccessDeniedException", VolumeException),
        ("ThrottlingException", RateLimitException),
    ],
)
def test_efs_errors_are_translated_without_the_aws_message(
    code: str, expected: type[Exception]
) -> None:
    api = FakeMountTargets({}, error=client_error(code, "fs-0123abcd secreto", "X"))
    with pytest.raises(expected) as caught:
        MountTargetResolver(api).ip_for("fs-0123abcd")
    assert "fs-0123abcd" not in str(caught.value)


# --------------------------------------------------------------- section


def section() -> EfsVolumesSection:
    return EfsVolumesSection({"/mnt/v": PINNED, "/mnt/ro": OTHER_FS})


def test_the_section_fills_every_volume_into_the_request() -> None:
    request = configure_pb2.ConfigureRequest()
    section().fill(request)
    mounts = {m.mount_path: m for m in request.efs_volumes.mounts}
    assert mounts["/mnt/v"].mount_target_ip == "10.0.9.9"
    assert mounts["/mnt/v"].access_point_id == "fsap-0789abcd"
    assert mounts["/mnt/ro"].mount_target_ip == ""
    assert mounts["/mnt/ro"].file_system_id == "fs-0456abcd"


def test_a_read_only_volume_travels_as_read_only() -> None:
    request = configure_pb2.ConfigureRequest()
    read_only = EfsVolume(
        file_system_id="fs-0123abcd", access_point_id="fsap-0123abcd", read_only=True
    )
    EfsVolumesSection({"/mnt/ro": read_only}).fill(request)
    assert request.efs_volumes.mounts[0].read_only


@pytest.mark.parametrize(
    "code", [configure_pb2.SECTION_CODE_APPLIED, configure_pb2.SECTION_CODE_PENDING]
)
def test_applied_and_pending_are_not_errors(code: int) -> None:
    section().check_result(code, "")


def test_unsupported_names_the_opt_in_image() -> None:
    with pytest.raises(UnimplementedError, match="--with-efs"):
        section().check_result(configure_pb2.SECTION_CODE_UNSUPPORTED, "")


@pytest.mark.parametrize(
    ("error_class", "code"),
    [("iam_denied", "iam_denied"), ("invalid_path", "invalid_path"), ("weird", "unknown")],
)
def test_a_failed_section_is_a_volume_mount_exception_with_a_closed_code(
    error_class: str, code: str
) -> None:
    with pytest.raises(VolumeMountException) as caught:
        section().check_result(configure_pb2.SECTION_CODE_FAILED, error_class)
    assert caught.value.code == code
    assert isinstance(caught.value, VolumeException)


def status(**states: tuple[Any, str]) -> configure_pb2.ConfigureStatusResponse:
    return configure_pb2.ConfigureStatusResponse(
        efs_volumes=efs_volumes_pb2.EfsVolumesStatus(
            volumes=[
                efs_volumes_pb2.EfsVolumeStatus(
                    mount_path=path.replace("_", "/"), state=state, last_error_class=error
                )
                for path, (state, error) in states.items()
            ]
        )
    )


MOUNTED = (efs_volumes_pb2.EFS_VOLUME_STATE_MOUNTED, "")
MOUNTING = (efs_volumes_pb2.EFS_VOLUME_STATE_MOUNTING, "")


def test_check_status_waits_until_every_volume_is_mounted() -> None:
    assert not section().check_status(status(_mnt_v=MOUNTED, _mnt_ro=MOUNTING), final=False)
    assert section().check_status(status(_mnt_v=MOUNTED, _mnt_ro=MOUNTED), final=False)


def test_check_status_raises_the_failed_class() -> None:
    failed = (efs_volumes_pb2.EFS_VOLUME_STATE_FAILED, "network")
    with pytest.raises(VolumeMountException) as caught:
        section().check_status(status(_mnt_v=MOUNTED, _mnt_ro=failed), final=False)
    assert caught.value.code == "network"


def test_check_status_times_out_on_the_final_poll() -> None:
    with pytest.raises(VolumeMountException) as caught:
        section().check_status(status(_mnt_v=MOUNTED), final=True)
    assert caught.value.code == "timeout"


def test_the_section_gates_on_its_own_flag_with_its_own_message() -> None:
    entry = section()
    assert isinstance(entry, CapabilityGate)
    with pytest.raises(UnimplementedError, match="amazon-efs-utils"):
        require_capabilities([entry], AgentFeatures(configure=True))
    require_capabilities([entry], AgentFeatures(configure=True, efs_volumes=True))


def test_the_configure_deadline_covers_rayd_mounting_every_volume() -> None:
    """`rayd` mounts inside `Configure`, one by one, up to 15 s each."""
    assert isinstance(section(), SlowApplySection)
    assert VOLUME_APPLY_TIMEOUT_SECONDS >= EFS_VOLUMES_MAX_PER_SANDBOX * 15.0
    mounts = plan_s3_mounts({"/mnt/s3": S3Mount(bucket="b")})
    assert mounts is not None and not isinstance(mounts, SlowApplySection)
    assert configure_timeout_s([mounts], 60.0) == 60.0
    assert configure_timeout_s([mounts, section()], 60.0) == VOLUME_APPLY_TIMEOUT_SECONDS
    assert configure_timeout_s([section()], 600.0) == 600.0
