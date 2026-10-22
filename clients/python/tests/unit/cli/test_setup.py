"""`rayito setup` por la CLI: los casos compartidos de
`testdata/account-setup/cli-cases.json` sobre dobles en memoria, el apply con
el doctor sustituido, la confirmación en una terminal y el contrato de
textos, opciones y documento JSON."""

from __future__ import annotations

import copy
import json
import types
import zipfile
from pathlib import Path
from typing import Any

import boto3
import pytest
import typer.main
from botocore.exceptions import ClientError
from typer.testing import CliRunner, Result

from rayito import __version__ as SDK_VERSION
from rayito.cli import _setup_output as output
from rayito.cli._artifact import (
    EFS_MARKER_CONTENT,
    EFS_MARKER_ENTRY,
    SLIM_MARKER_CONTENT,
    WARMUP_MARKER_ENTRY,
)
from rayito.cli._checks import CheckResult
from rayito.cli._session import Clients, UsageError
from rayito.cli.app import app
from rayito.exceptions import StackException

from ..fake_account_setup import (
    BUILD_ROLE_ARN,
    FakeAccount,
    fill_placeholders,
    from_vector_input,
    load_cases,
    load_contract,
)

CONTRACT = load_contract()
MESSAGES = CONTRACT["messages"]
CASES = load_cases("cli-cases.json")
VECTORS = {case["name"]: case["input"] for case in load_cases("plan-vectors.json")}
FRESH = VECTORS["cuenta nueva con el bucket del bootstrap de CDK"]
PREPARED = VECTORS["cuenta preparada a mano: todo se reutiliza"]
NO_BUCKET = VECTORS["sin ningún bucket: se crea uno con el nombre por defecto"]
WRITING_CALLS = {
    "create",
    "update",
    "wait",
    "create_artifact_stack",
    "wait_artifact_stack",
    "download",
    "publish",
    "publish:caps",
    "gate",
}


@pytest.fixture(autouse=True)
def no_bucket_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RAYITO_BUCKET", raising=False)


def clients_for(vector: dict[str, Any]) -> tuple[Clients, FakeAccount]:
    account = from_vector_input(copy.deepcopy(vector), SDK_VERSION)
    session = boto3.session.Session(
        region_name=vector["region"], aws_access_key_id="testing", aws_secret_access_key="testing"
    )
    clients = Clients(
        session=session,
        region=vector["region"],
        account_id_override=vector["account"],
        account_setup_override=account.gateways,
    )
    return clients, account


def run(
    vector: dict[str, Any], *args: str, input_text: str | None = None
) -> tuple[Result, FakeAccount]:
    clients, account = clients_for(vector)
    result = CliRunner().invoke(app, list(args), obj=clients, input=input_text)
    return result, account


def fill(value: Any, vector: dict[str, Any]) -> Any:
    return fill_placeholders(
        value, account=vector["account"], region=vector["region"], sdk_version=SDK_VERSION
    )


def doctor_returning(*statuses: str) -> Any:
    results = [CheckResult(f"check-{n}", status, "resumen") for n, status in enumerate(statuses)]  # type: ignore[arg-type]
    return lambda clients, context: results


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_shared_cli_cases(case: dict[str, Any]) -> None:
    result, _account = run(case["input"], *case["args"])
    expected = fill(case["expected"], case["input"])
    assert result.exit_code == expected["exitCode"]
    if "json" in expected:
        assert json.loads(result.stdout) == expected["json"]
    else:
        assert result.stdout == expected["stdout"]
    assert result.stderr == expected["stderr"]


def test_an_unsupported_region_makes_no_call() -> None:
    case = next(case for case in CASES if case["name"] == "región sin Lambda MicroVMs")
    _result, account = run(case["input"], *case["args"])
    assert account.calls == []


def test_the_tags_reach_the_stack_gateways(monkeypatch: pytest.MonkeyPatch) -> None:
    received: list[dict[str, str] | None] = []
    clients, account = clients_for(FRESH)

    def gateways(_clients: Clients, tags: dict[str, str] | None = None) -> Any:
        received.append(tags)
        return account.gateways

    monkeypatch.setattr("rayito.cli.setup.gateways_for", gateways)
    result = CliRunner().invoke(
        app, ["setup", "--dry-run", "--tag", "team=platform", "--tag", "env=test"], obj=clients
    )
    assert result.exit_code == 0, result.stderr
    assert received == [{"team": "platform", "env": "test"}]


