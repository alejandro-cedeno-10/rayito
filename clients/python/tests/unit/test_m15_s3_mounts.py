"""`rayito._s3_mounts` (`m15-s3-mounts`, ADR-017): el dominio de `S3Mount`
y la traducción pura hacia/desde `ConfigureRequest.s3_mounts`, probado aquí
directamente sobre `_s3_mounts`; el enlace end-to-end con
`Sandbox.create(mounts=)` (`_feature_options.plan_features`, la puerta de
capacidad, la `Configure` real) lo cubre `test_m15_feature_options.py` y
los e2e `test_m15_s3_mounts.py`/`m15-s3-mounts.e2e.test.ts`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from rayito._configure_base import AgentFeatures
from rayito._feature_options import FeaturePlan
from rayito._s3_mounts import (
    MountStatus,
    S3Mount,
    S3MountsSection,
    check_section_result,
    from_proto_status,
    plan_s3_mounts,
    to_proto,
)
from rayito._s3_mounts._domain import MOUNT_ERROR_HINTS
from rayito._s3_mounts._section import check_mounts_settled
from rayito.exceptions import InvalidArgumentException, MountException, UnimplementedError
from rayito.sandbox_async import main as async_main
from rayito.sandbox_sync import main as sync_main
from rayito.v1 import configure_pb2, features_pb2, s3_mounts_pb2

#: Compartido con `rayd-core` y el SDK de TypeScript: los tres validadores
#: leen los mismos casos (ver su `description`).
SHARED_VECTORS = Path(__file__).resolve().parents[4] / "testdata" / "s3-mounts" / "mount-specs.json"
VECTOR_CASES: list[dict[str, Any]] = json.loads(SHARED_VECTORS.read_text(encoding="utf-8"))["cases"]


def _plan_vector(case: dict[str, Any]) -> None:
    plan_s3_mounts(
        {
            raw["path"]: S3Mount(
                bucket=raw["bucket"],
                prefix=raw["prefix"],
                read_only=raw["read_only"],
                allow_overwrite=raw["allow_overwrite"],
                allow_delete=raw["allow_delete"],
            )
            for raw in case["mounts"]
        }
    )


@pytest.mark.parametrize("case", VECTOR_CASES, ids=lambda case: case["name"])
def test_the_shared_vectors_get_the_sdk_answer_they_document(case: dict[str, Any]) -> None:
    if case["sdk"] == "ok":
        _plan_vector(case)
    else:
        with pytest.raises(InvalidArgumentException):
            _plan_vector(case)


def test_s3mount_defaults_are_read_only_and_bucket_root() -> None:
    mount = S3Mount(bucket="team-data")
    assert mount.read_only is True
    assert mount.prefix == ""
    assert mount.allow_overwrite is False
    assert mount.allow_delete is False


def test_write_flags_are_accepted_with_read_only_false() -> None:
    mount = S3Mount(bucket="team-data", read_only=False, allow_overwrite=True, allow_delete=True)
    assert mount.allow_overwrite is True
    assert mount.allow_delete is True


def test_plan_s3_mounts_of_none_is_none() -> None:
    assert plan_s3_mounts(None) is None


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


@pytest.mark.parametrize(
    "code", [configure_pb2.SECTION_CODE_APPLIED, configure_pb2.SECTION_CODE_PENDING]
)
def test_applied_and_pending_raise_nothing(code: int) -> None:
    check_section_result(code, "")


def test_unsupported_raises_unimplemented_naming_the_option() -> None:
    with pytest.raises(UnimplementedError) as excinfo:
        check_section_result(configure_pb2.SECTION_CODE_UNSUPPORTED, "")
    assert excinfo.value.feature == "mounts="


def test_failed_raises_mount_exception_with_the_wire_error_class() -> None:
    with pytest.raises(MountException) as excinfo:
        check_section_result(configure_pb2.SECTION_CODE_FAILED, "iam_denied")
    assert excinfo.value.code == "iam_denied"


def test_a_bucket_off_the_image_allowlist_says_so_and_how_to_fix_it() -> None:
    with pytest.raises(MountException) as excinfo:
        check_section_result(configure_pb2.SECTION_CODE_INVALID, "not_allowed")
    error = excinfo.value
    assert error.code == "not_allowed"
    assert "allowlist de la imagen" in str(error)
    assert "--env RAYITO_ALLOWED_MOUNT_BUCKETS=<bucket>" in str(error)
    assert "make image-publish-caps MOUNT_BUCKETS=<bucket>" in str(error)
    assert str(error) == (
        "mounts=: la sección se rechazó (SECTION_CODE_INVALID, not_allowed): "
        + MOUNT_ERROR_HINTS["not_allowed"]
    )


def test_a_class_without_a_hint_keeps_the_plain_message() -> None:
    with pytest.raises(MountException) as excinfo:
        check_section_result(configure_pb2.SECTION_CODE_INVALID, "invalid_path")
    assert (
        str(excinfo.value) == "mounts=: la sección se rechazó (SECTION_CODE_INVALID, invalid_path)"
    )


def test_the_hints_are_the_shared_ones() -> None:
    shared = json.loads(
        (
            Path(__file__).resolve().parents[4] / "testdata" / "s3-mounts" / "error-hints.json"
        ).read_text(encoding="utf-8")
    )
    assert shared["hints"] == MOUNT_ERROR_HINTS


def test_invalid_without_a_known_error_class_falls_back_to_unknown() -> None:
    with pytest.raises(MountException) as excinfo:
        check_section_result(configure_pb2.SECTION_CODE_INVALID, "")
    assert excinfo.value.code == "unknown"


def test_importing_s3_mounts_builds_no_aws_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Off-by-default acceptance criterion (§9.2 of the M15 architecture):
    nothing in this module ever touches `boto3`."""
    import boto3

    def fail(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("rayito._s3_mounts must never construct a boto3 client")

    monkeypatch.setattr(boto3.session.Session, "client", fail)
    S3Mount(bucket="team-data")
    plan_s3_mounts({"/mnt/data": S3Mount(bucket="team-data")})


WANTED = {"/mnt/data": S3Mount(bucket="team-data")}


def test_check_mounts_settled_is_true_once_every_wanted_path_is_mounted() -> None:
    assert check_mounts_settled({"/mnt/data": MountStatus(state="mounted")}, WANTED, final=False)


def test_check_mounts_settled_keeps_waiting_while_pending_or_unreported() -> None:
    assert not check_mounts_settled(
        {"/mnt/data": MountStatus(state="pending")}, WANTED, final=False
    )
    assert not check_mounts_settled({}, WANTED, final=False)


def test_a_failed_mount_raises_its_own_error_class() -> None:
    states = {"/mnt/data": MountStatus(state="failed", last_error_class="iam_denied")}
    with pytest.raises(MountException) as excinfo:
        check_mounts_settled(states, WANTED, final=False)
    assert excinfo.value.code == "iam_denied"


def test_still_pending_at_the_deadline_is_a_timeout() -> None:
    with pytest.raises(MountException) as excinfo:
        check_mounts_settled({"/mnt/data": MountStatus(state="pending")}, WANTED, final=True)
    assert excinfo.value.code == "timeout"


def _configure_response(
    code: configure_pb2.SectionCode,
) -> configure_pb2.ConfigureResponse:
    return configure_pb2.ConfigureResponse(
        results=[
            configure_pb2.SectionResult(section=configure_pb2.CONFIG_SECTION_S3_MOUNTS, code=code)
        ]
    )


def _status(
    phase: s3_mounts_pb2.S3MountPhase, error_class: str = ""
) -> configure_pb2.ConfigureStatusResponse:
    return configure_pb2.ConfigureStatusResponse(
        s3_mounts=s3_mounts_pb2.S3MountsStatus(
            mounts=[
                s3_mounts_pb2.S3MountState(
                    mount_path="/mnt/data", phase=phase, error_class=error_class
                )
            ]
        )
    )


READY = AgentFeatures.from_proto(features_pb2.AgentFeatures(configure=True, s3_mounts=True))
PENDING = _status(s3_mounts_pb2.S3_MOUNT_PHASE_PENDING)
MOUNTED = _status(s3_mounts_pb2.S3_MOUNT_PHASE_MOUNTED)


class ScriptedStub:
    """`Configure` answers `PENDING`; each `ConfigureStatus` pops the next
    scripted status (the last one repeats)."""

    def __init__(self, statuses: list[configure_pb2.ConfigureStatusResponse]) -> None:
        self.statuses = statuses
        self.status_calls = 0

    def Configure(self, request: object, *, timeout: float) -> object:
        return _configure_response(configure_pb2.SECTION_CODE_PENDING)

    def ConfigureStatus(self, request: object, *, timeout: float) -> object:
        self.status_calls += 1
        return self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]


