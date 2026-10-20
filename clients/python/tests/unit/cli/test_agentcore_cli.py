"""`rayito agent deploy|invoke|status|stop|list|delete --target agentcore`
(`cli/agent.py`, `cli/_agentcore.py`, `cli/_agentcore_output.py`) sobre dobles de los
puertos: el plan y `--dry-run`, la confirmación, el progreso, los documentos
`--json`, los códigos de salida y que sólo se borra lo de Rayito. Nada toca AWS."""

from __future__ import annotations

import json
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import boto3
import pytest
from typer.testing import CliRunner, Result

from rayito._agentcore._deploy import AgentCorePorts
from rayito._agentcore._messages import ERROR_PREFIX
from rayito._agentcore._models import RuntimeDetails
from rayito._stacks._condition import StackObservation
from rayito.cli._session import Clients
from rayito.cli.app import app
from tests.unit import fake_agentcore as fake
from tests.unit.fake_agent_project import project
from tests.unit.test_agentcore_delete import deployed, foreign, owned
from tests.unit.test_agentcore_invoke import json_body, sse_body

MESSAGES: dict[str, Any] = fake.CONTRACT["messages"]
EXIT_CODES: dict[str, int] = fake.CONTRACT["errorExitCodes"]
JSON_PLAN_KEYS: list[str] = fake.CONTRACT["json"]["planKeys"]


@dataclass
class World:
    clients: Clients
    cloud: fake.FakeCloud
    invoker: fake.FakeInvoker
    directory: Path


def world(tmp_path: Path, *, cloud: fake.FakeCloud | None = None, body: bytes = b"") -> World:
    the_cloud = cloud or fake.cloud()
    invoker = fake.FakeInvoker(body or json_body())
    session = boto3.session.Session(
        region_name=fake.REGION, aws_access_key_id="AKIDEXAMPLE0000000", aws_secret_access_key="x"
    )
    clients = Clients(
        session=session,
        region=fake.REGION,
        agentcore_override=AgentCorePorts(gateways=the_cloud.gateways, invoker=invoker),
    )
    (tmp_path / "rayito-agent.json").write_text(json.dumps(project().to_dict()))
    return World(clients, the_cloud, invoker, tmp_path)


def run(w: World, *args: str, input_text: str | None = None) -> Result:
    return CliRunner().invoke(app, list(args), obj=w.clients, input=input_text)


@pytest.fixture
def terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "rayito.cli._agentcore.sys",
        types.SimpleNamespace(stdin=types.SimpleNamespace(isatty=lambda: True)),
    )


@pytest.fixture(autouse=True)
def known_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAYITO_BUCKET", fake.BUCKET)
    monkeypatch.setenv("AWS_REGION", fake.REGION)


def deploy(w: World, *extra: str, json_output: bool = False) -> Result:
    prefix = ["--json"] if json_output else []
    return run(w, *prefix, "agent", "deploy", str(w.directory), "--target", "agentcore", *extra)


# ----------------------------------------------------------------------- dry run


def test_dry_run_prints_the_plan_security_and_cost_and_calls_aws_never(tmp_path: Path) -> None:
    w = world(tmp_path)
    result = deploy(w, "--dry-run")
    assert result.exit_code == 0, result.stderr
    lines = result.stdout.splitlines()
    assert lines[0] == MESSAGES["header"].format(name="support-bot", region=fake.REGION)
    assert MESSAGES["dryRunFooter"] == lines[-1]
    assert lines.index(MESSAGES["securityHeader"]) > 0
    security = lines[lines.index(MESSAGES["securityHeader"]) + 1]
    assert security == MESSAGES["securityLine"].format(text=MESSAGES["security"]["noGateway"])
    assert lines[lines.index(MESSAGES["costHeader"]) + 1].startswith("  runtime (plataforma V1")
    assert w.cloud.recorder.calls == []


def test_the_dry_run_steps_use_the_planned_texts(tmp_path: Path) -> None:
    lines = deploy(world(tmp_path), "--dry-run").stdout.splitlines()
    stack = MESSAGES["step"].format(
        label="pila",
        name=fake.STACK_NAME,
        action=MESSAGES["actions"]["stackPlanned"],
    )
    assert stack in lines
    assert lines[lines.index(stack) + 1] == MESSAGES["stackContents"]


