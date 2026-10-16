"""Los drivers `opencode` y `http-app` del host del agente
(`_runner/agent_host/drivers/`) y el arranque real del host como proceso
(`python agent_host task|serve`). El driver de OpenCode lee los vectores
compartidos que ya usan los dos SDK; el de `http-app` habla con una app
HTTP de verdad (un proceso hijo con `http.server`)."""

from __future__ import annotations

import json
import os
import shlex
import signal
import socket
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from rayito._agent import _runner
from rayito._agent._runner.agent_host import __main__ as host_main
from rayito._agent._runner.agent_host.drivers import http_app, opencode
from rayito._agent._runner.agent_host.protocol import (
    AgentFailure,
    Request,
    RunState,
)
from rayito._limits import MAX_AGENT_EVENT_LINE_BYTES, OPENCODE_SESSION_TITLE
from tests.unit.agent_host_support import (
    invoke,
    request,
    running_service,
    sse_messages,
    wait_for,
)

TESTDATA = Path(__file__).resolve().parents[4] / "testdata" / "agent"
HOST_DIR = Path(_runner.__file__).parent / "agent_host"
OPENCODE_EVENTS = TESTDATA / "opencode-v1.18.34-events.jsonl"


def _strip_version(event: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in event.items() if key != "v"}


def _collect(driver: Any, request_: Request) -> tuple[list[dict[str, Any]], RunState]:
    events: list[dict[str, Any]] = []
    state = RunState()
    driver.run(request_, events.append, state)
    return events, state


def test_opencode_constants_match_limits_json() -> None:
    assert opencode.SESSION_TITLE == OPENCODE_SESSION_TITLE
    assert opencode.MAX_LINE_BYTES == MAX_AGENT_EVENT_LINE_BYTES


def test_opencode_translation_matches_the_shared_vectors() -> None:
    expected = json.loads((TESTDATA / "expected-events.json").read_text(encoding="utf-8"))
    translation = opencode.Translation()
    events: list[dict[str, Any]] = []
    for line in OPENCODE_EVENTS.read_bytes().splitlines():
        events.extend(opencode.translate_line(line, translation))
    session, *progress = events
    assert _strip_version(session) == {"type": "session", "session_id": expected["session_id"]}
    assert [_strip_version(event) for event in progress] == expected["events"]
    assert translation.ignored_lines == expected["ignored_lines"]
    assert translation.failure is None


