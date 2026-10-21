"""El protocolo `langgraph-api` del driver `http-app` (ADR-033 D5): el host
traduce cada invocación a `POST /threads/{thread_id}/runs/wait` del
LangGraph Platform API, con el hilo derivado de la sesión, y lee el estado
final del grafo. Las pruebas usan los vectores de
`testdata/agent-langgraph-api/` y un Agent Server falso en el loopback (una
app de la biblioteca estándar que imita `/ok` y la ejecución de un hilo);
nunca `langgraph dev`. El host se prueba una vez, aquí
(`parity-checklist.md`)."""

from __future__ import annotations

import json
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from rayito._agent._runner.agent_host.drivers import http_app
from rayito._agent._runner.agent_host.protocol import (
    AgentFailure,
    Request,
    RunState,
    require_input_support,
)
from rayito._limits import (
    AGENT_LANGGRAPH_HEALTH_PATH,
    AGENT_LANGGRAPH_INVOKE_PATH,
    AGENT_LANGGRAPH_READY_WAIT_SECONDS,
    AGENT_LANGGRAPH_THREAD_NAMESPACE,
)
from tests.unit.agent_host_support import APP_START_TIMEOUT_SECONDS, free_port

TESTDATA = Path(__file__).resolve().parents[4] / "testdata" / "agent-langgraph-api"
REQUESTS = json.loads((TESTDATA / "requests.json").read_text("utf-8"))
RESPONSES = json.loads((TESTDATA / "responses.json").read_text("utf-8"))
#: El prompt con el que el servidor falso devuelve lo que recibió en vez de
#: un caso de `responses.json`.
ECHO_PROMPT = "__echo__"

FAKE_AGENT_SERVER = """
import json, os, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CASES = json.load(open(sys.argv[1], encoding="utf-8"))["cases"]
ENV_NAMES = ("LANGGRAPH_CLI_NO_ANALYTICS", "LANGSMITH_TRACING")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/ok":
            self._send(200, {"ok": True})
        else:
            self._send(404, {"detail": "Not Found"})

    def do_POST(self):
        size = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(size) or b"{}")
        messages = body["input"].get("messages")
        prompt = messages[-1]["content"] if messages else "__echo__"
        if prompt == "__echo__":
            seen = {
                "path": self.path,
                "body": body,
                "headers": sorted(name.lower() for name in self.headers),
                "env": {name: os.environ.get(name) for name in ENV_NAMES},
            }
            self._send(200, {"messages": [{"type": "ai", "content": json.dumps(seen)}]})
            return
        case = next(item for item in CASES if item["name"] == prompt)
        self._send(case["status"], case["response"])


ThreadingHTTPServer(("127.0.0.1", int(os.environ["APP_PORT"])), Handler).serve_forever()
"""


def _driver(tmp_path: Path, **extra: Any) -> http_app.Driver:
    """El driver `langgraph-api` sobre el Agent Server falso, sin
    `health_path` ni `invoke_path`: usa los del protocolo."""
    script = tmp_path / "fake_agent_server.py"
    script.write_text(textwrap.dedent(FAKE_AGENT_SERVER), encoding="utf-8")
    return http_app.Driver(
        {
            "command": [sys.executable, str(script), str(TESTDATA / "responses.json")],
            "port": free_port(),
            "port_env": "APP_PORT",
            "protocol": "langgraph-api",
            "assistant_id": "agent",
            "workdir": str(tmp_path),
            "start_timeout_seconds": APP_START_TIMEOUT_SECONDS,
            **extra,
        }
    )


@pytest.fixture(scope="module")
def driver(tmp_path_factory: pytest.TempPathFactory) -> Iterator[http_app.Driver]:
    started = _driver(tmp_path_factory.mktemp("langgraph"))
    yield started
    started.cancel()


def _run(driver: http_app.Driver, prompt: str, session_id: str | None = None) -> list[Any]:
    events: list[dict[str, Any]] = []
    driver.run(Request(prompt=prompt, session_id=session_id), events.append, RunState())
    return events


