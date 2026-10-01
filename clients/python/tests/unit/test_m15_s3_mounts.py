"""`rayito._s3_mounts` (`m15-s3-mounts`, ADR-017): el dominio de `S3Mount`
y la traducción pura hacia/desde `ConfigureRequest.s3_mounts`. El paquete
no está todavía enlazado a `Sandbox.create()` (`_feature_options.mounts`
sigue lanzando `UnimplementedError`, `test_m15_feature_options.py`), así
que estos tests llaman a `_s3_mounts` directamente.
"""

from __future__ import annotations

import pytest

from rayito._s3_mounts import (
    MountStatus,
    S3Mount,
    S3MountsSection,
    check_section_result,
    from_proto_status,
    plan_s3_mounts,
    to_proto,
)
from rayito.exceptions import InvalidArgumentException, MountException, UnimplementedError
from rayito.v1 import s3_mounts_pb2


def test_s3mount_defaults_are_read_only_and_bucket_root() -> None:
    mount = S3Mount(bucket="team-data")
    assert mount.read_only is True
    assert mount.prefix == ""
    assert mount.allow_overwrite is False
    assert mount.allow_delete is False


def test_an_empty_bucket_is_rejected() -> None:
    with pytest.raises(InvalidArgumentException, match="bucket"):
        S3Mount(bucket="")


def test_a_prefix_with_a_leading_slash_is_rejected() -> None:
    with pytest.raises(InvalidArgumentException, match="prefix"):
        S3Mount(bucket="team-data", prefix="/team7/")


@pytest.mark.parametrize("field", ["allow_overwrite", "allow_delete"])
def test_write_flags_require_read_only_false(field: str) -> None:
    with pytest.raises(InvalidArgumentException, match="read_only=False"):
        S3Mount(bucket="team-data", **{field: True})  # type: ignore[arg-type]


def test_write_flags_are_accepted_with_read_only_false() -> None:
    mount = S3Mount(bucket="team-data", read_only=False, allow_overwrite=True, allow_delete=True)
    assert mount.allow_overwrite is True
    assert mount.allow_delete is True


def test_plan_s3_mounts_of_none_is_none() -> None:
    assert plan_s3_mounts(None) is None


def test_plan_s3_mounts_validates_paths_before_building_a_section() -> None:
    with pytest.raises(InvalidArgumentException, match="absoluta"):
        plan_s3_mounts({"relative/path": S3Mount(bucket="team-data")})


def test_plan_s3_mounts_rejects_overlapping_paths_with_volumes_rule() -> None:
    mounts = {
        "/mnt/data": S3Mount(bucket="team-data"),
        "/mnt/data/sub": S3Mount(bucket="team-data", prefix="sub/"),
    }
    with pytest.raises(InvalidArgumentException, match="solaparse"):
        plan_s3_mounts(mounts)


def test_plan_s3_mounts_returns_a_section_with_the_required_flag_and_name() -> None:
    section = plan_s3_mounts({"/mnt/data": S3Mount(bucket="team-data", prefix="team7/")})
    assert isinstance(section, S3MountsSection)
    assert section.section == "s3_mounts"
    assert section.required_flag == "s3_mounts"


def test_to_proto_round_trips_every_field() -> None:
    mounts = {
        "/mnt/data": S3Mount(bucket="team-data", prefix="team7/"),
        "/mnt/out": S3Mount(
            bucket="team-data", prefix="runs/", read_only=False, allow_overwrite=True
        ),
    }
    wire = to_proto(mounts)
    assert len(wire.mounts) == 2
    by_path = {entry.mount_path: entry for entry in wire.mounts}
    assert by_path["/mnt/data"].bucket == "team-data"
    assert by_path["/mnt/data"].prefix == "team7/"
    assert by_path["/mnt/data"].read_only is True
    assert by_path["/mnt/out"].read_only is False
    assert by_path["/mnt/out"].allow_overwrite is True


def test_section_fill_copies_the_proto_config_onto_the_request() -> None:
    from rayito.v1 import configure_pb2

    section = plan_s3_mounts({"/mnt/data": S3Mount(bucket="team-data")})
    assert section is not None
    request = configure_pb2.ConfigureRequest()
    section.fill(request)
    assert request.s3_mounts.mounts[0].bucket == "team-data"
    assert request.s3_mounts.mounts[0].mount_path == "/mnt/data"


def test_from_proto_status_maps_every_phase() -> None:
    status = s3_mounts_pb2.S3MountsStatus(
        mounts=[
            s3_mounts_pb2.S3MountState(
                mount_path="/mnt/a", phase=s3_mounts_pb2.S3_MOUNT_PHASE_MOUNTED
            ),
            s3_mounts_pb2.S3MountState(
                mount_path="/mnt/b", phase=s3_mounts_pb2.S3_MOUNT_PHASE_PENDING
            ),
            s3_mounts_pb2.S3MountState(
                mount_path="/mnt/c",
                phase=s3_mounts_pb2.S3_MOUNT_PHASE_FAILED,
                error_class="iam_denied",
            ),
        ]
    )
    result = from_proto_status(status)
    assert result == {
        "/mnt/a": MountStatus(state="mounted"),
        "/mnt/b": MountStatus(state="pending"),
        "/mnt/c": MountStatus(state="failed", last_error_class="iam_denied"),
    }


@pytest.mark.parametrize("code_name", ["SECTION_CODE_APPLIED", "SECTION_CODE_PENDING"])
def test_applied_and_pending_raise_nothing(code_name: str) -> None:
    check_section_result(code_name, "")


def test_unsupported_raises_unimplemented_naming_the_option() -> None:
    with pytest.raises(UnimplementedError) as excinfo:
        check_section_result("SECTION_CODE_UNSUPPORTED", "")
    assert excinfo.value.feature == "mounts="


def test_failed_raises_mount_exception_with_the_wire_error_class() -> None:
    with pytest.raises(MountException) as excinfo:
        check_section_result("SECTION_CODE_FAILED", "iam_denied")
    assert excinfo.value.code == "iam_denied"


def test_invalid_without_a_known_error_class_falls_back_to_network() -> None:
    with pytest.raises(MountException) as excinfo:
        check_section_result("SECTION_CODE_INVALID", "")
    assert excinfo.value.code == "network"


def test_importing_s3_mounts_builds_no_aws_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Off-by-default acceptance criterion (§9.2 of the M15 architecture):
    nothing in this module ever touches `boto3`."""
    import boto3

    def fail(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("rayito._s3_mounts must never construct a boto3 client")

    monkeypatch.setattr(boto3.session.Session, "client", fail)
    S3Mount(bucket="team-data")
    plan_s3_mounts({"/mnt/data": S3Mount(bucket="team-data")})
