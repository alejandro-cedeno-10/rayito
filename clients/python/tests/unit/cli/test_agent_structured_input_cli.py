"""Las banderas de la entrada estructurada en `rayito agent` (ADR-033 D3):
`--graph` en `detect`, y `--input` / `--input-file` en `invoke`, con sus
errores de uso antes de cualquier llamada y la salida impresa como JSON. Los
textos de ayuda son los de `testdata/agnostic-deploy/contract.json`."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from rayito._agent._events import Done, TokenUsage
from rayito.cli import agent as agent_cli
from rayito.cli._session import Clients
from rayito.cli.app import app
from rayito.exceptions import InvalidArgumentException
from tests.unit.cli.test_agent_project_cli import ACCESS_TOKEN, SANDBOX_ID, TOKEN_ENV

TESTDATA = Path(__file__).resolve().parents[5] / "testdata"
SAMPLE = TESTDATA / "agent-structured-samples" / "python" / "langgraph-structured"
CONTRACT = json.loads((TESTDATA / "agnostic-deploy" / "contract.json").read_text("utf-8"))
TICKET = {"ticket_id": "T-1"}


def test_the_cli_help_texts_are_the_contract_texts() -> None:
    helps = {flag["name"]: flag["help"] for flag in CONTRACT["cliFlags"] if flag["feature"] == "B"}
    assert helps == {
        "graph": agent_cli.GRAPH_HELP,
        "prompt": agent_cli.INVOKE_PROMPT_HELP,
        "input_json": agent_cli.INPUT_HELP,
        "input_file": agent_cli.INPUT_FILE_HELP,
    }


@dataclass
class InputResult:
    session_id: str = "ses_1"
    text: str = ""
    steps: int = 1
    usage: TokenUsage = field(default_factory=TokenUsage)
    exit_code: int = 0
    usage_reported: bool = False
    output: dict[str, Any] | None = field(default_factory=lambda: {"priority": "high"})


@dataclass
class InputDeployment:
    calls: list[tuple[str, Any, dict[str, Any]]] = field(default_factory=list)
    error: Exception | None = None

    def invoke(self, prompt: str | None = None, **options: Any) -> InputResult:
        self.calls.append(("invoke", prompt, options))
        if self.error is not None:
            raise self.error
        return InputResult()

    def stream(self, prompt: str | None = None, **options: Any) -> Any:
        self.calls.append(("stream", prompt, options))
        done = Done("ses_1", 0, TokenUsage(), output={"priority": "high"})

        class Events:
            def __enter__(self) -> list[Any]:
                return [done]

            def __exit__(self, *exc: object) -> None:
                return None

        return Events()


@pytest.fixture
def deployment(monkeypatch: pytest.MonkeyPatch) -> InputDeployment:
    fake = InputDeployment()
    monkeypatch.setenv(TOKEN_ENV, ACCESS_TOKEN)
    monkeypatch.setattr(
        agent_cli,
        "AgentDeployment",
        type("FakeAgentDeployment", (), {"connect": staticmethod(lambda *a, **k: fake)}),
    )
    return fake


def test_invoke_sends_input_and_prints_the_output(
    deployment: InputDeployment, runner: CliRunner, clients: Clients
) -> None:
    result = runner.invoke(
        app, ["agent", "invoke", SANDBOX_ID, "--input", json.dumps(TICKET)], obj=clients
    )
    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"priority": "high"}
    assert deployment.calls == [("invoke", None, {"session_id": None, "input": TICKET})]
    as_json = runner.invoke(
        app, ["--json", "agent", "invoke", SANDBOX_ID, "--input", json.dumps(TICKET)], obj=clients
    )
    assert json.loads(as_json.stdout)["output"] == {"priority": "high"}


def test_invoke_reads_the_input_from_a_file_or_stdin(
    deployment: InputDeployment, runner: CliRunner, clients: Clients, tmp_path: Path
) -> None:
    source = tmp_path / "ticket.json"
    source.write_text(json.dumps(TICKET), encoding="utf-8")
    from_file = runner.invoke(
        app, ["agent", "invoke", SANDBOX_ID, "--input-file", str(source)], obj=clients
    )
    from_stdin = runner.invoke(
        app,
        ["agent", "invoke", SANDBOX_ID, "--input-file", "-", "--stream"],
        obj=clients,
        input=json.dumps(TICKET),
    )
    assert from_file.exit_code == 0 and from_stdin.exit_code == 0
    assert [call[2]["input"] for call in deployment.calls] == [TICKET, TICKET]
    assert json.loads(from_stdin.stdout) == {"priority": "high"}


@pytest.mark.parametrize(
    "extra",
    [
        [],
        ["hola", "--input", "{}"],
        ["--input", "{}", "--input-file", "-"],
        ["--input", "[1]"],
        ["--input", "no-es-json"],
        ["--input-file", "no-existe.json"],
    ],
)
def test_invoke_input_errors_are_usage_errors_before_any_call(
    deployment: InputDeployment, runner: CliRunner, clients: Clients, extra: list[str]
) -> None:
    result = runner.invoke(app, ["agent", "invoke", SANDBOX_ID, *extra], obj=clients)
    assert result.exit_code == 2
    assert deployment.calls == []
    assert "no-es-json" not in result.output


@pytest.mark.parametrize(
    ("extra", "exit_code"),
    [(["hola"], 1), (["--input", json.dumps(TICKET)], 2)],
    ids=["prompt-keeps-its-exit-code", "input-is-a-usage-error"],
)
def test_an_invalid_argument_from_the_sdk_keeps_the_prompt_exit_code(
    deployment: InputDeployment,
    runner: CliRunner,
    clients: Clients,
    extra: list[str],
    exit_code: int,
) -> None:
    deployment.error = InvalidArgumentException("el agente desplegado rechazó la petición")
    result = runner.invoke(app, ["agent", "invoke", SANDBOX_ID, *extra], obj=clients)
    assert result.exit_code == exit_code
    assert "rechazó la petición" in result.output


def test_detect_passes_the_graph(runner: CliRunner, clients: Clients) -> None:
    result = runner.invoke(
        app,
        ["--json", "agent", "detect", str(SAMPLE), "--graph", "triage"],
        obj=clients,
    )
    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    assert document["project"]["runtime"]["graph"] == "triage"
    assert document["detection"]["options"] == {"graph": "triage"}