def _echo(driver: http_app.Driver, session_id: str | None = None) -> tuple[str, dict[str, Any]]:
    events = _run(driver, ECHO_PROMPT, session_id)
    return events[0]["session_id"], json.loads(events[2]["text"])


def test_the_host_constants_match_the_shared_limits() -> None:
    assert http_app.LANGGRAPH_HEALTH_PATH == AGENT_LANGGRAPH_HEALTH_PATH
    assert http_app.LANGGRAPH_INVOKE_PATH == AGENT_LANGGRAPH_INVOKE_PATH
    assert str(http_app.LANGGRAPH_THREAD_NAMESPACE) == AGENT_LANGGRAPH_THREAD_NAMESPACE
    assert str(http_app.LANGGRAPH_THREAD_NAMESPACE) == REQUESTS["threadNamespace"]
    assert http_app.LANGGRAPH_READY_WAIT_SECONDS == AGENT_LANGGRAPH_READY_WAIT_SECONDS


@pytest.mark.parametrize("case", REQUESTS["threadCases"], ids=lambda case: case["sessionId"])
def test_the_thread_is_derived_from_the_session(case: dict[str, str]) -> None:
    assert http_app.langgraph_thread_id(case["sessionId"]) == case["threadId"]


@pytest.mark.parametrize(
    "case",
    [case for case in REQUESTS["bodyCases"] if "prompt" in case["request"]],
    ids=lambda case: case["name"],
)
def test_a_prompt_becomes_the_body_and_path_of_the_vector(case: dict[str, Any]) -> None:
    session_id = case["request"]["sessionId"]
    path = http_app.langgraph_invoke_path(http_app.LANGGRAPH_INVOKE_PATH, session_id)
    graph_input = http_app.langgraph_prompt_input(case["request"]["prompt"])
    assert path == case["path"]
    assert http_app.langgraph_body(case["assistantId"], graph_input) == case["body"]


@pytest.mark.parametrize(
    "case",
    [case for case in REQUESTS["bodyCases"] if "input" in case["request"]],
    ids=lambda case: case["name"],
)
def test_a_structured_input_becomes_the_body_and_path_of_the_vector(
    case: dict[str, Any],
) -> None:
    session_id = case["request"]["sessionId"]
    request = Request(prompt="", session_id=session_id, input=case["request"]["input"])
    path = http_app.langgraph_invoke_path(http_app.LANGGRAPH_INVOKE_PATH, session_id)
    assert path == case["path"]
    assert (
        http_app.langgraph_body(case["assistantId"], http_app.langgraph_input(request))
        == (case["body"])
    )


@pytest.mark.parametrize(
    "case",
    [case for case in REQUESTS["bodyCases"] if "prompt" in case["request"]],
    ids=lambda case: case["name"],
)
def test_without_input_the_prompt_is_the_graph_input(case: dict[str, Any]) -> None:
    request = Request(prompt=case["request"]["prompt"], session_id=case["request"]["sessionId"])
    assert http_app.langgraph_input(request) == case["body"]["input"]


def test_only_langgraph_api_accepts_a_structured_input() -> None:
    langgraph = http_app.Driver(
        {"command": ["x"], "port": 1, "protocol": "langgraph-api", "assistant_id": "agent"}
    )
    assert langgraph.accepts_input is True
    for protocol in ("agentcore", "json", "openai-chat", "raw"):
        other = http_app.Driver({"command": ["x"], "port": 1, "protocol": protocol})
        assert other.accepts_input is False
        with pytest.raises(AgentFailure) as error:
            require_input_support(other, Request(prompt="", input={"ticket_id": "T-1"}))
        assert (error.value.reason, error.value.detail_code) == ("protocol_error", "invalid_input")
    require_input_support(langgraph, Request(prompt="", input={"ticket_id": "T-1"}))


def test_a_structured_input_reaches_the_graph_as_is(tmp_path: Path) -> None:
    case = next(case for case in REQUESTS["bodyCases"] if "input" in case["request"])
    target = _driver(tmp_path, assistant_id=case["assistantId"])
    events: list[dict[str, Any]] = []
    request = Request(
        prompt="", session_id=case["request"]["sessionId"], input=case["request"]["input"]
    )
    try:
        target.run(request, events.append, RunState())
    finally:
        target.cancel()
    seen = json.loads(events[2]["text"])
    assert seen["path"] == case["path"]
    assert seen["body"] == case["body"]