def test_a_tag_without_a_value_separator_is_a_usage_error_before_any_call() -> None:
    result, account = run(FRESH, "setup", "--tag", "team")
    assert result.exit_code == 2
    assert account.calls == []


def test_a_dry_run_only_reads() -> None:
    _result, account = run(FRESH, "setup", "--dry-run")
    assert {call[0] for call in account.calls} == {
        "account",
        "describe",
        "head_bucket",
        "get_image",
        "get_image:caps",
    }


def test_a_dry_run_that_would_create_the_bucket_only_reads() -> None:
    result, account = run(NO_BUCKET, "setup", "--dry-run")
    assert result.exit_code == 0, result.stderr
    assert "describe_artifact_stack" in {call[0] for call in account.calls}
    assert not WRITING_CALLS & {call[0] for call in account.calls}


def test_the_bucket_variable_is_read_by_the_command(monkeypatch: pytest.MonkeyPatch) -> None:
    vector = {**FRESH, "buckets": {"amzn-s3-demo-bucket": "us-east-1"}}
    monkeypatch.setenv("RAYITO_BUCKET", "amzn-s3-demo-bucket")
    result, _account = run(vector, "setup", "--dry-run")
    assert "  bucket amzn-s3-demo-bucket: reutilizar (RAYITO_BUCKET)" in result.stdout


def test_apply_prints_the_progress_the_doctor_and_the_next_step_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("rayito.cli.setup.run_doctor", doctor_returning("OK", "WARN"))
    result, account = run(FRESH, "setup", "--yes")
    lines = result.stdout.splitlines()
    assert result.exit_code == 0, result.stderr
    assert lines[0] == "rayito setup: cuenta 123456789012, región us-east-1"
    assert "creando la pila de IAM rayito-m0-iam" in lines
    assert lines[-3:] == [
        "rayito setup: cuenta preparada en us-east-1",
        "RAYITO_TEMPLATE=arn:aws:lambda:us-east-1:123456789012:microvm-image:rayito-base",
        f"RAYITO_BUCKET={account.iam_stack.stack.parameters['ArtifactBucket']}",
    ]
    assert account.publisher.published_with[1] == BUILD_ROLE_ARN


def test_apply_without_a_bucket_creates_it_and_prints_it_for_the_next_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("rayito.cli.setup.run_doctor", doctor_returning("OK"))
    result, account = run(NO_BUCKET, "setup", "--yes")
    bucket = fill("{defaultBucket}", NO_BUCKET)
    lines = result.stdout.splitlines()
    assert result.exit_code == 0, result.stderr
    assert f"creando el bucket {bucket} con la pila rayito-m0-artifacts" in lines
    assert f"bucket {bucket} listo (pila rayito-m0-artifacts)" in lines
    assert lines[-1] == f"RAYITO_BUCKET={bucket}"
    assert ("create_artifact_stack", bucket) in account.calls


def test_a_doctor_failure_fails_the_command(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("rayito.cli.setup.run_doctor", doctor_returning("OK", "FAIL"))
    result, _account = run(FRESH, "setup", "--yes")
    assert result.exit_code == 1
    assert result.stdout.splitlines()[-1] == MESSAGES["doctorFailed"]
    assert "RAYITO_TEMPLATE=" not in result.stdout


def test_a_prepared_account_runs_only_the_doctor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("rayito.cli.setup.run_doctor", doctor_returning("OK"))
    result, account = run(PREPARED, "setup")
    assert result.exit_code == 0, result.stderr
    assert not WRITING_CALLS & {call[0] for call in account.calls}
    assert MESSAGES["nothingToDo"] in result.stdout
    assert result.stdout.splitlines()[-3] == "rayito setup: cuenta preparada en us-east-1"


@pytest.fixture
def terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "rayito.cli.setup.sys",
        types.SimpleNamespace(stdin=types.SimpleNamespace(isatty=lambda: True)),
    )


def test_a_no_at_the_prompt_exits_without_writing(terminal: None) -> None:
    result, account = run(FRESH, "setup", input_text="n\n")
    assert result.exit_code == 1
    assert "¿Preparar la cuenta? [y/N]: " in result.stdout
    assert not WRITING_CALLS & {call[0] for call in account.calls}