class AsyncScriptedStub(ScriptedStub):
    async def Configure(self, request: object, *, timeout: float) -> object:
        return super().Configure(request, timeout=timeout)

    async def ConfigureStatus(self, request: object, *, timeout: float) -> object:
        return super().ConfigureStatus(request, timeout=timeout)


def _plan() -> FeaturePlan:
    section = plan_s3_mounts(WANTED)
    assert section is not None
    return FeaturePlan(configure_sections=(section,))


def _sync_sandbox(stub: ScriptedStub) -> sync_main.Sandbox:
    sandbox = sync_main.Sandbox.__new__(sync_main.Sandbox)
    sandbox._configure = stub
    sandbox._agent_features = READY
    sandbox._section_handles = {}
    return sandbox


def _async_sandbox(stub: ScriptedStub) -> async_main.AsyncSandbox:
    sandbox = async_main.AsyncSandbox.__new__(async_main.AsyncSandbox)
    sandbox._configure = stub
    sandbox._agent_features = READY
    sandbox._section_handles = {}
    return sandbox


@pytest.fixture(autouse=True)
def _no_settle_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sync_main, "CONFIGURE_SETTLE_POLL_S", 0.0)
    monkeypatch.setattr(async_main, "CONFIGURE_SETTLE_POLL_S", 0.0)


