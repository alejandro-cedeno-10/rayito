"""Pure, offline tests for `scripts/measure/efs_volumes.py` (`m15-efs-volumes`,
ADR-018): the tagging scheme, idempotent state file, and the `plan`/`run`/
`report`/`cleanup` subcommands against a fake `MeasurementAwsPort` — never
touches AWS; `real_port`'s own boto3/`OptionalStacks` calls are exercised
only by the serialized acceptance stage.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPO_ROOT / "scripts" / "measure" / "efs_volumes.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("efs_volumes_measure", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


measure = _load_module()

#: The caller's existing network; `run` must never create, record or delete it.
VPC_ID = "vpc-0123456789abcdef0"
SUBNET_IDS = "subnet-0123456789abcdef0,subnet-ffffffffffff"
EXISTING = ["--vpc-id", VPC_ID, "--subnet-ids", SUBNET_IDS]

#: `run` without a caps template: only the infra (the stack in the VPC).
RUN_INFRA = ["run", "--region", "us-east-1", "--infra-only", *EXISTING, "--run-id"]


@pytest.fixture(autouse=True)
def _state_home(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    yield tmp_path


@dataclass
class FakeMeasurementPort:
    """In-memory stand-in for `MeasurementAwsPort`: resources are indexed
    by run id, exactly like the real port resolves them by the
    `rayito:run-id` tag, never by a locally stored id. `fail_on` makes one
    delete raise, to exercise `cleanup`'s "stop and keep state" path."""

    stacks: dict[str, dict[str, str]] = field(default_factory=dict)
    file_systems: dict[str, str] = field(default_factory=dict)
    attached: set[str] = field(default_factory=set)
    isolated: bool = False
    calls: list[str] = field(default_factory=list)
    fail_on: str | None = None
    deployed_into: tuple[str, tuple[str, ...]] | None = None
    network_ok: bool = True
    _next_id: int = 1

    def _new_id(self, prefix: str) -> str:
        value = f"{prefix}-{self._next_id:08x}"
        self._next_id += 1
        return value

    def check_network(self, vpc_id: str, subnet_ids: Any) -> Any:
        self.calls.append("check_network")
        findings = (("internet-egress", "OK"),)
        if not self.network_ok:
            findings = (("subnet-same-az", "FAIL"),)
        return measure.NetworkCheck(
            ok=self.network_ok, status="OK" if self.network_ok else "FAIL", findings=findings
        )

    def stack_status(self, stack_name: str) -> dict[str, str] | None:
        self.calls.append("stack_status")
        return self.stacks.get(stack_name)

    def deploy_stack(
        self, stack_name: str, *, vpc_id: str, subnet_ids: Any, tags: dict[str, str]
    ) -> dict[str, str]:
        self.calls.append("deploy_stack")
        self.deployed_into = (vpc_id, tuple(subnet_ids))
        outputs = {
            "FileSystemId": self._new_id("fs"),
            "ConnectorArn": "arn:aws:lambda:us-east-1:123456789012:network-connector/fake",
            "ConnectorState": "ACTIVE",
            "CallerPolicyArn": "arn:aws:iam::123456789012:policy/fake",
            "MountTargetSecurityGroupId": self._new_id("sg"),
        }
        self.stacks[stack_name] = outputs
        return outputs

    def destroy_stack(self, stack_name: str) -> None:
        if self.fail_on == "destroy_stack":
            raise RuntimeError("StackError (fake)")
        self.calls.append("destroy_stack")
        self.stacks.pop(stack_name, None)

    def tag_file_system(self, file_system_id: str, *, tags: dict[str, str]) -> None:
        self.calls.append("tag_file_system")
        self.file_systems[tags["rayito:run-id"]] = file_system_id

    def find_tagged_file_system(self, run_id: str) -> str | None:
        return self.file_systems.get(run_id)

    def delete_file_system(self, file_system_id: str) -> None:
        if self.fail_on == "delete_file_system":
            raise RuntimeError("FileSystemInUse (fake)")
        if self.stacks:
            raise RuntimeError("FileSystemInUse: mount targets still exist (fake)")
        self.calls.append("delete_file_system")
        self.file_systems = {
            run_id: f for run_id, f in self.file_systems.items() if f != file_system_id
        }

    def ensure_access_point(self, file_system_id: str, name: str) -> str:
        self.calls.append("ensure_access_point")
        return "fsap-0123456789abcdef0"

    def mount_target_ip(self, file_system_id: str) -> str:
        return "10.90.0.5"

    def attach_client_policy(self, role_arn: str, policy_arn: str) -> None:
        self.calls.append("attach_client_policy")
        self.attached.add(policy_arn)

    def detach_client_policy(self, policy_arn: str) -> None:
        self.calls.append("detach_client_policy")
        self.attached.discard(policy_arn)

    def isolate_mount_targets(self, security_group_id: str) -> Any:
        self.calls.append("isolate_mount_targets")
        self.isolated = True
        return ["rule"]

    def restore_mount_targets(self, security_group_id: str, token: Any) -> None:
        self.calls.append("restore_mount_targets")
        assert token == ["rule"]
        self.isolated = False


