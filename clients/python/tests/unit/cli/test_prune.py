"""`rayito.cli._prune` con `Stubber`: el keep set (N más nuevas, versión con
MicroVM vivo, build en vuelo), `ConflictException` → espera → reintento, un
dry run sin llamadas mutantes, una versión ACTIVE rechazada se desactiva
primero, la forma del resumen y la salida 1, los defaults y límites y que
`rayito image prune` y el shim comparten la salida."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from typer.testing import CliRunner

from rayito.cli import _prune
from rayito.cli._session import Clients
from rayito.cli.app import app

from .conftest import IMAGE_ARN, Stubs

ARN = IMAGE_ARN
T0 = datetime(2026, 9, 1, tzinfo=UTC)


def version_item(number: int, state: str = "SUCCESSFUL", status: str = "ACTIVE") -> dict[str, Any]:
    return {
        "imageVersion": f"{number}.0",
        "state": state,
        "status": status,
        "createdAt": T0 + timedelta(hours=number),
        "imageArn": ARN,
        "baseImageArn": "arn:aws:lambda:us-east-1:aws:microvm-image:al2023-1",
        "buildRoleArn": "arn:aws:iam::123456789012:role/build",
        "codeArtifact": {"uri": "s3://b/k.zip"},
    }


def version(number: int, state: str = "SUCCESSFUL", status: str = "ACTIVE") -> _prune.Version:
    return _prune.Version(
        image_version=f"{number}.0",
        state=state,
        status=status,
        created_at=T0 + timedelta(hours=number),
    )


def microvm_item(number: int, state: str) -> dict[str, Any]:
    return {
        "microvmId": f"microvm-{number}",
        "state": state,
        "imageArn": ARN,
        "imageVersion": f"{number}.0",
        "startedAt": T0,
    }


def stub_listing(
    stubs: Stubs, versions: list[dict[str, Any]], microvms: list[dict[str, Any]]
) -> None:
    stubs.microvms.add_response(
        "list_microvm_image_versions", {"items": versions}, {"imageIdentifier": ARN}
    )
    stubs.microvms.add_response("list_microvms", {"items": microvms}, {"imageIdentifier": ARN})


def image_response(state: str) -> dict[str, Any]:
    return {"imageArn": ARN, "name": "rayito-base", "state": state, "createdAt": T0}


def test_keep_set_keeps_newest_three_and_the_live_version() -> None:
    versions = [version(n) for n in range(1, 9)]
    versions.sort(key=lambda v: v.created_at, reverse=True)
    plan = _prune.build_plan(versions, live={"2.0"}, keep=3)
    assert [v.image_version for v, _ in plan.keep] == ["8.0", "7.0", "6.0", "2.0"]
    assert [why for _, why in plan.keep] == ["newest", "newest", "newest", "live microvm"]
    assert [v.image_version for v in plan.delete] == ["1.0", "3.0", "4.0", "5.0"]


def test_in_flight_builds_are_neither_counted_nor_deleted() -> None:
    versions = [
        version(5, state="IN_PROGRESS", status="INACTIVE"),
        version(4),
        version(3, status="INACTIVE"),
        version(2, state="FAILED", status="INACTIVE"),
        version(1),
    ]
    plan = _prune.build_plan(versions, live=set(), keep=2)
    kept = {v.image_version: why for v, why in plan.keep}
    assert kept == {"5.0": "in_progress", "4.0": "newest", "1.0": "newest"}
    assert [v.image_version for v in plan.delete] == ["2.0", "3.0"]


def test_dry_run_makes_no_mutating_call(
    clients: Clients, stubbed_clients: Stubs, capsys: pytest.CaptureFixture[str]
) -> None:
    stub_listing(
        stubbed_clients,
        [version_item(n) for n in range(1, 9)],
        [microvm_item(2, "RUNNING"), microvm_item(1, "TERMINATED")],
    )
    code = _prune.run(clients, _prune.PruneSettings(keep=3, dry_run=True, wait_timeout=1.0))
    assert code == 0
    text = capsys.readouterr().out
    assert "delete" in text and "keep (live microvm)" in text
    assert '"dryRun": true' in text
    assert '"planned"' in text


def test_conflict_waits_for_the_image_then_retries(stubbed_clients: Stubs) -> None:
    stub = stubbed_clients.microvms
    client = stubbed_clients.clients["microvms"]
    stub.add_client_error(
        "delete_microvm_image_version",
        service_error_code="ConflictException",
        service_message="MicroVM Image is already in state: UPDATING",
        expected_params={"imageIdentifier": ARN, "imageVersion": "1.0"},
    )
    for state in ("UPDATING", "UPDATING", "UPDATED"):
        stub.add_response("get_microvm_image", image_response(state), {"imageIdentifier": ARN})
    stub.add_response(
        "delete_microvm_image_version",
        {"imageIdentifier": ARN, "imageVersion": "1.0", "state": "DELETING"},
        {"imageIdentifier": ARN, "imageVersion": "1.0"},
    )
    stub.add_response(
        "get_microvm_image_version",
        {**version_item(1, state="DELETING")},
        {"imageIdentifier": ARN, "imageVersion": "1.0"},
    )
    stub.add_client_error(
        "get_microvm_image_version",
        service_error_code="ResourceNotFoundException",
        expected_params={"imageIdentifier": ARN, "imageVersion": "1.0"},
    )
    stub.add_response("get_microvm_image", image_response("DELETING"), {"imageIdentifier": ARN})
    stub.add_response("get_microvm_image", image_response("UPDATED"), {"imageIdentifier": ARN})
    slept: list[float] = []
    ticks = iter(range(10_000))
    lines: list[str] = []
    pruner = _prune.Pruner(
        client,
        ARN,
        wait_timeout=600.0,
        sleep=slept.append,
        clock=lambda: float(next(ticks)),
        emit=lines.append,
    )
    report = pruner.prune(version(1))
    assert report.outcome == "deleted"
    assert report.attempts == 2
    assert report.conflicts == 1
    assert not report.deactivated
    assert slept == [5.0, 5.0, 5.0, 5.0, 5.0]
    assert report.as_dict()["attempts"] == 2
    assert lines == ["  1.0: ConflictException on attempt 1; retrying in 5s"]


def test_a_refused_active_version_is_deactivated_first(stubbed_clients: Stubs) -> None:
    stub = stubbed_clients.microvms
    stub.add_client_error(
        "delete_microvm_image_version",
        service_error_code="ValidationException",
        service_message="version is ACTIVE",
        expected_params={"imageIdentifier": ARN, "imageVersion": "3.0"},
    )
    stub.add_response(
        "update_microvm_image_version",
        {**version_item(3, status="INACTIVE")},
        {"imageIdentifier": ARN, "imageVersion": "3.0", "status": "INACTIVE"},
    )
    stub.add_response("get_microvm_image", image_response("UPDATED"), {"imageIdentifier": ARN})
    stub.add_response(
        "delete_microvm_image_version",
        {"imageIdentifier": ARN, "imageVersion": "3.0", "state": "DELETING"},
        {"imageIdentifier": ARN, "imageVersion": "3.0"},
    )
    stub.add_response(
        "get_microvm_image_version",
        {**version_item(3, state="DELETED", status="INACTIVE")},
        {"imageIdentifier": ARN, "imageVersion": "3.0"},
    )
    stub.add_response("get_microvm_image", image_response("UPDATED"), {"imageIdentifier": ARN})
    pruner = _prune.Pruner(
        stubbed_clients.clients["microvms"], ARN, wait_timeout=600.0, sleep=lambda _: None
    )
    report = pruner.prune(version(3))
    assert report.outcome == "deleted"
    assert report.deactivated
    assert report.attempts == 2


def test_summary_shape_and_exit_code_on_failure(
    clients: Clients, stubbed_clients: Stubs, capsys: pytest.CaptureFixture[str]
) -> None:
    stub_listing(stubbed_clients, [version_item(2), version_item(1)], [])
    stubbed_clients.microvms.add_response("list_microvms", {"items": []}, {"imageIdentifier": ARN})
    stubbed_clients.microvms.add_client_error(
        "delete_microvm_image_version",
        service_error_code="AccessDeniedException",
        service_message="nope",
        expected_params={"imageIdentifier": ARN, "imageVersion": "1.0"},
    )
    code = _prune.run(clients, _prune.PruneSettings(keep=1, wait_timeout=1.0), sleep=lambda _: None)
    assert code == 1
    text = capsys.readouterr().out
    assert '"deleted": []' in text
    assert '"outcome": "refused"' in text
    assert '"kept": [\n    "2.0"\n  ]' in text


def test_settings_defaults_and_bounds() -> None:
    parsed = _prune.PruneSettings()
    assert (parsed.image_name, parsed.keep, parsed.dry_run, parsed.wait_timeout) == (
        "rayito-base",
        5,
        False,
        600.0,
    )
    with pytest.raises(ValueError, match="--keep"):
        _prune.PruneSettings(keep=0)


def test_cli_prune_dry_run_matches_the_library(
    runner: CliRunner, clients: Clients, stubbed_clients: Stubs
) -> None:
    stub_listing(
        stubbed_clients,
        [version_item(n) for n in range(1, 9)],
        [microvm_item(2, "RUNNING")],
    )
    result = runner.invoke(
        app, ["--json", "image", "prune", "--keep", "3", "--dry-run"], obj=clients
    )
    assert result.exit_code == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary["kept"] == ["8.0", "7.0", "6.0", "2.0"]
    assert summary["planned"] == ["1.0", "3.0", "4.0", "5.0"]
    assert summary["dryRun"] is True
    assert "keep (live microvm)" in result.stderr


def test_cli_prune_rejects_keep_below_one(runner: CliRunner, clients: Clients) -> None:
    result = runner.invoke(app, ["image", "prune", "--keep", "0"], obj=clients)
    assert result.exit_code == 2
    assert "--keep" in result.stderr
