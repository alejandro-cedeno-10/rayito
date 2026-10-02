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

    vpcs: dict[str, str] = field(default_factory=dict)
    subnets: dict[str, str] = field(default_factory=dict)
    stacks: dict[str, dict[str, str]] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    fail_on: str | None = None
    _next_id: int = 1

    def _new_id(self, prefix: str) -> str:
        value = f"{prefix}-{self._next_id:08x}"
        self._next_id += 1
        return value

    def find_tagged_vpc(self, run_id: str) -> str | None:
        self.calls.append("find_tagged_vpc")
        return self.vpcs.get(run_id)

    def create_vpc(self, *, tags: dict[str, str]) -> str:
        self.calls.append("create_vpc")
        vpc_id = self._new_id("vpc")
        self.vpcs[tags["rayito:run-id"]] = vpc_id
        return vpc_id

    def delete_vpc(self, vpc_id: str) -> None:
        if self.fail_on == "delete_vpc":
            raise RuntimeError("DependencyViolation (fake)")
        self.calls.append("delete_vpc")
        self.vpcs = {run_id: v for run_id, v in self.vpcs.items() if v != vpc_id}

    def find_tagged_subnet(self, run_id: str) -> str | None:
        self.calls.append("find_tagged_subnet")
        return self.subnets.get(run_id)

    def create_subnet(self, vpc_id: str, *, tags: dict[str, str]) -> str:
        self.calls.append("create_subnet")
        subnet_id = self._new_id("subnet")
        self.subnets[tags["rayito:run-id"]] = subnet_id
        return subnet_id

    def delete_subnet(self, subnet_id: str) -> None:
        if self.fail_on == "delete_subnet":
            raise RuntimeError("InvalidSubnetID.NotFound-ish (fake)")
        self.calls.append("delete_subnet")
        self.subnets = {
            run_id: s for run_id, s in self.subnets.items() if s != subnet_id
        }

    def stack_status(self, stack_name: str) -> dict[str, str] | None:
        self.calls.append("stack_status")
        return self.stacks.get(stack_name)

    def deploy_stack(
        self, stack_name: str, *, vpc_id: str, subnet_id: str, tags: dict[str, str]
    ) -> dict[str, str]:
        self.calls.append("deploy_stack")
        outputs = {
            "FileSystemId": self._new_id("fs"),
            "ConnectorArn": "arn:aws:lambda:fake",
        }
        self.stacks[stack_name] = outputs
        return outputs

    def destroy_stack(self, stack_name: str) -> None:
        if self.fail_on == "destroy_stack":
            raise RuntimeError("StackError (fake)")
        self.calls.append("destroy_stack")
        self.stacks.pop(stack_name, None)


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
    assert "cap: $1.50" in out


def test_run_creates_each_resource_once_and_resolves_the_rest_by_tag(
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake = FakeMeasurementPort()
    assert (
        measure.main(["run", "--region", "us-east-1", "--run-id", "r1"], port=fake) == 0
    )
    assert fake.calls.count("create_vpc") == 1
    assert fake.calls.count("create_subnet") == 1
    assert fake.calls.count("deploy_stack") == 1
    state = measure.load_state("r1")
    assert state is not None
    assert state.resources == ["vpc", "subnet", "stack"]

    capsys.readouterr()
    fake.calls.clear()
    assert (
        measure.main(["run", "--region", "us-east-1", "--run-id", "r1"], port=fake) == 0
    )
    assert "create_vpc" not in fake.calls
    assert "create_subnet" not in fake.calls
    assert "deploy_stack" not in fake.calls
    assert fake.calls.count("find_tagged_vpc") == 1
    assert fake.calls.count("find_tagged_subnet") == 1
    assert fake.calls.count("stack_status") == 1


def test_run_never_prints_a_real_resource_id(
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake = FakeMeasurementPort()
    assert (
        measure.main(["run", "--region", "us-east-1", "--run-id", "r1b"], port=fake)
        == 0
    )
    out = capsys.readouterr().out
    assert "vpc-0" not in out
    assert "subnet-0" not in out
    assert "fs-0" not in out
    assert "arn:aws" not in out


def test_report_without_a_run_fails_cleanly(capsys: pytest.CaptureFixture[str]) -> None:
    assert measure.main(["report", "--run-id", "no-such-run"]) == 1


def test_report_redacts_and_marks_stop_criteria(
    capsys: pytest.CaptureFixture[str],
) -> None:
    measure.main(
        ["run", "--region", "us-east-1", "--run-id", "r2"], port=FakeMeasurementPort()
    )
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
    measure.main(["run", "--region", "us-east-1", "--run-id", "r3"], port=fake)
    fake.calls.clear()

    assert measure.main(["cleanup", "--run-id", "r3"], port=fake) == 0
    deletes = [call for call in fake.calls if call.startswith(("destroy_", "delete_"))]
    assert deletes == ["destroy_stack", "delete_subnet", "delete_vpc"]
    assert measure.load_state("r3") is None
    assert fake.stacks == {}
    assert fake.vpcs == {}
    assert fake.subnets == {}


def test_cleanup_stops_and_keeps_the_rest_of_state_when_a_delete_fails(
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake = FakeMeasurementPort(fail_on="delete_subnet")
    measure.main(["run", "--region", "us-east-1", "--run-id", "r4"], port=fake)

    assert measure.main(["cleanup", "--run-id", "r4"], port=fake) == 1
    state = measure.load_state("r4")
    assert state is not None
    assert state.resources == ["vpc", "subnet"]  # "stack" already deleted, kept out
    assert fake.stacks == {}

    fake.fail_on = None
    assert measure.main(["cleanup", "--run-id", "r4"], port=fake) == 0
    assert measure.load_state("r4") is None


def test_cleanup_tolerates_a_resource_already_gone() -> None:
    fake = FakeMeasurementPort()
    measure.main(["run", "--region", "us-east-1", "--run-id", "r5"], port=fake)
    # Someone deleted the subnet out of band; `cleanup` must not treat an
    # already-gone resource as a failure.
    fake.subnets.clear()

    assert measure.main(["cleanup", "--run-id", "r5"], port=fake) == 0
    assert measure.load_state("r5") is None


def test_state_round_trips_through_json(tmp_path) -> None:
    state = measure.RunState(
        run_id="r6",
        region="us-east-1",
        created_at_ms=1,
        expires_at_ms=2,
        resources=["vpc"],
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
    assert (
        measure.main(["run", "--region", "us-east-1", "--run-id", "r7"], port=fake) == 0
    )
    assert measure.main(["cleanup", "--run-id", "r7"], port=fake) == 0