def test_state_never_lands_in_the_repo(tmp_path) -> None:
    assert measure.state_dir() == tmp_path / "rayito-measure"
    assert not str(measure.state_dir()).startswith(str(REPO_ROOT))


def test_resource_tags_carry_the_fixed_measurement_key() -> None:
    tags = measure.resource_tags("abc123", 1_000)
    assert tags[measure.MEASUREMENT_TAG_KEY] == measure.MEASUREMENT_TAG_VALUE
    assert tags[measure.RUN_ID_TAG_KEY] == "abc123"
    assert tags[measure.EXPIRES_AT_TAG_KEY] == "1000"


def test_plan_makes_no_aws_call_and_exits_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = measure.main(["plan"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "EFS-2" in out
    assert "budget cap: $3.00" in out
    assert "EXISTING VPC" in out


def test_run_checks_the_network_then_deploys_once_and_resolves_the_rest(
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake = FakeMeasurementPort()
    assert measure.main(RUN_INFRA + ["r1"], port=fake) == 0
    assert fake.calls.index("check_network") < fake.calls.index("deploy_stack")
    assert fake.calls.count("deploy_stack") == 1
    assert fake.deployed_into == (VPC_ID, tuple(SUBNET_IDS.split(",")))
    state = measure.load_state("r1")
    assert state is not None
    assert state.resources == ["file-system", "stack"]
    assert VPC_ID not in json.dumps(state.to_json())

    capsys.readouterr()
    fake.calls.clear()
    assert measure.main(RUN_INFRA + ["r1"], port=fake) == 0
    assert "deploy_stack" not in fake.calls
    assert "check_network" not in fake.calls


def test_run_never_prints_a_real_resource_id(
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake = FakeMeasurementPort()
    assert measure.main(RUN_INFRA + ["r1b"], port=fake) == 0
    out = capsys.readouterr().out
    assert "vpc-0" not in out
    assert "subnet-0" not in out
    assert "fs-0" not in out
    assert "arn:aws" not in out


def test_the_network_can_come_only_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(measure.VPC_ID_ENV, VPC_ID)
    monkeypatch.setenv(measure.SUBNET_IDS_ENV, SUBNET_IDS)
    fake = FakeMeasurementPort()
    args = ["run", "--region", "us-east-1", "--infra-only", "--run-id", "e1"]
    assert measure.main(args, port=fake) == 0
    assert fake.deployed_into == (VPC_ID, tuple(SUBNET_IDS.split(",")))


@pytest.mark.parametrize("half", [EXISTING[:2], EXISTING[2:], []])
def test_run_refuses_without_a_full_existing_network_before_any_aws_call(
    half: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(measure.VPC_ID_ENV, raising=False)
    monkeypatch.delenv(measure.SUBNET_IDS_ENV, raising=False)
    fake = FakeMeasurementPort()
    args = ["run", "--region", "us-east-1", "--infra-only", *half, "--run-id", "b2"]
    assert measure.main(args, port=fake) == 2
    assert fake.calls == []


def test_a_network_that_fails_the_check_stops_before_creating_anything() -> None:
    fake = FakeMeasurementPort(network_ok=False)
    assert measure.main(RUN_INFRA + ["n1"], port=fake) == 1
    assert "deploy_stack" not in fake.calls
    state = measure.load_state("n1")
    assert state is not None and state.resources == []


@pytest.mark.parametrize("run_id", ["Upper", "has.dot", "x" * 38, "_lead"])
def test_a_run_id_that_cannot_name_the_stack_is_refused(run_id: str) -> None:
    fake = FakeMeasurementPort()
    assert measure.main(RUN_INFRA + [run_id], port=fake) == 2
    assert fake.calls == []


def test_the_script_never_creates_or_changes_a_network() -> None:
    """No VPC/subnet/route/NACL operation anywhere in the port or the real
    adapter: the only EC2 writes are EFS-13's on the stack's own group."""
    source = MODULE_PATH.read_text(encoding="utf-8")
    for forbidden in (
        "create_vpc",
        "delete_vpc",
        "create_subnet",
        "delete_subnet",
        "create_route",
        "replace_route",
        "network_acl",
        "modify_vpc",
        "modify_subnet",
    ):
        assert forbidden not in source
    assert measure.CLEANUP_ORDER == ("client-policy", "stack", "file-system")


def test_report_without_a_run_fails_cleanly(capsys: pytest.CaptureFixture[str]) -> None:
    assert measure.main(["report", "--run-id", "no-such-run"]) == 1


def test_report_redacts_and_marks_stop_criteria(
    capsys: pytest.CaptureFixture[str],
) -> None:
    measure.main(RUN_INFRA + ["r2"], port=FakeMeasurementPort())
    state = measure.load_state("r2")
    assert state is not None
    state.results.append(
        measure.QuestionResult(
            question="EFS-2", answered=True, is_stop_criterion=True, summary="ok"
        )
    )
    measure.save_state(state)
    capsys.readouterr()  # discard the `run` command's own output

    assert measure.main(["report", "--run-id", "r2"]) == 0
    out = capsys.readouterr().out
    assert "[*] EFS-2: OK - ok" in out
    assert "fs-" not in out
    assert "fsap-" not in out


def test_cleanup_without_a_run_is_a_noop(capsys: pytest.CaptureFixture[str]) -> None:
    assert measure.main(["cleanup", "--run-id", "ghost"]) == 0
    out = capsys.readouterr().out
    assert "nothing to clean up" in out


def test_cleanup_deletes_in_reverse_dependency_order_and_removes_state() -> None:
    fake = FakeMeasurementPort()
    measure.main(RUN_INFRA + ["r3"], port=fake)
    fake.calls.clear()

    assert measure.main(["cleanup", "--run-id", "r3"], port=fake) == 0
    deletes = [call for call in fake.calls if call.startswith(("destroy_", "delete_"))]
    assert deletes == ["destroy_stack", "delete_file_system"]
    assert measure.load_state("r3") is None
    assert fake.stacks == {}
    assert fake.file_systems == {}


def test_cleanup_stops_and_keeps_the_rest_of_state_when_a_delete_fails(
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake = FakeMeasurementPort(fail_on="delete_file_system")
    measure.main(RUN_INFRA + ["r4"], port=fake)

    assert measure.main(["cleanup", "--run-id", "r4"], port=fake) == 1
    state = measure.load_state("r4")
    assert state is not None
    assert state.resources == ["file-system"]  # "stack" already deleted, kept out
    assert fake.stacks == {}

    fake.fail_on = None
    assert measure.main(["cleanup", "--run-id", "r4"], port=fake) == 0
    assert measure.load_state("r4") is None


def test_cleanup_tolerates_a_resource_already_gone() -> None:
    fake = FakeMeasurementPort()
    measure.main(RUN_INFRA + ["r5"], port=fake)
    # Someone deleted the file system out of band; `cleanup` must not treat
    # an already-gone resource as a failure.
    fake.file_systems.clear()

    assert measure.main(["cleanup", "--run-id", "r5"], port=fake) == 0
    assert measure.load_state("r5") is None


def test_state_round_trips_through_json(tmp_path) -> None:
    state = measure.RunState(
        run_id="r6",
        region="us-east-1",
        created_at_ms=1,
        expires_at_ms=2,
        resources=["stack"],
    )
    measure.save_state(state)
    raw = json.loads(measure.state_path("r6").read_text(encoding="utf-8"))
    restored = measure.RunState.from_json(raw)
    assert restored == state


def test_real_port_is_not_constructed_when_a_fake_is_injected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(**_kwargs: Any) -> None:
        raise AssertionError("real_port must not be called when a port is injected")

    monkeypatch.setattr(measure, "real_port", boom)
    fake = FakeMeasurementPort()
    assert measure.main(RUN_INFRA + ["r7"], port=fake) == 0
    assert measure.main(["cleanup", "--run-id", "r7"], port=fake) == 0


# ------------------------------------------------------------- ★ campaign


@dataclass
class FakeGuest:
    """In-memory `GuestPort`: every command succeeds unless a substring of
    it is in `failures` (mapped to the `CommandOutcome` to return instead);
    `pause_state` is what every `pause()` reports. Tracks live VMs so a
    test can assert that each step killed what it launched."""

    failures: dict[str, Any] = field(default_factory=dict)
    pause_state: str = "paused"
    live: set[str] = field(default_factory=set)
    launches: list[dict[str, Any]] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    files: dict[str, str] = field(default_factory=dict)

    def launch(
        self, *, template, lifetime_seconds, egress=None, execution_role_arn=None
    ) -> str:
        handle = f"vm-{len(self.launches) + 1}"
        self.launches.append(
            {
                "template": template,
                "lifetime": lifetime_seconds,
                "egress": egress,
                "role": execution_role_arn,
            }
        )
        self.live.add(handle)
        return handle

    def run_as_root(self, handle: str, command: str, *, timeout_seconds: float):
        assert handle in self.live
        self.commands.append(command)
        for needle, outcome in self.failures.items():
            if needle in command:
                return outcome
        if command.startswith("echo "):
            text, path = command[len("echo ") :].split(" > ")
            self.files[path.split(" ")[0]] = text
        if command.startswith("cat "):
            return measure.CommandOutcome(0, self.files.get(command[4:], ""), "", 1.0)
        if command.startswith("timeout") and "nfs4" in command:
            return measure.CommandOutcome(measure.TIMEOUT_EXIT_CODE, "", "", 15000.0)
        return measure.CommandOutcome(0, "1\n" if "pgrep" in command else "", "", 42.0)

    def pause(self, handle: str, *, deadline_seconds: float):
        return measure.PauseOutcome(self.pause_state, 3.0)

    def resume(self, handle: str) -> None:
        assert handle in self.live

    def kill(self, handle: str) -> None:
        self.live.discard(handle)


CAMPAIGN = [
    "run",
    "--region",
    "us-east-1",
    *EXISTING,
    "--caps-template",
    "caps-throwaway",
    "--execution-role-arn",
    "arn:aws:iam::123456789012:role/rayito-exec",
    "--efs11-pauses",
    "60",
    "--run-id",
]


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(measure.time, "sleep", lambda _seconds: None)


def test_run_without_a_caps_template_refuses_before_any_aws_call() -> None:
    fake = FakeMeasurementPort()
    assert (
        measure.main(["run", "--region", "us-east-1", *EXISTING, "--run-id", "c0"], port=fake)
        == 2
    )
    assert fake.calls == []


def test_campaign_measures_every_automated_star_step_in_order_and_kills_its_vms(
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake, guest = FakeMeasurementPort(), FakeGuest()
    assert measure.main(CAMPAIGN + ["c1"], port=fake, guest=guest) == 0
    state = measure.load_state("c1")
    assert state is not None
    assert [r.question for r in state.results] == [
        "EFS-2",
        "EFS-3",
        "EFS-8",
        "EFS-11",
        "EFS-13",
    ]
    assert all(r.answered and r.is_stop_criterion for r in state.results)
    assert guest.live == set()
    assert not fake.isolated
    assert "attach_client_policy" in fake.calls
    assert sum("mount -t efs" in c for c in guest.commands) == (
        measure.EFS8_MOUNT_SAMPLES + 2  # EFS-8's samples, EFS-11's and EFS-13's mount
    )
    assert "client-policy" in state.resources  # still up for the manual steps


def test_efs2_default_image_rejecting_root_never_fails_the_star_step() -> None:
    """Without `RAYITO_ALLOW_ROOT=1` the default image rejects `user="root"`
    (`PERMISSION_DENIED`) before `mount` runs; that informational half of
    EFS-2 is recorded by type, the ★ verdict comes from the caps image."""

    class RootRejectingGuest(FakeGuest):
        def run_as_root(self, handle: str, command: str, *, timeout_seconds: float):
            if self.launches[-1]["template"] == "rayito-base":
                raise PermissionError("root rejected (fake)")
            return super().run_as_root(handle, command, timeout_seconds=timeout_seconds)

    fake, guest = FakeMeasurementPort(), RootRejectingGuest()
    args = CAMPAIGN + ["d1", "--default-template", "rayito-base"]
    assert measure.main(args, port=fake, guest=guest) == 0
    state = measure.load_state("d1")
    assert state is not None
    efs2 = state.result_for("EFS-2")
    assert efs2 is not None and efs2.answered
    assert "default image: tmpfs not measured (PermissionError)" in efs2.summary
    assert guest.live == set()


def test_a_star_failure_stops_the_run_and_cleans_up(
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake = FakeMeasurementPort()
    guest = FakeGuest(
        failures={
            "/dev/tcp/": measure.CommandOutcome(
                measure.TIMEOUT_EXIT_CODE, "", "", 5000.0
            )
        }
    )
    assert measure.main(CAMPAIGN + ["c2"], port=fake, guest=guest) == 1
    state = measure.load_state("c2")
    assert state is not None  # results survive cleanup, for report
    assert state.stopped
    assert [r.question for r in state.results] == ["EFS-2", "EFS-3"]
    assert state.resources == []
    assert fake.stacks == {} and fake.file_systems == {} and fake.attached == set()
    assert guest.live == set()
    assert not any("mount -t efs" in c for c in guest.commands)

    # A stopped run never resumes into more spending.
    assert measure.main(CAMPAIGN + ["c2"], port=fake, guest=guest) == 1
    assert fake.stacks == {}


def test_efs13_restores_the_mount_target_ingress_even_when_pause_hangs() -> None:
    fake, guest = FakeMeasurementPort(), FakeGuest()
    measure.main(CAMPAIGN + ["c3"], port=fake, guest=guest)
    guest.pause_state = "timeout"
    state = measure.load_state("c3")
    assert state is not None
    ctx = measure.StepContext(
        aws=fake,
        guest=guest,
        infra=measure.prepare_infra(
            state,
            fake,
            fake.stacks[measure.measurement_stack_name("c3")],
            None,
            measure.CampaignOptions("caps", "arn:aws:iam::123456789012:role/x"),
        ),
        options=measure.CampaignOptions("caps", "arn:aws:iam::123456789012:role/x"),
    )
    outcome = measure.measure_efs13(ctx)
    assert not outcome.answered
    assert "timeout" in outcome.summary
    assert fake.calls[-2:] == ["isolate_mount_targets", "restore_mount_targets"]
    assert not fake.isolated
    assert guest.live == set()


def test_a_step_that_raises_is_a_failed_step_recorded_by_type_only() -> None:
    def boom(_ctx: Any) -> Any:
        raise RuntimeError("fs-0123456789abcdef0 at 10.90.0.5")

    outcome = measure.run_step("EFS-8", boom, ctx=None)  # type: ignore[arg-type]
    assert not outcome.answered
    assert outcome.summary == "raised RuntimeError"


def test_summaries_and_report_never_carry_an_id(
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake = FakeMeasurementPort()
    guest = FakeGuest(
        failures={
            "mount -t efs": measure.CommandOutcome(
                32,
                "",
                "mount fs-0123abcd to 10.90.0.5 via fsap-0123456789abcdef0 failed",
                9.0,
            )
        }
    )
    assert measure.main(CAMPAIGN + ["c4"], port=fake, guest=guest) == 1
    out = capsys.readouterr().out
    measure.main(["report", "--run-id", "c4"])
    out += capsys.readouterr().out
    for leaked in ("fs-0", "fsap-", "10.90.0.5", "123456789012", "arn:aws"):
        assert leaked not in out
    assert "EFS-8: FAILED" in out
    assert "<redacted>" in out


def test_resume_skips_already_answered_steps() -> None:
    fake, guest = FakeMeasurementPort(), FakeGuest()
    measure.main(CAMPAIGN + ["c5"], port=fake, guest=guest)
    launches = len(guest.launches)
    assert measure.main(CAMPAIGN + ["c5"], port=fake, guest=guest) == 0
    assert len(guest.launches) == launches


def test_cleanup_detaches_the_client_policy_before_the_stack_and_fs_after() -> None:
    fake, guest = FakeMeasurementPort(), FakeGuest()
    measure.main(CAMPAIGN + ["c6"], port=fake, guest=guest)
    fake.calls.clear()
    assert measure.main(["cleanup", "--run-id", "c6"], port=fake) == 0
    order = [c for c in fake.calls if c.startswith(("detach_", "destroy_", "delete_"))]
    assert order == ["detach_client_policy", "destroy_stack", "delete_file_system"]


def test_percentile_is_nearest_rank() -> None:
    samples = [float(n) for n in range(1, 21)]
    assert measure.percentile(samples, 0.5) == 10.0
    assert measure.percentile(samples, 0.95) == 19.0