def test_dry_run_json_has_the_contracts_keys(tmp_path: Path) -> None:
    result = deploy(world(tmp_path), "--dry-run", json_output=True)
    document = json.loads(result.stdout)
    assert list(document) == fake.CONTRACT["json"]["planDocumentKeys"]
    assert document["dryRun"] is True and document["target"] == "agentcore"
    assert list(document["plan"]) == JSON_PLAN_KEYS
    assert list(document["plan"]["lifecycle"]) == fake.CONTRACT["json"]["lifecycleKeys"]
    assert list(document["plan"]["steps"][0]) == fake.CONTRACT["json"]["stepKeys"]
    assert document["plan"]["bucket"] == fake.BUCKET
    assert document["plan"]["bucketSource"] == "environment"


def test_the_agentcore_options_reach_the_plan(tmp_path: Path) -> None:
    result = deploy(
        world(tmp_path),
        "--dry-run",
        "--bedrock-model",
        "amazon.nova-lite-v1:0",
        "--readable-secret",
        "rayito/agents/support/db",
        "--idle-session-timeout",
        "600",
        "--max-session-lifetime",
        "7200",
        "--tag",
        "team=agents",
        json_output=True,
    )
    plan = json.loads(result.stdout)["plan"]
    assert plan["bedrockModels"][-1] == "amazon.nova-lite-v1:0"
    assert plan["readableSecrets"] == ["rayito/agents/support/db"]
    assert plan["lifecycle"] == {
        "idleSessionTimeoutSeconds": 600,
        "maxSessionLifetimeSeconds": 7200,
    }
    assert plan["tags"] == {"team": "agents"}
    assert "readableSecrets" in plan["security"]


# ----------------------------------------------------------------------- errors


def error_of(result: Result) -> dict[str, Any]:
    document = json.loads(result.stdout)
    assert list(document) == ["error", "exitCode"]
    assert list(document["error"]) == fake.CONTRACT["json"]["errorKeys"]
    return document  # type: ignore[no-any-return]


def test_microvm_options_that_do_not_apply_are_option_not_supported(tmp_path: Path) -> None:
    for flag, option in (
        (["--min-vms", "2"], "--min-vms"),
        (["--max-vms", "2"], "--max-vms"),
        (["--warm", "1"], "--warm"),
        (["--memory", "4096"], "--memory"),
        (["--port", "9000"], "--port"),
        (["--timeout", "60"], "--timeout"),
        (["--idle", "60"], "--idle"),
        (["--secret", "bedrock=x"], "--secret"),
        (["--create-secret"], "--create-secret"),
        (["--keep-on-failure"], "--keep-on-failure"),
    ):
        w = world(tmp_path)
        result = deploy(w, "--dry-run", *flag, json_output=True)
        document = error_of(result)
        assert document["error"]["code"] == "option_not_supported", flag
        assert document["exitCode"] == 2 and result.exit_code == 2
        assert option in document["error"]["message"]
        assert w.cloud.recorder.calls == []


def test_the_token_file_flag_is_not_supported_with_agentcore(tmp_path: Path) -> None:
    result = deploy(world(tmp_path), "--dry-run", "--token-file", "t", json_output=True)
    assert error_of(result)["error"]["code"] == "option_not_supported"


def test_an_egress_policy_is_network_policy_unsupported(tmp_path: Path) -> None:
    result = deploy(
        world(tmp_path), "--dry-run", "--allow-out", "api.example.com", json_output=True
    )
    assert error_of(result)["error"]["code"] == "network_policy_unsupported"


def test_agentcore_flags_with_the_microvm_target_are_option_requires_agentcore(
    tmp_path: Path,
) -> None:
    w = world(tmp_path)
    for flag in (
        ["--tag", "a=b"],
        ["--bedrock-model", "x"],
        ["--readable-secret", "x"],
        ["--idle-session-timeout", "100"],
        ["--max-session-lifetime", "100"],
    ):
        result = run(w, "--json", "agent", "deploy", str(tmp_path), "--dry-run", *flag)
        document = error_of(result)
        assert document["error"]["code"] == "option_requires_agentcore"
        assert result.exit_code == 2
    assert w.cloud.recorder.calls == []


def test_an_invalid_lifecycle_is_lifecycle_invalid_with_exit_two(tmp_path: Path) -> None:
    result = deploy(world(tmp_path), "--dry-run", "--idle-session-timeout", "30", json_output=True)
    document = error_of(result)
    assert document["error"]["code"] == "lifecycle_invalid" and document["exitCode"] == 2
    assert result.stderr.startswith(ERROR_PREFIX)


