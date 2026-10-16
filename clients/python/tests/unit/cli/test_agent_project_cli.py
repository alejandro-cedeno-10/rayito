"""`rayito agent init | build | run | deploy | export | invoke | status | stop`
(`agent-runtimes-byo`): conchas sobre `AgentProject` y `AgentDeployment`,
probadas con dobles. Nada toca AWS."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import pytest
from typer.testing import CliRunner

from rayito import AgentProject
from rayito._agent._custom import CustomRuntime, HttpApp
from rayito._agent._events import (
    AgentFailed,
    Done,
    StepStarted,
    Text,
    TextDelta,
    TokenUsage,
    ToolCall,
)
from rayito.cli import agent as agent_cli
from rayito.cli._agent_scaffold import ScaffoldRequest, scaffold_files
from rayito.cli._session import Clients
from rayito.cli.app import app
from rayito.exceptions import AgentException, InvalidArgumentException, SandboxException

REPO_ROOT_DEPTH: Final = 5
SCAFFOLD_VECTORS: Final = (
    Path(__file__).resolve().parents[REPO_ROOT_DEPTH]
    / "testdata"
    / "cli"
    / "agent-scaffold"
    / "cases.json"
)
UPDATE_ENV: Final = "RAYITO_UPDATE_AGENT_SCAFFOLD"
SANDBOX_ID: Final = "microvm-00000000-0000-0000-0000-000000000001"
DEPLOYMENT_URL: Final = "https://abc.lambda-microvm.us-east-1.on.aws"
PROXY_HEADER_VALUE: Final = "eyJhbGciOiJkaXIifQ..fake.jwe"
USAGE: Final = TokenUsage(input=10, output=5)
TOKEN_ENV: Final = "RAYITO_ACCESS_TOKEN"
ACCESS_TOKEN: Final = "fake-access-token"


@dataclass
class FakeResult:
    session_id: str = "ses_1"
    text: str = "hola"
    steps: int = 2
    usage: TokenUsage = USAGE
    exit_code: int = 0
    usage_reported: bool = True


@dataclass(frozen=True)
class FakeServe:
    port: int = 8088
    timeout_seconds: float = 3600.0


@dataclass
class FakeStatus:
    state: str = "RUNNING"
    ready: bool = True
    busy: bool = False
    port: int = 8088
    project: str = "support-bot"


@dataclass
class FakeDeployment:
    sandbox_id: str = SANDBOX_ID
    url: str = DEPLOYMENT_URL
    headers: dict[str, str] = field(default_factory=lambda: {"authorization": PROXY_HEADER_VALUE})
    events: list[Any] = field(default_factory=list)
    calls: list[tuple[str, Any]] = field(default_factory=list)

    def invoke(self, prompt: str, *, session_id: str | None = None) -> FakeResult:
        self.calls.append(("invoke", (prompt, session_id)))
        return FakeResult()

    @contextmanager
    def stream(self, prompt: str, *, session_id: str | None = None) -> Iterator[list[Any]]:
        self.calls.append(("stream", (prompt, session_id)))
        yield list(self.events)

    def status(self) -> FakeStatus:
        return FakeStatus()

    def stop(self) -> None:
        self.calls.append(("stop", None))


@dataclass(frozen=True)
class FakeExport:
    path: str = "/out"
    files: tuple[str, ...] = ("Dockerfile", "README.md")
    port: int = 8080
    platform: str = "linux/arm64"


@dataclass(frozen=True)
class FakeProject:
    runtime: str = "deepagents"
    serve: FakeServe = FakeServe()
    log: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    run_error: Exception | None = None
    run_events: tuple[Any, ...] = ()
    deployment: FakeDeployment = field(default_factory=FakeDeployment)

    def run(self, prompt: str, **kwargs: Any) -> FakeResult:
        self.log.append(("run", {"prompt": prompt, **kwargs}))
        if self.run_error is not None:
            raise self.run_error
        sink = kwargs["on_event"]
        if sink is not None:
            for event in self.run_events:
                sink(event)
        return FakeResult()

    def deploy(self, **kwargs: Any) -> FakeDeployment:
        self.log.append(("deploy", kwargs))
        return self.deployment

    def export(self, **kwargs: Any) -> FakeExport:
        self.log.append(("export", kwargs))
        return FakeExport()

    def build(self, **kwargs: Any) -> Any:
        self.log.append(("build", kwargs))
        return type(
            "Info", (), {"template_id": "tpl-1", "build_id": "bld-1", "alias": "support-bot"}
        )()


@pytest.fixture
def access_token(monkeypatch: pytest.MonkeyPatch) -> str:
    """El token del despliegue, por el entorno como lo da quien ya lo tiene."""
    monkeypatch.setenv(TOKEN_ENV, ACCESS_TOKEN)
    return ACCESS_TOKEN


@pytest.fixture
def no_access_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(TOKEN_ENV, raising=False)


def install_project(monkeypatch: pytest.MonkeyPatch, project: FakeProject) -> FakeProject:
    loader = type(
        "FakeAgentProject",
        (),
        {"load": staticmethod(lambda path: _loaded(project, path))},
    )
    monkeypatch.setattr(agent_cli, "AgentProject", loader)
    return project


def _loaded(project: FakeProject, path: str) -> FakeProject:
    project.log.append(("load", {"path": path}))
    return project


def install_deployment(
    monkeypatch: pytest.MonkeyPatch,
    deployment: FakeDeployment,
    connected: list[dict[str, Any]] | None = None,
) -> None:
    def connect(sandbox_id: str, **kwargs: Any) -> FakeDeployment:
        if connected is not None:
            connected.append({"sandbox_id": sandbox_id, **kwargs})
        return deployment

    monkeypatch.setattr(
        agent_cli,
        "AgentDeployment",
        type("FakeAgentDeployment", (), {"connect": staticmethod(connect)}),
    )


def scaffold_cases() -> list[dict[str, Any]]:
    requests = [
        ScaffoldRequest("support-bot", runtime, language, provider, model, "us-east-1")
        for runtime, language, provider, model in (
            ("opencode", "python", "bedrock", "us.anthropic.claude-haiku-4-5-20251001-v1:0"),
            ("deepagents", "python", "anthropic", "claude-haiku-4-5"),
            ("langgraph", "python", "bedrock", "us.anthropic.claude-haiku-4-5-20251001-v1:0"),
            ("strands", "python", "bedrock", "us.anthropic.claude-haiku-4-5-20251001-v1:0"),
            ("strands", "typescript", "openai", "gpt-4.1-mini"),
            ("strands-harness", "python", "anthropic", "claude-haiku-4-5"),
            ("hermes", "python", "anthropic", "claude-haiku-4-5"),
            ("custom", "python", "bedrock", "us.anthropic.claude-haiku-4-5-20251001-v1:0"),
        )
    ]
    return [
        {
            "case": f"{request.runtime}-{request.language}-{request.provider}",
            "request": {
                "name": request.name,
                "runtime": request.runtime,
                "language": request.language,
                "provider": request.provider,
                "model_id": request.model_id,
                "region": request.region,
            },
            "files": scaffold_files(request),
        }
        for request in requests
    ]


def test_scaffold_matches_the_shared_vectors() -> None:
    rendered = json.dumps(scaffold_cases(), indent=2, ensure_ascii=False) + "\n"
    if os.environ.get(UPDATE_ENV):
        SCAFFOLD_VECTORS.parent.mkdir(parents=True, exist_ok=True)
        SCAFFOLD_VECTORS.write_text(rendered, encoding="utf-8")
    assert SCAFFOLD_VECTORS.read_text(encoding="utf-8") == rendered


def test_scaffold_never_carries_a_secret_value() -> None:
    for case in scaffold_cases():
        project_file = case["files"]["rayito-agent.toml"]
        assert 'secret = "provider-key"' in project_file
        assert "Bearer" not in project_file


@pytest.fixture(autouse=True)
def _no_aws_session(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args: Any, **_kwargs: Any) -> Clients:
        raise AssertionError("el comando no debía abrir una sesión de AWS")

    monkeypatch.setattr("rayito.cli._session.resolve_session", refuse)


def test_init_writes_the_project_file_and_the_scaffold(tmp_path: Path, runner: CliRunner) -> None:
    target = tmp_path / "Support Bot"
    result = runner.invoke(
        app, ["agent", "init", str(target), "--runtime", "strands", "--region", "us-east-1"]
    )
    assert result.exit_code == 0, result.output
    assert (target / "agent.py").is_file()
    project_file = (target / "rayito-agent.toml").read_text(encoding="utf-8")
    assert 'name = "support-bot"' in project_file
    assert 'kind = "strands"' in project_file


def test_init_does_not_overwrite_without_force(tmp_path: Path, runner: CliRunner) -> None:
    existing = tmp_path / "rayito-agent.toml"
    existing.write_text("mine", encoding="utf-8")
    result = runner.invoke(app, ["agent", "init", str(tmp_path), "--runtime", "strands"])
    assert result.exit_code == 2
    assert existing.read_text(encoding="utf-8") == "mine"
    assert "--force" in result.output
    assert not (tmp_path / "agent.py").exists()


def test_init_force_overwrites(tmp_path: Path, runner: CliRunner) -> None:
    existing = tmp_path / "rayito-agent.toml"
    existing.write_text("mine", encoding="utf-8")
    result = runner.invoke(app, ["agent", "init", str(tmp_path), "--runtime", "custom", "--force"])
    assert result.exit_code == 0, result.output
    assert existing.read_text(encoding="utf-8").startswith("schema =")
    assert (tmp_path / "serve.py").is_file()
    assert (tmp_path / "requirements.txt").is_file()


def test_init_json_lists_the_written_files(tmp_path: Path, runner: CliRunner) -> None:
    result = runner.invoke(
        app, ["--json", "agent", "init", str(tmp_path), "--runtime", "langgraph"]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["files"] == [
        "rayito-agent.toml",
        "AGENTS.md",
        "graph.py",
    ]


@pytest.mark.parametrize(
    "arguments",
    [
        ["--runtime", "opencode", "--language", "typescript"],
        ["--runtime", "strands", "--provider", "openai"],
        ["--runtime", "nope"],
        ["--runtime", "strands", "--provider", "nope"],
    ],
)
def test_init_rejects_invalid_combinations_before_writing(
    tmp_path: Path, runner: CliRunner, arguments: list[str]
) -> None:
    result = runner.invoke(app, ["agent", "init", str(tmp_path), *arguments])
    assert result.exit_code == 2
    assert list(tmp_path.iterdir()) == []


def test_build_forwards_the_options(
    monkeypatch: pytest.MonkeyPatch, runner: CliRunner, clients: Clients
) -> None:
    project = install_project(monkeypatch, FakeProject())
    result = runner.invoke(
        app,
        ["--json", "agent", "build", "--bucket", "amzn-s3-demo-bucket", "--file", "p"],
        obj=clients,
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {
        "templateId": "tpl-1",
        "buildId": "bld-1",
        "alias": "support-bot",
    }
    name, kwargs = project.log[-1]
    assert name == "build"
    assert kwargs["bucket"] == "amzn-s3-demo-bucket"
    assert kwargs["region"] == clients.region
    assert kwargs["on_build_logs"] is None


def test_run_prints_the_text_and_the_summary(
    monkeypatch: pytest.MonkeyPatch, runner: CliRunner, clients: Clients
) -> None:
    project = install_project(monkeypatch, FakeProject())
    result = runner.invoke(
        app, ["agent", "run", "hola", "--session-id", "ses_0", "--model", "m"], obj=clients
    )
    assert result.exit_code == 0, result.output
    assert "hola" in result.output
    _, kwargs = project.log[-1]
    assert (kwargs["session_id"], kwargs["model"], kwargs["on_event"]) == ("ses_0", "m", None)


def test_run_json_document(
    monkeypatch: pytest.MonkeyPatch, runner: CliRunner, clients: Clients
) -> None:
    install_project(monkeypatch, FakeProject())
    result = runner.invoke(app, ["--json", "agent", "run", "hola"], obj=clients)
    assert json.loads(result.output) == {
        "sessionId": "ses_1",
        "text": "hola",
        "steps": 2,
        "usage": {
            "input": 10,
            "output": 5,
            "reasoning": 0,
            "cacheRead": 0,
            "cacheWrite": 0,
            "total": 15,
        },
        "usageReported": True,
        "exitCode": 0,
    }


def test_run_says_unknown_when_the_agent_reported_no_tokens(
    monkeypatch: pytest.MonkeyPatch, runner: CliRunner, clients: Clients
) -> None:
    @dataclass(frozen=True)
    class Project(FakeProject):
        def run(self, prompt: str, **kwargs: Any) -> FakeResult:
            return FakeResult(usage=TokenUsage(), usage_reported=False)

    install_project(monkeypatch, Project())
    text = runner.invoke(app, ["agent", "run", "hola"], obj=clients)
    assert "tokens=desconocido" in text.output
    document = runner.invoke(app, ["--json", "agent", "run", "hola"], obj=clients)
    assert json.loads(document.output)["usageReported"] is False


def test_run_stream_prints_deltas_then_a_newline(
    monkeypatch: pytest.MonkeyPatch, runner: CliRunner, clients: Clients
) -> None:
    events = (
        StepStarted(1),
        TextDelta("ho"),
        TextDelta("la"),
        Text("hola"),
        Done("ses_1", 0, USAGE),
    )
    install_project(monkeypatch, FakeProject(run_events=events))
    result = runner.invoke(app, ["agent", "run", "hola", "--stream"], obj=clients)
    assert result.exit_code == 0, result.output
    assert result.stdout == "hola\n"


def test_run_stream_json_is_one_line_per_event(
    monkeypatch: pytest.MonkeyPatch, runner: CliRunner, clients: Clients
) -> None:
    events = (ToolCall("c1", "read", "completed"), Done("ses_1", 0, USAGE))
    install_project(monkeypatch, FakeProject(run_events=events))
    result = runner.invoke(app, ["--json", "agent", "run", "hola", "--stream"], obj=clients)
    lines = [json.loads(line) for line in result.stdout.splitlines()]
    assert [line["type"] for line in lines] == ["tool_call", "done"]
    assert lines[0]["callId"] == "c1"


def test_run_stream_failure_exits_one(
    monkeypatch: pytest.MonkeyPatch, runner: CliRunner, clients: Clients
) -> None:
    install_project(monkeypatch, FakeProject(run_events=(AgentFailed("model_error"),)))
    result = runner.invoke(app, ["agent", "run", "hola", "--stream"], obj=clients)
    assert result.exit_code == 1


def test_run_agent_failure_exits_one(
    monkeypatch: pytest.MonkeyPatch, runner: CliRunner, clients: Clients
) -> None:
    failure = AgentException("falló", reason="model_error")
    install_project(monkeypatch, FakeProject(run_error=failure))
    result = runner.invoke(app, ["agent", "run", "hola"], obj=clients)
    assert result.exit_code == 1
    assert "falló" in result.output


def test_invalid_project_file_exits_two(
    monkeypatch: pytest.MonkeyPatch, runner: CliRunner, clients: Clients
) -> None:
    def refuse(_path: str) -> None:
        raise InvalidArgumentException("runtime.entrpoint: clave desconocida")

    loader = type("FakeAgentProject", (), {"load": staticmethod(refuse)})
    monkeypatch.setattr(agent_cli, "AgentProject", loader)
    result = runner.invoke(app, ["agent", "run", "hola"], obj=clients)
    assert result.exit_code == 2
    assert "runtime.entrpoint" in result.output


@pytest.mark.usefixtures("access_token")
def test_deploy_json_never_prints_the_proxy_headers(
    monkeypatch: pytest.MonkeyPatch, runner: CliRunner, clients: Clients
) -> None:
    install_project(monkeypatch, FakeProject())
    result = runner.invoke(app, ["--json", "agent", "deploy"], obj=clients)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {
        "sandboxId": SANDBOX_ID,
        "url": DEPLOYMENT_URL,
        "port": 8088,
    }
    assert PROXY_HEADER_VALUE not in result.output
    assert "authorization" not in result.output.lower()


@pytest.mark.usefixtures("access_token")
def test_deploy_text_never_prints_the_proxy_headers(
    monkeypatch: pytest.MonkeyPatch, runner: CliRunner, clients: Clients
) -> None:
    install_project(monkeypatch, FakeProject())
    result = runner.invoke(app, ["agent", "deploy"], obj=clients)
    assert f"sandbox_id={SANDBOX_ID}" in result.output
    assert PROXY_HEADER_VALUE not in result.output


@pytest.mark.usefixtures("access_token")
def test_deploy_overrides_only_what_was_passed(
    monkeypatch: pytest.MonkeyPatch, runner: CliRunner, clients: Clients
) -> None:
    seen: list[Any] = []

    @dataclass(frozen=True)
    class Project(FakeProject):
        def deploy(self, **kwargs: Any) -> FakeDeployment:
            seen.append(self.serve)
            return self.deployment

    install_project(monkeypatch, Project())
    runner.invoke(app, ["agent", "deploy"], obj=clients)
    runner.invoke(app, ["agent", "deploy", "--port", "9100", "--timeout", "120"], obj=clients)
    assert seen[0] == FakeServe()
    assert seen[1] == FakeServe(port=9100, timeout_seconds=120.0)


@pytest.mark.usefixtures("access_token")
def test_deploy_passes_the_token_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, runner: CliRunner, clients: Clients
) -> None:
    project = install_project(monkeypatch, FakeProject())
    runner.invoke(app, ["agent", "deploy"], obj=clients)
    deploy_calls = [kwargs for name, kwargs in project.log if name == "deploy"]
    assert [call["access_token"] for call in deploy_calls] == [ACCESS_TOKEN]


@pytest.mark.usefixtures("no_access_token")
def test_deploy_writes_a_new_token_to_the_token_file(
    monkeypatch: pytest.MonkeyPatch, runner: CliRunner, clients: Clients, tmp_path: Path
) -> None:
    project = install_project(monkeypatch, FakeProject())
    token_file = tmp_path / "token"
    result = runner.invoke(app, ["agent", "deploy", "--token-file", str(token_file)], obj=clients)
    assert result.exit_code == 0, result.output
    token = token_file.read_text()
    assert token
    assert token not in result.output
    assert token_file.stat().st_mode & 0o777 == 0o600
    deploy_calls = [kwargs for name, kwargs in project.log if name == "deploy"]
    assert [call["access_token"] for call in deploy_calls] == [token]


@pytest.mark.usefixtures("no_access_token")
def test_deploy_without_any_way_to_reconnect_creates_nothing(
    monkeypatch: pytest.MonkeyPatch, runner: CliRunner, clients: Clients
) -> None:
    project = install_project(monkeypatch, FakeProject())
    result = runner.invoke(app, ["agent", "deploy"], obj=clients)
    assert result.exit_code == 2
    assert "--token-file" in result.output
    assert [name for name, _ in project.log if name == "deploy"] == []


@pytest.mark.usefixtures("no_access_token")
def test_deploy_refuses_an_existing_token_file(
    monkeypatch: pytest.MonkeyPatch, runner: CliRunner, clients: Clients, tmp_path: Path
) -> None:
    project = install_project(monkeypatch, FakeProject())
    token_file = tmp_path / "token"
    token_file.write_text("old")
    result = runner.invoke(app, ["agent", "deploy", "--token-file", str(token_file)], obj=clients)
    assert result.exit_code == 2
    assert token_file.read_text() == "old"
    assert [name for name, _ in project.log if name == "deploy"] == []


@pytest.mark.usefixtures("access_token")
def test_deploy_keeps_the_sandbox_on_failure_only_when_asked(
    monkeypatch: pytest.MonkeyPatch, runner: CliRunner, clients: Clients
) -> None:
    project = install_project(monkeypatch, FakeProject())
    runner.invoke(app, ["agent", "deploy"], obj=clients)
    runner.invoke(app, ["agent", "deploy", "--keep-on-failure"], obj=clients)
    calls = [kwargs for name, kwargs in project.log if name == "deploy"]
    assert [call["keep_on_failure"] for call in calls] == [False, True]


@pytest.mark.usefixtures("access_token")
def test_a_failed_deploy_prints_the_cause_and_the_notes(
    monkeypatch: pytest.MonkeyPatch, runner: CliRunner, clients: Clients
) -> None:
    @dataclass(frozen=True)
    class Project(FakeProject):
        def deploy(self, **kwargs: Any) -> FakeDeployment:
            failure = SandboxException(
                "el host del agente no pudo arrancar (app_exited). Final de la salida de error "
                "de la app:\nModuleNotFoundError: agent"
            )
            failure.add_note(f"keep_on_failure=True: el sandbox {SANDBOX_ID} sigue vivo")
            raise failure

    install_project(monkeypatch, Project())
    result = runner.invoke(app, ["agent", "deploy", "--keep-on-failure"], obj=clients)
    assert result.exit_code == 1
    assert "ModuleNotFoundError: agent" in result.output
    assert SANDBOX_ID in result.output


@pytest.mark.usefixtures("access_token")
def test_an_invalid_argument_failure_also_prints_the_notes(
    monkeypatch: pytest.MonkeyPatch, runner: CliRunner, clients: Clients
) -> None:
    @dataclass(frozen=True)
    class Project(FakeProject):
        def deploy(self, **kwargs: Any) -> FakeDeployment:
            failure = InvalidArgumentException("pasarela sin configurar")
            failure.add_note(f"keep_on_failure=True: el sandbox {SANDBOX_ID} sigue vivo")
            raise failure

    install_project(monkeypatch, Project())
    result = runner.invoke(app, ["agent", "deploy", "--keep-on-failure"], obj=clients)
    assert result.exit_code == 2
    assert "pasarela sin configurar" in result.output
    assert SANDBOX_ID in result.output


@pytest.mark.usefixtures("access_token")
def test_any_other_failure_names_the_kept_sandbox_too(
    monkeypatch: pytest.MonkeyPatch, runner: CliRunner, clients: Clients
) -> None:
    @dataclass(frozen=True)
    class Project(FakeProject):
        def deploy(self, **kwargs: Any) -> FakeDeployment:
            failure = RuntimeError("boom")
            failure.add_note(f"keep_on_failure=True: el sandbox {SANDBOX_ID} sigue vivo")
            raise failure

    install_project(monkeypatch, Project())
    result = runner.invoke(app, ["agent", "deploy", "--keep-on-failure"], obj=clients)
    assert result.exit_code != 0
    assert SANDBOX_ID in result.output


@pytest.mark.usefixtures("access_token")
def test_deploy_warns_when_the_app_routes_are_not_exposed(
    monkeypatch: pytest.MonkeyPatch, runner: CliRunner, clients: Clients
) -> None:
    hidden = CustomRuntime(
        app=HttpApp(command=["python", "serve.py"], port=9100, health_path="/health")
    )
    exposed = CustomRuntime(
        app=HttpApp(
            command=["python", "serve.py"], port=9100, health_path="/health", protocol="raw"
        )
    )
    install_project(monkeypatch, FakeProject(runtime=hidden))  # type: ignore[arg-type]
    warned = runner.invoke(app, ["agent", "deploy"], obj=clients)
    assert 'protocol = "raw"' in warned.output
    assert "/health" in warned.output
    install_project(monkeypatch, FakeProject(runtime=exposed))  # type: ignore[arg-type]
    quiet = runner.invoke(app, ["agent", "deploy"], obj=clients)
    assert "aviso" not in quiet.output


def test_init_custom_explains_the_raw_protocol(tmp_path: Path, runner: CliRunner) -> None:
    result = runner.invoke(app, ["agent", "init", str(tmp_path), "--runtime", "custom"])
    assert result.exit_code == 0, result.output
    assert 'protocol = "raw"' in result.output
    project_file = (tmp_path / "rayito-agent.toml").read_text(encoding="utf-8")
    for key in (
        'requirements = "requirements.txt"',
        "install = []",
        "[runtime.app]",
        "port = 9100",
    ):
        assert key in project_file


def test_init_custom_scaffolds_a_project_that_loads(tmp_path: Path, runner: CliRunner) -> None:
    runner.invoke(app, ["agent", "init", str(tmp_path), "--runtime", "custom"])
    loaded = AgentProject.load(tmp_path)
    runtime = loaded.runtime
    assert isinstance(runtime, CustomRuntime)
    assert runtime.app is not None
    assert runtime.app.protocol == "raw"
    assert runtime.requirements == "requirements.txt"
    assert not runtime.app.hides_own_routes


def test_init_langgraph_scaffolds_a_plain_graph(tmp_path: Path, runner: CliRunner) -> None:
    runner.invoke(app, ["agent", "init", str(tmp_path), "--runtime", "langgraph"])
    graph = (tmp_path / "graph.py").read_text(encoding="utf-8")
    assert "create_react_agent" in graph
    assert "deepagents" not in graph


def test_export_makes_no_aws_call(
    monkeypatch: pytest.MonkeyPatch, runner: CliRunner, tmp_path: Path
) -> None:
    project = install_project(monkeypatch, FakeProject())
    result = runner.invoke(
        app, ["--json", "agent", "export", "--target", "agentcore", "--out", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["port"] == 8080
    _, kwargs = project.log[-1]
    assert kwargs == {"target": "agentcore", "out_dir": str(tmp_path), "overwrite": False}


def test_export_rejects_an_unknown_target(runner: CliRunner, tmp_path: Path) -> None:
    result = runner.invoke(app, ["agent", "export", "--target", "ecs", "--out", str(tmp_path)])
    assert result.exit_code == 2


@pytest.mark.usefixtures("access_token")
def test_invoke_stream_and_plain(
    monkeypatch: pytest.MonkeyPatch, runner: CliRunner, clients: Clients
) -> None:
    deployment = FakeDeployment(events=[TextDelta("hi"), Done("ses_1", 0, USAGE)])
    connected: list[dict[str, Any]] = []
    install_deployment(monkeypatch, deployment, connected)
    plain = runner.invoke(
        app, ["agent", "invoke", SANDBOX_ID, "hola", "--session-id", "s"], obj=clients
    )
    streamed = runner.invoke(app, ["agent", "invoke", SANDBOX_ID, "hola", "--stream"], obj=clients)
    assert plain.exit_code == 0 and "hola" in plain.output
    assert streamed.stdout == "hi\n"
    assert deployment.calls == [("invoke", ("hola", "s")), ("stream", ("hola", None))]
    assert [call["access_token"] for call in connected] == [ACCESS_TOKEN, ACCESS_TOKEN]


@pytest.mark.usefixtures("no_access_token")
def test_invoke_and_status_read_the_token_file(
    monkeypatch: pytest.MonkeyPatch, runner: CliRunner, clients: Clients, tmp_path: Path
) -> None:
    connected: list[dict[str, Any]] = []
    install_deployment(monkeypatch, FakeDeployment(), connected)
    token_file = tmp_path / "token"
    token_file.write_text(ACCESS_TOKEN + "\n")
    invoke = runner.invoke(
        app, ["agent", "invoke", SANDBOX_ID, "hola", "--token-file", str(token_file)], obj=clients
    )
    status = runner.invoke(
        app, ["agent", "status", SANDBOX_ID, "--token-file", str(token_file)], obj=clients
    )
    assert invoke.exit_code == 0 and status.exit_code == 0
    assert [call["access_token"] for call in connected] == [ACCESS_TOKEN, ACCESS_TOKEN]


@pytest.mark.usefixtures("no_access_token")
@pytest.mark.parametrize("command", [["invoke", SANDBOX_ID, "hola"], ["status", SANDBOX_ID]])
def test_invoke_and_status_without_a_token_are_usage_errors(
    monkeypatch: pytest.MonkeyPatch, runner: CliRunner, clients: Clients, command: list[str]
) -> None:
    connected: list[dict[str, Any]] = []
    install_deployment(monkeypatch, FakeDeployment(), connected)
    result = runner.invoke(app, ["agent", *command], obj=clients)
    assert result.exit_code == 2
    assert "--token-file" in result.output
    assert connected == []


@pytest.mark.usefixtures("access_token")
def test_status(monkeypatch: pytest.MonkeyPatch, runner: CliRunner, clients: Clients) -> None:
    install_deployment(monkeypatch, FakeDeployment())
    status = runner.invoke(app, ["--json", "agent", "status", SANDBOX_ID], obj=clients)
    assert json.loads(status.output)["ready"] is True


@pytest.mark.usefixtures("no_access_token")
@pytest.mark.parametrize("found", [True, False])
def test_stop_needs_no_token_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch, runner: CliRunner, clients: Clients, found: bool
) -> None:
    killed: list[str] = []

    def kill(_clients: Clients, sandbox_id: str) -> bool:
        killed.append(sandbox_id)
        return found

    monkeypatch.setattr(agent_cli, "kill_one", kill)
    stop = runner.invoke(app, ["--json", "agent", "stop", SANDBOX_ID], obj=clients)
    assert stop.exit_code == 0, stop.output
    assert json.loads(stop.output) == {"sandboxId": SANDBOX_ID, "stopped": found}
    assert killed == [SANDBOX_ID]