@pytest.mark.parametrize("case", RESPONSES["cases"], ids=lambda case: case["name"])
def test_the_final_state_is_read_as_the_vector_says(
    case: dict[str, Any], tmp_path: Path, driver: http_app.Driver
) -> None:
    field = case.get("responseField")
    target = _driver(tmp_path, response_field=field) if field else driver
    expected = case["expected"]
    try:
        if "failure" in expected:
            with pytest.raises(AgentFailure) as error:
                _run(target, case["name"])
            failure = expected["failure"]
            assert (error.value.reason, error.value.detail_code) == (
                failure["reason"],
                failure["detail_code"],
            )
            return
        events = _run(target, case["name"])
    finally:
        if target is not driver:
            target.cancel()
    text = events[2]["text"]
    if expected.get("textIsJson"):
        assert json.loads(text) == case["response"]
    else:
        assert text == expected["text"]
    assert events[3]["usage"] == http_app.app_usage(expected["usage"])


def test_the_same_session_runs_on_the_same_thread(driver: http_app.Driver) -> None:
    session_id, first = _echo(driver)
    again, second = _echo(driver, session_id)
    assert session_id.startswith(http_app.SESSION_ID_PREFIX)
    assert again == session_id
    thread = http_app.langgraph_thread_id(session_id)
    assert first["path"] == second["path"] == f"/threads/{thread}/runs/wait"


def test_the_body_carries_the_assistant_and_the_run_options(driver: http_app.Driver) -> None:
    session_id = REQUESTS["threadCases"][0]["sessionId"]
    _, seen = _echo(driver, session_id)
    assert seen["path"] == f"/threads/{REQUESTS['threadCases'][0]['threadId']}/runs/wait"
    assert seen["body"] == {
        "assistant_id": "agent",
        "input": {"messages": [{"role": "user", "content": ECHO_PROMPT}]},
        "if_not_exists": "create",
        "multitask_strategy": "reject",
    }
    assert http_app.SESSION_HEADER.lower() not in seen["headers"]


def test_the_server_starts_without_cli_telemetry_or_langsmith_traces(
    driver: http_app.Driver,
) -> None:
    _, seen = _echo(driver)
    assert seen["env"] == {"LANGGRAPH_CLI_NO_ANALYTICS": "1", "LANGSMITH_TRACING": "false"}


def test_tracing_that_the_project_sets_is_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    target = _driver(tmp_path)
    try:
        _, seen = _echo(target)
    finally:
        target.cancel()
    assert seen["env"]["LANGSMITH_TRACING"] == "true"
    assert seen["env"]["LANGGRAPH_CLI_NO_ANALYTICS"] == "1"


def test_an_app_that_is_never_healthy_on_ok_fails_to_start(tmp_path: Path) -> None:
    target = _driver(tmp_path, health_path="/ping", start_timeout_seconds=1)
    try:
        with pytest.raises(AgentFailure) as error:
            target.warm()
    finally:
        target.cancel()
    assert error.value.detail_code == "app_not_ready"


def test_the_assistant_is_required() -> None:
    with pytest.raises(AgentFailure) as error:
        http_app.Driver({"command": ["x"], "port": 1, "protocol": "langgraph-api"})
    assert (error.value.reason, error.value.detail_code) == ("protocol_error", "invalid_protocol")


@pytest.mark.parametrize(
    ("state", "message"),
    [
        ({"messages": "x"}, None),
        ({"messages": [{"content": {"a": 1}}]}, {"content": {"a": 1}}),
        ({"messages": ["texto"]}, None),
        ([1, 2], None),
    ],
)
def test_odd_states_never_break_the_reading(state: object, message: object) -> None:
    assert http_app.langgraph_last_message(state) == message
    if message is not None:
        assert http_app.langgraph_text(state) == ""
    assert http_app.langgraph_usage(state) is None