def test_an_unsupported_region_is_exit_two(tmp_path: Path) -> None:
    w = world(tmp_path)
    result = run(
        w,
        "--region",
        "eu-central-2",
        "--json",
        "agent",
        "deploy",
        str(tmp_path),
        "--target",
        "agentcore",
        "--dry-run",
    )
    assert error_of(result)["error"]["code"] == "unsupported_region"


def test_the_target_must_be_one_of_the_two(tmp_path: Path) -> None:
    result = run(world(tmp_path), "agent", "deploy", str(tmp_path), "--target", "ecs")
    assert result.exit_code == 2


# ---------------------------------------------------------------- confirm / apply


def test_without_a_terminal_or_yes_the_deploy_needs_confirmation(tmp_path: Path) -> None:
    w = world(tmp_path)
    result = deploy(w, json_output=True)
    document = error_of(result)
    assert document["error"]["code"] == "needs_confirmation" and result.exit_code == 2
    assert not {"create_stack", "put_object", "create_runtime"} & set(w.cloud.recorder.names())


def test_a_no_at_the_prompt_exits_one_without_writing(tmp_path: Path, terminal: None) -> None:
    w = world(tmp_path)
    result = (
        deploy(w, input_text="n\n")
        if False
        else run(w, "agent", "deploy", str(tmp_path), "--target", "agentcore", input_text="n\n")
    )
    assert result.exit_code == 1
    assert MESSAGES["confirm"] in result.stdout
    assert "create_stack" not in w.cloud.recorder.names()


def test_yes_deploys_prints_progress_on_stderr_and_the_result_on_stdout(tmp_path: Path) -> None:
    w = world(tmp_path)
    result = deploy(w, "--yes")
    assert result.exit_code == 0, result.stderr
    out = result.stdout.splitlines()
    start = out.index("target=agentcore")
    assert out[start : start + 3] == [
        "target=agentcore",
        "name=support-bot",
        f"runtime_id={fake.RUNTIME_ID}",
    ]
    assert out[-1] == MESSAGES["deployed"][-1].format(name="support-bot")
    assert out[0] == MESSAGES["header"].format(name="support-bot", region=fake.REGION)
    assert MESSAGES["progress"]["stackDeploy"].format(stackName=fake.STACK_NAME) in result.stderr
    assert w.cloud.recorder.count("create_runtime") == 1


def test_json_deploy_is_one_document_with_the_contracts_keys(tmp_path: Path) -> None:
    w = world(tmp_path)
    result = deploy(w, "--yes", json_output=True)
    document = json.loads(result.stdout)
    assert list(document) == fake.CONTRACT["json"]["deployKeys"]
    assert document["target"] == "agentcore" and document["status"] == "READY"
    assert document["stackName"] == fake.STACK_NAME
    assert [step["resource"] for step in document["steps"]] == ["stack", "image", "runtime"]
    assert document["steps"][2]["action"] == "create"
    assert MESSAGES["progress"]["stackDeploy"].format(stackName=fake.STACK_NAME) in result.stderr
    assert "arn:aws" not in result.stderr


def test_an_aws_failure_is_exit_one_with_a_closed_code(tmp_path: Path) -> None:
    cloud = fake.cloud()
    cloud.builder.statuses = ["FAILED"]
    w = world(tmp_path, cloud=cloud)
    result = deploy(w, "--yes", json_output=True)
    document = error_of(result)
    assert document["error"]["code"] == "image_build_failed"
    assert document["exitCode"] == 1 == result.exit_code
    assert EXIT_CODES["image_build_failed"] == 1


# ---------------------------------------------------------------- invoke / status


def deployed_world(tmp_path: Path, *, body: bytes = b"") -> World:
    stack = StackObservation(fake.STACK_NAME, "UPDATE_COMPLETE", dict(fake.DEFAULT_OUTPUTS), {})
    cloud = fake.cloud(stack=stack, listing=owned())
    cloud.runtimes.details = RuntimeDetails(
        container_uri="123456789012.dkr.ecr.us-east-1.amazonaws.com/rayito/agentcore/x:src-1",
        role_arn="r",
        network_mode="PUBLIC",
        server_protocol="HTTP",
        environment_variables={},
        idle_runtime_session_timeout=900,
        max_lifetime=28800,
        require_mmds_v2=True,
    )
    return world(tmp_path, cloud=cloud, body=body)