def test_create_waits_for_a_pending_mount_to_be_mounted() -> None:
    stub = ScriptedStub([PENDING, PENDING, MOUNTED])
    _sync_sandbox(stub)._send_configure_sections(_plan().configure_sections, timeout=1.0)
    assert stub.status_calls == 3


def test_create_raises_the_mounts_own_failure_instead_of_returning() -> None:
    stub = ScriptedStub([PENDING, _status(s3_mounts_pb2.S3_MOUNT_PHASE_FAILED, "not_found")])
    with pytest.raises(MountException) as excinfo:
        _sync_sandbox(stub)._send_configure_sections(_plan().configure_sections, timeout=1.0)
    assert excinfo.value.code == "not_found"


def test_create_gives_up_as_timeout_once_the_settle_bound_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sync_main, "settle_timeout_s", lambda _pending: 0.0)
    with pytest.raises(MountException) as excinfo:
        _sync_sandbox(ScriptedStub([PENDING]))._send_configure_sections(
            _plan().configure_sections, timeout=1.0
        )
    assert excinfo.value.code == "timeout"


async def test_async_create_waits_for_a_pending_mount_to_be_mounted() -> None:
    stub = AsyncScriptedStub([PENDING, MOUNTED])
    await _async_sandbox(stub)._send_configure_sections(_plan().configure_sections, timeout=1.0)
    assert stub.status_calls == 2


async def test_async_create_raises_the_mounts_own_failure() -> None:
    stub = AsyncScriptedStub([_status(s3_mounts_pb2.S3_MOUNT_PHASE_FAILED, "iam_denied")])
    with pytest.raises(MountException) as excinfo:
        await _async_sandbox(stub)._send_configure_sections(_plan().configure_sections, timeout=1.0)
    assert excinfo.value.code == "iam_denied"
