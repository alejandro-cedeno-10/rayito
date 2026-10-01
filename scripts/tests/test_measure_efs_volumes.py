"""Pure, offline tests for `scripts/measure/efs_volumes.py` (`m15-efs-volumes`,
ADR-018): the tagging scheme, idempotent state file, and the `plan`/`report`/
`cleanup` subcommands. Never touches AWS; `run`'s own AWS calls are added and
exercised only by the serialized acceptance stage.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

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


def test_state_never_lands_in_the_repo(tmp_path) -> None:
    assert measure.state_dir() == tmp_path / "rayito-measure"
    assert not str(measure.state_dir()).startswith(str(REPO_ROOT))


def test_resource_tags_carry_the_fixed_measurement_key() -> None:
    tags = measure.resource_tags("abc123", 1_000)
    assert tags[measure.MEASUREMENT_TAG_KEY] == measure.MEASUREMENT_TAG_VALUE
    assert tags[measure.RUN_ID_TAG_KEY] == "abc123"
    assert tags[measure.EXPIRES_AT_TAG_KEY] == "1000"


def test_plan_makes_no_aws_call_and_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = measure.main(["plan"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "EFS-2" in out
    assert "cap: $1.50" in out


def test_run_is_idempotent_by_run_id(capsys: pytest.CaptureFixture[str]) -> None:
    assert measure.main(["run", "--region", "us-east-1", "--run-id", "r1"]) == 0
    first_state = measure.load_state("r1")
    assert first_state is not None
    created_at = first_state.created_at_ms

    assert measure.main(["run", "--region", "us-east-1", "--run-id", "r1"]) == 0
    out = capsys.readouterr().out
    assert "already exists" in out
    second_state = measure.load_state("r1")
    assert second_state is not None
    assert second_state.created_at_ms == created_at


def test_report_without_a_run_fails_cleanly(capsys: pytest.CaptureFixture[str]) -> None:
    assert measure.main(["report", "--run-id", "no-such-run"]) == 1


def test_report_redacts_and_marks_stop_criteria(capsys: pytest.CaptureFixture[str]) -> None:
    measure.main(["run", "--region", "us-east-1", "--run-id", "r2"])
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


def test_cleanup_clears_the_resource_list() -> None:
    measure.main(["run", "--region", "us-east-1", "--run-id", "r3"])
    state = measure.load_state("r3")
    assert state is not None
    state.resources.append("vpc-fake")
    measure.save_state(state)

    measure.main(["cleanup", "--run-id", "r3"])
    cleaned = measure.load_state("r3")
    assert cleaned is not None
    assert cleaned.resources == []


def test_state_round_trips_through_json(tmp_path) -> None:
    state = measure.RunState(
        run_id="r4", region="us-east-1", created_at_ms=1, expires_at_ms=2, resources=["a"]
    )
    measure.save_state(state)
    raw = json.loads(measure.state_path("r4").read_text(encoding="utf-8"))
    restored = measure.RunState.from_json(raw)
    assert restored == state