def test_invoke_prints_the_text_and_the_session_id_to_use_next(tmp_path: Path) -> None:
    w = deployed_world(tmp_path)
    result = run(w, "agent", "invoke", fake.AGENT, "hola", "--target", "agentcore")
    assert result.exit_code == 0, result.stderr
    assert result.stdout.strip() == "42"
    session = w.invoker.calls[0][2]
    assert f"session_id={session}" in result.stderr


def test_invoke_continues_the_session_with_the_given_id(tmp_path: Path) -> None:
    w = deployed_world(tmp_path)
    run(
        w, "agent", "invoke", fake.AGENT, "hola", "--target", "agentcore", "--session-id", "user-42"
    )
    assert w.invoker.calls[0][2] == "rayito-6d894aa3ee802549d7f340e7c1cf0d1c"


def test_invoke_json_and_stream(tmp_path: Path) -> None:
    w = deployed_world(tmp_path)
    document = json.loads(
        run(w, "--json", "agent", "invoke", fake.AGENT, "hola", "--target", "agentcore").stdout
    )
    assert document["text"] == "42" and document["sessionId"] == w.invoker.calls[0][2]
    streaming = deployed_world(tmp_path, body=sse_body())
    result = run(
        streaming, "agent", "invoke", fake.AGENT, "hola", "--target", "agentcore", "--stream"
    )
    assert result.exit_code == 0, result.stderr
    assert "42" in result.stdout
    assert streaming.invoker.calls[0][0] == "stream"


def test_invoke_rejects_the_token_file(tmp_path: Path) -> None:
    w = deployed_world(tmp_path)
    result = run(
        w,
        "--json",
        "agent",
        "invoke",
        fake.AGENT,
        "hola",
        "--target",
        "agentcore",
        "--token-file",
        "t",
    )
    assert error_of(result)["error"]["code"] == "option_not_supported"
    assert w.invoker.calls == []


def test_invoking_an_agent_that_does_not_exist_is_runtime_not_found(tmp_path: Path) -> None:
    w = world(tmp_path)
    result = run(w, "--json", "agent", "invoke", "nobody", "hola", "--target", "agentcore")
    document = error_of(result)
    assert document["error"]["code"] == "runtime_not_found" and result.exit_code == 1


def test_status_prints_the_contracts_lines_and_document(tmp_path: Path) -> None:
    w = deployed_world(tmp_path)
    text = run(w, "agent", "status", fake.AGENT, "--target", "agentcore")
    assert text.stdout.splitlines()[0] == "target: agentcore"
    assert f"runtime_id: {fake.RUNTIME_ID}" in text.stdout
    document = json.loads(
        run(w, "--json", "agent", "status", fake.AGENT, "--target", "agentcore").stdout
    )
    assert list(document) == fake.CONTRACT["json"]["statusKeys"]
    assert document["status"] == "READY"


def test_stop_needs_a_session_with_agentcore(tmp_path: Path) -> None:
    w = deployed_world(tmp_path)
    result = run(w, "--json", "agent", "stop", fake.AGENT, "--target", "agentcore")
    assert error_of(result)["error"]["code"] == "session_required" and result.exit_code == 2
    stopped = run(
        w, "--json", "agent", "stop", fake.AGENT, "--target", "agentcore", "--session-id", "user-42"
    )
    document = json.loads(stopped.stdout)
    assert list(document) == fake.CONTRACT["json"]["stopKeys"]
    assert document["stopped"] is True
    assert w.invoker.stopped == ["rayito-6d894aa3ee802549d7f340e7c1cf0d1c"]
    text = run(w, "agent", "stop", fake.AGENT, "--target", "agentcore", "--session-id", "user-42")
    assert text.stdout.strip() == MESSAGES["stopped"].format(name=fake.AGENT, sessionId="user-42")


def test_stop_session_id_without_agentcore_is_option_requires_agentcore(tmp_path: Path) -> None:
    result = run(
        deployed_world(tmp_path), "--json", "agent", "stop", fake.AGENT, "--session-id", "x"
    )
    assert error_of(result)["error"]["code"] == "option_requires_agentcore"


# ---------------------------------------------------------------- list / delete


