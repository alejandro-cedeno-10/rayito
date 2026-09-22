"""Aceptación de M7 `m7-cli`: la CLI `rayito` en proceso (`CliRunner`, misma
sesión de AWS que el resto del e2e) contra la cuenta real, en cinco bloques:
`image list` (imágenes y versiones), `sandbox list|info|logs` sobre la
fixture `sandbox` (los logs sólo con `RAYITO_EXECUTION_ROLE_ARN`),
`doctor --launch` y `doctor` contra la fixture, `image publish` por la vía
del reuse (sin build, sólo si `image/rayito-image.zip` es el artefacto de
una versión ACTIVE y hay `RAYITO_BUCKET`) y `sandbox kill` de la fixture.
Mismos guardrails que todo e2e (`conftest.py`): la fixture nace con 900 s y
el doctor mata su sandbox de 300 s en un `finally`.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from rayito import Sandbox
from rayito._aws import LambdaMicrovmsControlPlane
from rayito._limits import TERMINAL_STATES
from rayito.cli import _checks
from rayito.cli._session import resolve_session
from rayito.cli.app import app
from rayito.exceptions import SandboxNotFoundException

from .conftest import EXECUTION_ROLE_VAR, E2ESettings

pytestmark = pytest.mark.e2e

BUCKET_VAR = "RAYITO_BUCKET"
REPO_ROOT = Path(__file__).resolve().parents[4]
ARTIFACT = REPO_ROOT / "image" / "rayito-image.zip"
TERMINATE_VISIBLE_TIMEOUT_SECONDS = 30.0
LOGS_WAIT_SECONDS = 60.0
POLL_INTERVAL_SECONDS = 2.0
STREAM_NAME_PATTERN = r"^\d{4}/\d{2}/\d{2}\[[^\]]+\]microvm-"


def invoke(*args: str) -> tuple[int, str, str]:
    result = CliRunner().invoke(app, list(args))
    return result.exit_code, result.stdout, result.stderr


def invoke_json(*args: str) -> tuple[int, Any, str]:
    code, stdout, stderr = invoke("--json", *args)
    document = json.loads(stdout) if stdout.strip() else None
    return code, document, stderr


def bucket() -> str | None:
    return os.environ.get(BUCKET_VAR) or None


def statuses(document: dict[str, Any]) -> dict[str, str]:
    return {check["name"]: check["status"] for check in document["checks"]}


def check(document: dict[str, Any], name: str) -> dict[str, Any]:
    return next(item for item in document["checks"] if item["name"] == name)


def wait_for_terminal_state(sandbox_id: str, control_plane: LambdaMicrovmsControlPlane) -> str:
    started = time.perf_counter()
    while True:
        try:
            state = control_plane.get_microvm(sandbox_id).state
        except SandboxNotFoundException:
            return "TERMINATED"
        if state in TERMINAL_STATES:
            return state
        if time.perf_counter() - started > TERMINATE_VISIBLE_TIMEOUT_SECONDS:
            pytest.fail(f"{sandbox_id} sigue {state} tras {TERMINATE_VISIBLE_TIMEOUT_SECONDS} s")
        time.sleep(POLL_INTERVAL_SECONDS)


def test_image_list_shows_the_template_and_its_versions(template_arn: str) -> None:
    started = time.perf_counter()
    code, images, stderr = invoke_json("image", "list")
    assert code == 0, stderr
    ours = next(image for image in images if image["imageArn"] == template_arn)
    assert ours["latestActiveImageVersion"]
    code, versions, stderr = invoke_json("image", "list", template_arn)
    assert code == 0, stderr
    active = next(v for v in versions if v["imageVersion"] == ours["latestActiveImageVersion"])
    assert (active["state"], active["status"]) == ("SUCCESSFUL", "ACTIVE")
    assert [v["createdAt"] for v in versions] == sorted(
        (v["createdAt"] for v in versions), reverse=True
    )
    print(
        f"\nimage list: {len(images)} imágenes, {len(versions)} versiones de "
        f"{ours['name']}, activa {ours['latestActiveImageVersion']} "
        f"({time.perf_counter() - started:.1f} s)",
        flush=True,
    )


def test_sandbox_list_info_and_logs(
    sandbox: Sandbox, e2e_settings: E2ESettings, template_arn: str
) -> None:
    code, listed, stderr = invoke_json("sandbox", "list", "--template", template_arn)
    assert code == 0, stderr
    ours = next(item for item in listed if item["sandbox_id"] == sandbox.sandbox_id)
    assert ours["state"] == "RUNNING" and ours["template_arn"] == template_arn
    code, info, stderr = invoke_json("sandbox", "info", sandbox.sandbox_id)
    assert code == 0, stderr
    assert info["state"] == "RUNNING" and info["template"] == template_arn
    assert info["metadata"] == sandbox.metadata
    if not e2e_settings.execution_role_arn:
        code, _stdout, stderr = invoke("sandbox", "logs", sandbox.sandbox_id)
        assert code == 1 and "sin logs" in stderr
        print(f"\nsandbox logs: sin {EXECUTION_ROLE_VAR}, `sin logs` y salida 1 (Q55 no observado)")
        return
    deadline = time.perf_counter() + LOGS_WAIT_SECONDS
    events: list[dict[str, Any]] = []
    while time.perf_counter() < deadline:
        code, events, stderr = invoke_json("sandbox", "logs", sandbox.sandbox_id, "--limit", "20")
        if code == 0 and events:
            break
        time.sleep(POLL_INTERVAL_SECONDS)
    assert code == 0 and events, stderr
    assert re.match(STREAM_NAME_PATTERN, events[0]["stream"]), events[0]["stream"]
    print(f"\nsandbox logs: stream {events[0]['stream']}, {len(events)} eventos (Q55)")


def test_doctor_with_launch_and_against_the_fixture(
    sandbox: Sandbox, control_plane: LambdaMicrovmsControlPlane, template_arn: str
) -> None:
    artifact_bucket = bucket()
    bucket_args = ["--bucket", artifact_bucket] if artifact_bucket else []
    started = time.perf_counter()
    code, launched_doc, stderr = invoke_json(
        "doctor", "--template", template_arn, *bucket_args, "--launch"
    )
    launch_seconds = time.perf_counter() - started
    assert launched_doc is not None, stderr
    print("\ndoctor --launch:", json.dumps(statuses(launched_doc)), f"{launch_seconds:.1f} s")
    for item in launched_doc["checks"]:
        print(f"  {item['status']:<4} {item['name']:<15} {item['summary']}")
    assert code == 0, launched_doc
    assert [item["name"] for item in launched_doc["checks"]] == list(_checks.CHECK_NAMES)
    assert "FAIL" not in statuses(launched_doc).values()
    assert check(launched_doc, "agent")["details"]["agent_version"]
    assert check(launched_doc, "compatibility")["status"] == "OK"
    launched_id = launched_doc["launched_sandbox_id"]
    assert launched_id
    terminal = wait_for_terminal_state(launched_id, control_plane)
    print(f"  sandbox de --launch {launched_id}: {terminal}")
    started = time.perf_counter()
    code, doc, stderr = invoke_json("doctor", "--template", template_arn, *bucket_args)
    assert doc is not None, stderr
    print("doctor:", json.dumps(statuses(doc)), f"{time.perf_counter() - started:.1f} s")
    assert code == 0, doc
    assert statuses(doc)["token"] == "OK" and statuses(doc)["agent"] == "OK"
    assert statuses(doc)["compatibility"] == "OK"
    assert check(doc, "token")["details"]["sandbox_id"] == sandbox.sandbox_id
    assert check(doc, "token")["details"]["source"] == "create-microvm-auth-token"
    assert doc["launched_sandbox_id"] is None
    print(
        f"  agent_version={check(doc, 'agent')['details']['agent_version']} "
        f"rayd sobre {template_arn} {check(doc, 'compatibility')['details']['image_version']}"
    )


def test_iam_simulation_sees_the_organizations_decision() -> None:
    """Hecho a medir (design.md): `s3:CreateBucket` sobre el bucket es la sonda
    de la SCP que lo deniega; la acción no forma parte de la tabla de la CLI."""
    clients = resolve_session(None, None)
    identity = clients.sts.get_caller_identity()
    source_arn = _checks.policy_source_arn(clients, str(identity["Arn"]))
    if source_arn is None:
        pytest.skip(f"principal {identity['Arn']} no simulable")
    target = bucket() or "rayito-doctor-probe"
    results = _checks.simulate(clients, source_arn, ["s3:CreateBucket"], [f"arn:aws:s3:::{target}"])
    print("\nsimulación s3:CreateBucket:", json.dumps(results, default=str))
    assert results and results[0]["action"] == "s3:CreateBucket"


def artifact_key_basename(path: Path) -> str:
    return f"rayd-{hashlib.sha256(path.read_bytes()).hexdigest()[:12]}.zip"


def test_image_publish_reuses_the_current_version(template_arn: str) -> None:
    if not ARTIFACT.is_file() or not bucket():
        pytest.skip(f"sin {ARTIFACT} o sin {BUCKET_VAR}")
    code, versions, stderr = invoke_json("image", "list", template_arn)
    assert code == 0, stderr
    basename = artifact_key_basename(ARTIFACT)
    matching = [v for v in versions if v["artifact"] == basename and v["status"] == "ACTIVE"]
    if not matching:
        pytest.skip(f"{ARTIFACT.name} ({basename}) no es el artefacto de ninguna versión ACTIVE")
    started = time.perf_counter()
    code, stdout, stderr = invoke(
        "image",
        "publish",
        "--artifact",
        str(ARTIFACT),
        "--base-image-version",
        "1",
        "--bucket",
        str(bucket()),
        "--image-name",
        template_arn.rsplit(":", 1)[-1],
    )
    print(f"\nimage publish (reuse) {time.perf_counter() - started:.1f} s:\n{stdout}")
    assert code == 0, stderr
    assert "already built from this artifact and config; reusing" in stdout
    assert stdout.rstrip().endswith(f"RAYITO_TEMPLATE={template_arn}")
    code, after, stderr = invoke_json("image", "list", template_arn)
    assert code == 0 and len(after) == len(versions)


def test_sandbox_kill_terminates_the_fixture(
    sandbox: Sandbox, control_plane: LambdaMicrovmsControlPlane, template_arn: str
) -> None:
    code, stdout, stderr = invoke("sandbox", "kill", sandbox.sandbox_id)
    assert code == 0, stderr
    assert f"{sandbox.sandbox_id} terminated" in stdout
    terminal = wait_for_terminal_state(sandbox.sandbox_id, control_plane)
    code, listed, stderr = invoke_json("sandbox", "list", "--template", template_arn)
    assert code == 0, stderr
    running = [item["sandbox_id"] for item in listed if item["state"] == "RUNNING"]
    print(f"\nsandbox kill: {sandbox.sandbox_id} {terminal}; RUNNING de la imagen: {running}")
    assert sandbox.sandbox_id not in running
