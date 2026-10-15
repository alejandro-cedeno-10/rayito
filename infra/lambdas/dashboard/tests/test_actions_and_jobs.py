"""El rol de acciones por llamante y la cola síncrona de la Lambda."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import _stack_path  # noqa: F401
import pytest
from botocore.exceptions import ClientError
from dashboard_stack.actions import AssumedRoleActions, session_name
from dashboard_stack.app import ActionsUnavailable
from dashboard_stack.jobs import InlineJobs, JobLog
from dashboard_stack.tokens import Caller
from stack_fakes import FakeControlPlane

from rayito._dashboard._domain import plan_bulk
from rayito._dashboard._fleet import Fleet

CALLER = Caller("subject-0001", "ada@example.com", frozenset())
NOW = 1_800_000_000.0
SESSION_SECONDS = 900
IDS = tuple(f"microvm-00000000-0000-0000-0000-{n:012d}" for n in (1, 2))


class FakeSts:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail = fail

    def assume_role(self, **params: Any) -> dict[str, Any]:
        self.calls.append(params)
        if self.fail:
            raise ClientError({"Error": {"Code": "AccessDenied"}}, "AssumeRole")
        expires = datetime.fromtimestamp(NOW + SESSION_SECONDS, UTC)
        return {
            "Credentials": {
                "AccessKeyId": "AKIAEXAMPLE",
                "SecretAccessKey": "secret",
                "SessionToken": "session",
                "Expiration": expires,
            }
        }


def actions(sts: FakeSts, clock: list[float], built: list[object]) -> AssumedRoleActions:
    def build_api(session: object) -> Any:
        built.append(session)
        return object()

    return AssumedRoleActions(
        sts=sts,
        role_arn="arn:aws:iam::123456789012:role/actions",
        region="us-east-1",
        build_api=build_api,
        make_session=lambda credentials, region: credentials["SessionToken"],  # type: ignore[arg-type,return-value]
        clock=lambda: clock[0],
        session_seconds=SESSION_SECONDS,
    )


def test_the_session_name_carries_the_caller_and_stays_valid_for_sts() -> None:
    name = session_name("a" * 100 + "!?")
    assert name.startswith("rayito-dashboard-") and len(name) <= 64
    assert "!" not in name and "?" not in name


def test_credentials_are_reused_until_they_are_about_to_expire() -> None:
    sts, clock, built = FakeSts(), [NOW], []
    gateway = actions(sts, clock, built)
    gateway.api_for(CALLER)
    gateway.api_for(CALLER)
    assert len(sts.calls) == 1 and sts.calls[0]["RoleSessionName"].endswith("subject-0001")
    clock[0] = NOW + SESSION_SECONDS
    gateway.api_for(CALLER)
    assert len(sts.calls) == 2


def test_each_caller_gets_its_own_session() -> None:
    sts, built = FakeSts(), []
    gateway = actions(sts, [NOW], built)
    gateway.api_for(CALLER)
    gateway.api_for(Caller("subject-0002", "bob@example.com", frozenset()))
    assert len(sts.calls) == 2


def test_a_refused_assume_role_is_actions_unavailable() -> None:
    with pytest.raises(ActionsUnavailable):
        actions(FakeSts(fail=True), [NOW], []).api_for(CALLER)


def inline_jobs(plane: FakeControlPlane) -> InlineJobs:
    fleet = Fleet(control_plane=plane, allow_actions=True, sleep=lambda _: None)
    return InlineJobs(fleet, JobLog(), clock=lambda: datetime(2026, 10, 13, tzinfo=UTC))


def test_a_job_runs_inside_the_request_and_returns_finished() -> None:
    plane = FakeControlPlane(IDS)
    jobs = inline_jobs(plane)
    snapshot = jobs.submit("kill", IDS, plan=plan_bulk("kill", 2, None), applied_rate=None)
    assert snapshot["status"] == "finished"
    assert [call for call, _ in plane.acted] == ["terminate_microvm"] * 2
    assert jobs.get(snapshot["jobId"]) == snapshot
    assert jobs.snapshots() == [snapshot]


def test_an_interrupted_job_marks_what_it_did_not_reach_as_aborted() -> None:
    class Exploding(FakeControlPlane):
        def terminate_microvm(self, sandbox_id: str) -> bool:
            raise RuntimeError("boom")

    jobs = inline_jobs(Exploding(IDS))
    snapshot = jobs.submit("kill", IDS, plan=plan_bulk("kill", 2, None), applied_rate=None)
    assert snapshot["status"] == "finished"
    assert len(snapshot["items"]) == 2


def test_the_log_keeps_only_the_most_recent_jobs() -> None:
    log = JobLog(retained=2)
    for number in range(3):
        log.put({"jobId": f"job-{number}"})
    assert [job["jobId"] for job in log.snapshots()] == ["job-2", "job-1"]
    assert log.get("job-0") is None