def test_a_yes_at_the_prompt_applies_the_plan(
    terminal: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("rayito.cli.setup.run_doctor", doctor_returning("OK"))
    result, account = run(FRESH, "setup", input_text="y\n")
    assert result.exit_code == 0, result.stderr
    assert ("create", account.iam_stack.stack.parameters["ArtifactBucket"]) in account.calls


def test_json_apply_embeds_the_doctor_and_sends_progress_to_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("rayito.cli.setup.run_doctor", doctor_returning("OK"))
    result, _account = run(FRESH, "--json", "setup", "--yes")
    document = json.loads(result.stdout)
    assert result.exit_code == 0, result.stderr
    assert list(document) == CONTRACT["json"]["documentKeys"]
    assert document["ready"] is True
    assert document["dryRun"] is False
    assert list(document["result"]) == CONTRACT["json"]["resultKeys"]
    assert document["result"]["artifactUploaded"] is True
    assert document["doctor"]["exit_code"] == 0
    assert document["doctor"]["checks"][0]["name"] == "check-0"
    assert document["error"] is None
    assert "creando la pila de IAM rayito-m0-iam" in result.stderr
    assert "creando la pila" not in result.stdout


def test_json_apply_with_a_doctor_failure_is_not_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("rayito.cli.setup.run_doctor", doctor_returning("FAIL"))
    result, _account = run(FRESH, "--json", "setup", "--yes")
    document = json.loads(result.stdout)
    assert (result.exit_code, document["exitCode"], document["ready"]) == (1, 1, False)
    assert document["result"] is not None


def test_an_apply_error_keeps_the_plan_in_the_json_document() -> None:
    clients, account = clients_for(FRESH)
    account.releases.sha256_listed = "0" * 64
    result = CliRunner().invoke(app, ["--json", "setup", "--yes"], obj=clients)
    document = json.loads(result.stdout)
    assert result.exit_code == 1
    assert document["error"]["code"] == "checksum_mismatch"
    assert len(document["steps"]) == 4
    assert document["result"] is None
    assert document["ready"] is False
    assert result.stderr.splitlines()[-1] == f"rayito: {document['error']['message']}"
    assert "publish" not in [call[0] for call in account.calls]


def zip_with(path: Path, entries: dict[str, str]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("Dockerfile", "FROM scratch\n")
        for name, content in entries.items():
            archive.writestr(name, content)
    return path


def test_a_missing_artifact_exits_2_before_any_call(tmp_path: Path) -> None:
    missing = tmp_path / "absent.zip"
    result, account = run(FRESH, "setup", "--dry-run", "--artifact", str(missing))
    assert result.exit_code == 2
    assert result.stderr == f"rayito: {missing} no existe\n"
    assert account.calls == []


@pytest.mark.parametrize(
    "entries",
    [
        {WARMUP_MARKER_ENTRY: SLIM_MARKER_CONTENT},
        {EFS_MARKER_ENTRY: EFS_MARKER_CONTENT},
    ],
    ids=["slim", "efs"],
)
def test_an_artifact_of_another_variant_exits_2_before_any_call(
    tmp_path: Path, entries: dict[str, str]
) -> None:
    artifact = zip_with(tmp_path / "image.zip", entries)
    result, account = run(FRESH, "setup", "--dry-run", "--artifact", str(artifact))
    assert result.exit_code == 2
    assert "no es un zip de la variante full sin EFS" in result.stderr
    assert account.calls == []


def test_a_file_that_is_not_a_zip_is_not_a_full_artifact(tmp_path: Path) -> None:
    artifact = tmp_path / "image.zip"
    artifact.write_text("not a zip", encoding="utf-8")
    result, account = run(FRESH, "setup", "--dry-run", "--artifact", str(artifact))
    assert result.exit_code == 2
    assert account.calls == []


def test_a_full_artifact_is_planned_instead_of_the_release(tmp_path: Path) -> None:
    artifact = zip_with(tmp_path / "image.zip", {})
    result, _account = run(FRESH, "setup", "--dry-run", "--artifact", str(artifact))
    assert result.exit_code == 0, result.stderr
    assert f"publicar desde {artifact} (≈ 10 min)" in result.stdout


def test_plan_templates_match_the_contract() -> None:
    assert MESSAGES["header"] == output.HEADER_TEMPLATE
    assert MESSAGES["step"] == output.STEP_TEMPLATE
    assert dict(output.RESOURCE_LABELS) == MESSAGES["labels"]
    assert dict(output.ACTION_TEMPLATES) == MESSAGES["actions"]
    assert dict(output.BUCKET_SOURCE_LABELS) == MESSAGES["bucketSources"]
    assert MESSAGES["costHeader"] == output.COST_HEADER
    assert MESSAGES["costLine"] == output.COST_LINE_TEMPLATE
    assert MESSAGES["nothingToDo"] == output.NOTHING_TO_DO
    assert MESSAGES["dryRunFooter"] == output.DRY_RUN_FOOTER
    assert MESSAGES["confirm"] == output.CONFIRM_QUESTION
    assert MESSAGES["done"] == output.DONE_TEMPLATE
    assert MESSAGES["templateLine"] == output.TEMPLATE_LINE_TEMPLATE
    assert MESSAGES["bucketLine"] == output.BUCKET_LINE_TEMPLATE
    assert MESSAGES["doctorFailed"] == output.DOCTOR_FAILED
    assert MESSAGES["errorPrefix"] == output.ERROR_PREFIX


def test_the_document_keys_follow_the_contract_order() -> None:
    document = output.setup_document(
        sdk_version=SDK_VERSION,
        region="us-east-1",
        account=None,
        dry_run=False,
        ready=False,
        plan=None,
        result=None,
        doctor=None,
        error={"code": "bucket_taken", "message": "m"},
        exit_code=2,
    )
    assert list(document) == CONTRACT["json"]["documentKeys"]
    assert document["steps"] == [] and document["cost"] == []
    assert list(document["error"]) == CONTRACT["json"]["errorKeys"]


def normalised(text: str) -> str:
    return " ".join(text.split())


def test_the_command_help_and_options_match_the_contract() -> None:
    command = typer.main.get_command(app).commands["setup"]  # type: ignore[attr-defined]
    assert normalised(command.help) == normalised(CONTRACT["help"])
    options = [parameter for parameter in command.params if parameter.name != "help"]
    assert [option.name for option in options] == [o["name"] for o in CONTRACT["options"]]
    for option, expected in zip(options, CONTRACT["options"], strict=True):
        assert option.opts == expected["flags"]
        declared_type = f"list[{option.type.name}]" if option.multiple else option.type.name
        assert declared_type == expected["type"]
        assert option.default == expected["default"]
        assert option.is_flag == expected["isFlag"]
        assert option.envvar is None
        assert normalised(option.help) == normalised(expected["help"])


def failing_clients(error: Exception) -> Clients:
    clients, account = clients_for(FRESH)

    def observe() -> Any:
        raise error

    account.iam_stack.observe = observe  # type: ignore[method-assign]
    return clients


ACCESS_DENIED = ClientError(
    {"Error": {"Code": "AccessDenied", "Message": "no puedes"}}, "DescribeStacks"
)
BLOCKED_STACK = StackException("la pila 'rayito-m0-iam' está bloqueada", code="blocked")


@pytest.mark.parametrize(
    ("error", "message", "exit_code"),
    [
        (ACCESS_DENIED, "AWS error AccessDenied: no puedes", 1),
        (BLOCKED_STACK, "la pila 'rayito-m0-iam' está bloqueada", 1),
        (UsageError("sin credenciales"), "sin credenciales", 2),
    ],
    ids=["aws", "stack", "usage"],
)
def test_json_mode_reports_an_untranslated_error_as_one_document(
    error: Exception, message: str, exit_code: int
) -> None:
    result = CliRunner().invoke(app, ["--json", "setup"], obj=failing_clients(error))
    document = json.loads(result.stdout)
    code = "bucket_unavailable" if exit_code == 2 else "stack_blocked"
    assert result.exit_code == exit_code
    assert list(document) == CONTRACT["json"]["documentKeys"]
    assert document["error"] == {"code": code, "message": message}
    assert document["exitCode"] == exit_code
    assert document["steps"] == [] and document["ready"] is False
    assert result.stderr == f"rayito: {message}\n"


@pytest.mark.parametrize(
    ("error", "line"),
    [
        (ACCESS_DENIED, "rayito: AWS error AccessDenied: no puedes"),
        (BLOCKED_STACK, "rayito: la pila 'rayito-m0-iam' está bloqueada"),
    ],
    ids=["aws", "stack"],
)
def test_human_mode_keeps_the_global_error_line(error: Exception, line: str) -> None:
    result = CliRunner().invoke(app, ["setup"], obj=failing_clients(error))
    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr == f"{line}\n"