@pytest.mark.parametrize(
    ("case", "kwargs"),
    [
        ("default", {}),
        (
            "resume",
            {
                "session_id": "ses_0000000000000000000000000a",
                "model": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
                "reasoning": True,
            },
        ),
    ],
)
def test_opencode_arguments_match_the_golden_commands(
    case: str, kwargs: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    golden = json.loads((TESTDATA / "opencode-run-commands.json").read_text(encoding="utf-8"))
    exec_line = golden[case]["script"].splitlines()[-1]
    expected = shlex.split(exec_line)[1:]
    workdir = "/home/user" if case == "default" else "/home/user/mi proyecto's"
    config_file = tmp_path / "opencode.json"
    config_file.write_text(json.dumps({"model": "amazon-bedrock/algun-modelo"}))
    monkeypatch.setenv(opencode.CONFIG_ENV, str(config_file))
    args = opencode.build_args({"workdir": workdir}, Request(prompt="hola", **kwargs))
    assert args == expected
    assert "hola" not in args


def test_opencode_rejects_a_session_id_that_is_not_opencodes() -> None:
    with pytest.raises(AgentFailure) as raised:
        opencode.build_args({"workdir": "/w"}, Request(prompt="x", session_id='a"b'))
    assert (raised.value.reason, raised.value.detail_code) == (
        "protocol_error",
        "invalid_session_id",
    )


def _fake_opencode(tmp_path: Path, body: str) -> str:
    script = tmp_path / "opencode"
    script.write_text(f"#!/bin/sh\n{body}\n")
    script.chmod(0o755)
    return str(script)


def test_opencode_runs_the_binary_and_streams_events(tmp_path: Path) -> None:
    stdin_copy = tmp_path / "stdin"
    argv_copy = tmp_path / "argv"
    binary = _fake_opencode(
        tmp_path,
        f'echo "$@" > {argv_copy}\ncat > {stdin_copy}\ncat {OPENCODE_EVENTS}',
    )
    driver = opencode.create({"binary": binary, "workdir": str(tmp_path)})
    events, state = _collect(driver, Request(prompt="hola ñ"))
    kinds = [event["type"] for event in events]
    assert kinds[0] == "session"
    assert kinds.count("step_started") == 2
    assert state.session_id == "ses_0000000000000000000000000a"
    assert stdin_copy.read_text(encoding="utf-8") == "hola ñ"
    assert "hola" not in argv_copy.read_text()


def test_opencode_error_line_is_a_model_error(tmp_path: Path) -> None:
    line = json.dumps({"type": "error", "sessionID": "ses_x", "error": {"name": "APIError"}})
    binary = _fake_opencode(tmp_path, f"cat > /dev/null\necho '{line}'")
    with pytest.raises(AgentFailure) as raised:
        _collect(opencode.create({"binary": binary, "workdir": str(tmp_path)}), Request(prompt="x"))
    assert (raised.value.reason, raised.value.detail_code) == ("model_error", "APIError")


def test_opencode_exit_code_and_missing_session_fail(tmp_path: Path) -> None:
    failing = _fake_opencode(tmp_path, "cat > /dev/null\nexit 3")
    with pytest.raises(AgentFailure) as raised:
        _collect(
            opencode.create({"binary": failing, "workdir": str(tmp_path)}), Request(prompt="x")
        )
    assert raised.value.reason == "runtime_error"
    silent = tmp_path / "silent"
    silent.mkdir()
    quiet = _fake_opencode(silent, "cat > /dev/null")
    with pytest.raises(AgentFailure) as raised:
        _collect(opencode.create({"binary": quiet, "workdir": str(tmp_path)}), Request(prompt="x"))
    assert raised.value.reason == "protocol_error"


def test_opencode_drops_an_oversized_line(tmp_path: Path) -> None:
    big = {"type": "text", "sessionID": "ses_a", "part": {"text": "x" * opencode.MAX_LINE_BYTES}}
    oversized = tmp_path / "oversized.jsonl"
    oversized.write_text(json.dumps(big) + "\n")
    binary = _fake_opencode(tmp_path, f"cat > /dev/null\ncat {oversized} {OPENCODE_EVENTS}")
    events, _ = _collect(
        opencode.create({"binary": binary, "workdir": str(tmp_path)}), Request(prompt="x")
    )
    assert all(len(json.dumps(event)) < opencode.MAX_LINE_BYTES for event in events)
    assert [e["type"] for e in events].count("step_started") == 2


def test_opencode_cancel_kills_the_child(tmp_path: Path) -> None:
    binary = _fake_opencode(tmp_path, "cat > /dev/null\nsleep 60")
    driver = opencode.create({"binary": binary, "workdir": str(tmp_path)})
    outcome: list[BaseException] = []

    def run() -> None:
        try:
            _collect(driver, Request(prompt="x"))
        except AgentFailure as exc:
            outcome.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    time.sleep(0.3)
    driver.cancel()
    thread.join(10)
    assert not thread.is_alive()
    assert outcome


APP_SOURCE = textwrap.dedent(
    """
    import json, sys
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def log_message(self, *args): pass
        def _body(self):
            return self.rfile.read(int(self.headers.get("Content-Length") or 0))
        def _json(self, status, payload):
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        def _sse(self, chunks):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            for chunk in chunks:
                self.wfile.write(("data: " + chunk + "\\n\\n").encode())
                self.wfile.flush()
            self.close_connection = True
        def do_GET(self):
            if self.path == "/ping":
                self._json(200, {"status": "Healthy"})
            elif self.path == "/raw/echo":
                self._json(200, {"path": self.path, "method": "GET"})
            else:
                self._json(404, {})
        def do_POST(self):
            body = json.loads(self._body() or b"{}")
            if self.path == "/invocations":
                prompt = body.get("prompt", "")
                if prompt == "stream":
                    self._sse(["Hola ", "mundo"])
                elif prompt == "fail":
                    self._json(500, {"error": "boom"})
                else:
                    self._json(200, {"response": "eco:" + prompt,
                                     "session": self.headers.get(
                                         "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id")})
            elif self.path == "/v1/chat/completions":
                content = body["messages"][0]["content"]
                chunks = [json.dumps({"choices": [{"delta": {"content": part}}]})
                          for part in ("uno ", content)]
                chunks.append(json.dumps({"choices": [], "usage":
                    {"prompt_tokens": 11, "completion_tokens": 5}}))
                chunks.append("[DONE]")
                self._sse(chunks)
            elif self.path == "/raw/echo":
                self._json(200, {"echo": body})
            else:
                self._json(404, {})

    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
    """
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def app_options(tmp_path: Path) -> dict[str, Any]:
    script = tmp_path / "app.py"
    script.write_text(APP_SOURCE)
    port = _free_port()
    return {
        "command": [sys.executable, str(script), str(port)],
        "port": port,
        "protocol": "agentcore",
        "workdir": str(tmp_path),
        "start_timeout_seconds": 20,
    }


@pytest.fixture
def app_driver(app_options: dict[str, Any]) -> Any:
    drivers: list[Any] = []

    def make(**overrides: Any) -> Any:
        driver = http_app.create({**app_options, **overrides})
        drivers.append(driver)
        return driver

    yield make
    for driver in drivers:
        driver.cancel()


def test_http_app_agentcore_json_reply_becomes_v1_events(app_driver: Any) -> None:
    events, state = _collect(app_driver(), Request(prompt="hola", session_id="app_" + "1" * 32))
    assert state.session_id == "app_" + "1" * 32
    assert [e["type"] for e in events] == ["session", "step_started", "text", "step_finished"]
    assert events[2]["text"] == "eco:hola"


def test_http_app_agentcore_sse_reply_streams_deltas(app_driver: Any) -> None:
    events, _ = _collect(app_driver(), Request(prompt="stream"))
    deltas = [e["text"] for e in events if e["type"] == "text_delta"]
    assert deltas == ["Hola ", "mundo"]
    assert next(e for e in events if e["type"] == "text")["text"] == "Hola mundo"


def test_http_app_openai_chat_translates_deltas_and_usage(app_driver: Any) -> None:
    driver = app_driver(protocol="openai-chat", invoke_path="/v1/chat/completions")
    events, _ = _collect(driver, Request(prompt="dos", model="m"))
    assert [e["text"] for e in events if e["type"] == "text_delta"] == ["uno ", "dos"]
    finished = next(e for e in events if e["type"] == "step_finished")
    assert finished["usage"]["input"] == 11
    assert finished["usage"]["output"] == 5


def test_http_app_error_status_is_a_runtime_error(app_driver: Any) -> None:
    with pytest.raises(AgentFailure) as raised:
        _collect(app_driver(), Request(prompt="fail"))
    assert (raised.value.reason, raised.value.detail_code) == ("runtime_error", "app_http_500")


def test_http_app_that_never_gets_healthy_fails(tmp_path: Path) -> None:
    driver = http_app.create(
        {
            "command": [sys.executable, "-c", "import time; time.sleep(30)"],
            "port": _free_port(),
            "start_timeout_seconds": 0.5,
        }
    )
    try:
        with pytest.raises(AgentFailure) as raised:
            driver.warm()
        assert raised.value.detail_code == "app_not_ready"
    finally:
        driver.cancel()


def test_http_app_that_exits_fails_fast() -> None:
    driver = http_app.create(
        {
            "command": [sys.executable, "-c", "pass"],
            "port": _free_port(),
            "start_timeout_seconds": 20,
        }
    )
    with pytest.raises(AgentFailure) as raised:
        driver.warm()
    assert raised.value.detail_code == "app_exited"


def test_http_app_raw_cannot_be_invoked_but_is_forwarded(app_driver: Any) -> None:
    driver = app_driver(protocol="raw")
    with pytest.raises(AgentFailure) as raised:
        _collect(driver, Request(prompt="x"))
    assert raised.value.detail_code == "raw_app"
    with running_service(driver) as (_, port):
        driver.warm()
        status, _, data = request(port, "GET", "/raw/echo")
        assert (status, json.loads(data)["path"]) == (200, "/raw/echo")
        status, _, data = request(port, "POST", "/raw/echo", {"a": 1})
        assert json.loads(data) == {"echo": {"a": 1}}
        assert request(port, "GET", "/ping")[0] == 200


def test_http_app_rejects_an_unknown_protocol() -> None:
    with pytest.raises(AgentFailure):
        http_app.create({"command": ["x"], "port": 1, "protocol": "grpc"})


def test_the_host_serves_an_http_app_through_the_service_contract(app_driver: Any) -> None:
    driver = app_driver()
    driver.warm()
    with running_service(driver) as (_, port):
        status, _, body = invoke(port, "hola")
        assert status == 200
        assert body["text"] == "eco:hola"
        assert body["session_id"].startswith("app_")
        _, _, data = request(
            port, "POST", "/invocations", {"prompt": "stream"}, {"Accept": "text/event-stream"}
        )
        assert sse_messages(data)[-1][0] == "done"


def _write_host_config(tmp_path: Path, driver: str, options: dict[str, Any], **extra: Any) -> Path:
    path = tmp_path / "host.json"
    path.write_text(json.dumps({"v": 1, "driver": driver, "options": options, **extra}))
    return path


def test_host_task_mode_as_a_process_keeps_stdout_for_the_protocol(tmp_path: Path) -> None:
    binary = _fake_opencode(
        tmp_path, f"cat > /dev/null\necho 'ruido en stdout'\ncat {OPENCODE_EVENTS}"
    )
    opencode_json = tmp_path / "opencode.json"
    opencode_json.write_text(json.dumps({"model": "amazon-bedrock/m"}))
    config = _write_host_config(tmp_path, "opencode", {"binary": binary, "workdir": str(tmp_path)})
    result = subprocess.run(
        [sys.executable, str(HOST_DIR), "task", str(config)],
        input=json.dumps({"v": 1, "prompt": "hola"}).encode(),
        capture_output=True,
        check=True,
        env={**os.environ, "OPENCODE_CONFIG": str(opencode_json)},
    )
    lines = [json.loads(line) for line in result.stdout.decode().splitlines()]
    assert lines[0]["type"] == "session"
    assert lines[-1] == {"v": 1, "type": "done", "session_id": "ses_0000000000000000000000000a"}
    assert all(line["v"] == 1 for line in lines)


def test_host_task_mode_reports_unreadable_input_as_one_line(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(HOST_DIR), "task", str(tmp_path / "no-existe.json")],
        input=b"{}",
        capture_output=True,
        check=True,
    )
    (line,) = result.stdout.decode().splitlines()
    assert json.loads(line) == {
        "v": 1,
        "type": "agent_failed",
        "reason": "protocol_error",
        "detail_code": "invalid_input",
    }


def test_host_task_mode_with_an_uninstalled_driver_is_a_runtime_error(tmp_path: Path) -> None:
    config = _write_host_config(tmp_path, "strands", {})
    result = subprocess.run(
        [sys.executable, str(HOST_DIR), "task", str(config)],
        input=json.dumps({"v": 1, "prompt": "hola"}).encode(),
        capture_output=True,
        check=True,
    )
    last = json.loads(result.stdout.decode().splitlines()[-1])
    assert last["type"] == "agent_failed"
    assert last["reason"] == "runtime_error"
    assert "hola" not in result.stdout.decode()


def test_host_serve_mode_as_a_process(tmp_path: Path, app_options: dict[str, Any]) -> None:
    marker = tmp_path / "state" / "ready"
    port = _free_port()
    config = _write_host_config(
        tmp_path,
        "http-app",
        app_options,
        limits={"max_steps": 5},
        ready_marker=str(marker),
    )
    process = subprocess.Popen(
        [sys.executable, str(HOST_DIR), "serve", str(config), "--port", str(port)],
        stderr=subprocess.PIPE,
        env={**os.environ, "RAYITO_AGENT_SERVICE_TOKEN": "tok-unico-123"},
    )
    try:
        assert wait_for(marker.exists, 30)
        assert invoke(port, "hola")[0] == 401
        status, _, body = invoke(port, "hola", headers={"Authorization": "Bearer tok-unico-123"})
        assert (status, body["text"]) == (200, "eco:hola")
    finally:
        process.send_signal(signal.SIGTERM)
        _, stderr = process.communicate(timeout=30)
    assert process.returncode == 0
    assert b"hola" not in stderr
    assert b"tok-unico-123" not in stderr


def test_host_driver_registry_has_one_line_per_driver() -> None:
    assert sorted(host_main.DRIVERS) == [
        "callable",
        "command",
        "deepagents",
        "hermes",
        "http-app",
        "node-callable",
        "opencode",
        "strands",
        "strands-ts",
    ]


def test_host_serve_mode_exits_nonzero_when_the_driver_cannot_warm(tmp_path: Path) -> None:
    config = _write_host_config(
        tmp_path,
        "http-app",
        {
            "command": [sys.executable, "-c", "pass"],
            "port": _free_port(),
            "start_timeout_seconds": 5,
        },
    )
    process = subprocess.run(
        [sys.executable, str(HOST_DIR), "serve", str(config), "--port", str(_free_port())],
        capture_output=True,
        timeout=60,
    )
    assert process.returncode == 1
    assert b"warm_failed code=app_exited" in process.stderr


def test_a_host_json_with_another_schema_is_invalid(tmp_path: Path) -> None:
    config = _write_host_config(tmp_path, "opencode", {}, schema="rayito.agent-host/2")
    with pytest.raises(ValueError, match=r"host\.json"):
        host_main.load_host_config(str(config))
    ok = _write_host_config(tmp_path, "opencode", {}, schema=host_main.HOST_SCHEMA)
    assert host_main.load_host_config(str(ok))["driver"] == "opencode"