def test_list_shows_only_rayito_agents(tmp_path: Path) -> None:
    cloud = deployed()
    w = world(tmp_path, cloud=cloud)
    text = run(w, "agent", "list", "--target", "agentcore")
    assert text.stdout.splitlines() == [
        MESSAGES["listLine"].format(
            name="other-bot",
            runtimeName="rayito_other_bot",
            runtimeId="other-bot-0123456789",
            version="2",
            status="READY",
        ),
        MESSAGES["listLine"].format(
            name=fake.AGENT,
            runtimeName=fake.RUNTIME_NAME,
            runtimeId=fake.RUNTIME_ID,
            version="2",
            status="READY",
        ),
    ]
    document = json.loads(run(w, "--json", "agent", "list", "--target", "agentcore").stdout)
    assert list(document) == fake.CONTRACT["json"]["listKeys"]
    assert list(document["agents"][0]) == fake.CONTRACT["json"]["listItemKeys"]


def test_an_empty_list_says_so(tmp_path: Path) -> None:
    result = run(world(tmp_path), "agent", "list", "--target", "agentcore")
    assert result.stdout.strip() == MESSAGES["listEmpty"].format(region=fake.REGION)


def test_list_and_delete_require_the_target() -> None:
    assert CliRunner().invoke(app, ["agent", "list"]).exit_code == 2
    assert CliRunner().invoke(app, ["agent", "delete", "x"]).exit_code == 2
    assert CliRunner().invoke(app, ["agent", "list", "--target", "microvm"]).exit_code == 2


def test_delete_with_yes_removes_exactly_what_rayito_created(tmp_path: Path) -> None:
    cloud = deployed()
    w = world(tmp_path, cloud=cloud)
    result = run(w, "agent", "delete", fake.AGENT, "--target", "agentcore", "--yes")
    assert result.exit_code == 0, result.stderr
    lines = result.stdout.splitlines()
    assert lines[0] == MESSAGES["deleteHeader"].format(name=fake.AGENT, region=fake.REGION)
    assert lines[-1] == MESSAGES["deleted"].format(
        name=fake.AGENT, runtimeDeleted="true", stackDeleted="true", logGroupDeleted="true"
    )
    assert cloud.runtimes.deleted == [fake.RUNTIME_ID]
    assert cloud.recorder.count("delete_stack") == 1
    assert cloud.recorder.count("delete_log_group") == 1


def test_delete_json_without_yes_exits_two_and_writes_nothing(tmp_path: Path) -> None:
    cloud = deployed()
    result = run(
        world(tmp_path, cloud=cloud),
        "--json",
        "agent",
        "delete",
        fake.AGENT,
        "--target",
        "agentcore",
    )
    document = error_of(result)
    assert document["error"]["code"] == "delete_needs_confirmation" and result.exit_code == 2
    assert not {"delete_runtime", "delete_stack", "delete_log_group"} & set(cloud.recorder.names())


def test_delete_asks_in_a_terminal_and_a_no_stops_it(tmp_path: Path, terminal: None) -> None:
    cloud = deployed()
    result = run(
        world(tmp_path, cloud=cloud),
        "agent",
        "delete",
        fake.AGENT,
        "--target",
        "agentcore",
        input_text="n\n",
    )
    assert result.exit_code == 1
    assert MESSAGES["deleteConfirm"] in result.stdout
    assert "delete_runtime" not in cloud.recorder.names()


def test_delete_with_nothing_there_is_a_clean_exit(tmp_path: Path) -> None:
    w = world(tmp_path)
    result = run(w, "agent", "delete", fake.AGENT, "--target", "agentcore")
    assert result.exit_code == 0
    assert result.stdout.strip() == MESSAGES["deleteNothing"].format(
        name=fake.AGENT, region=fake.REGION
    )
    json_result = run(w, "--json", "agent", "delete", fake.AGENT, "--target", "agentcore")
    document = json.loads(json_result.stdout)
    assert list(document) == fake.CONTRACT["json"]["deleteKeys"]
    assert (document["runtimeDeleted"], document["stackDeleted"], document["logGroupDeleted"]) == (
        False,
        False,
        False,
    )


def test_delete_never_touches_a_runtime_that_is_not_rayitos(tmp_path: Path) -> None:
    cloud = fake.cloud(listing=foreign(fake.RUNTIME_NAME))
    result = run(
        world(tmp_path, cloud=cloud),
        "--json",
        "agent",
        "delete",
        fake.AGENT,
        "--target",
        "agentcore",
        "--yes",
    )
    assert error_of(result)["error"]["code"] == "name_conflict"
    assert "delete_runtime" not in cloud.recorder.names()
