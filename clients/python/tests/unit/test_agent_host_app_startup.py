"""El arranque de una app o un comando propios en el host del agente: el
entorno en el que corren (venv en el `PATH`, código en el `PYTHONPATH` y como
directorio de trabajo), el alias `python`, el diagnóstico saneado de un
arranque fallido (la cola de su salida de error) y el uso de tokens que
informa o no la app."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import textwrap
import threading
from pathlib import Path
from typing import Any

import pytest

from rayito._agent import _runner
from rayito._agent._runner.agent_host import __main__ as host_main
from rayito._agent._runner.agent_host import diagnostics, serve
from rayito._agent._runner.agent_host.drivers import _common, command, http_app
from rayito._agent._runner.agent_host.process import ChildProcess
from rayito._agent._runner.agent_host.protocol import AgentFailure, Request, RunState
from rayito._limits import AGENT_STARTUP_DIAGNOSTICS_MAX_BYTES

HOST_DIR = Path(_runner.__file__).parent / "agent_host"
START_TIMEOUT_SECONDS = 20
SECRET_VALUE = "valor-secreto-que-no-debe-salir"
FAKE_ACCESS_KEY = "AK" + "IA" + "ABCDEFGHIJKLMNOP"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _collect(driver: Any, request: Request) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    driver.run(request, events.append, RunState())
    return events


def test_the_diagnostics_bound_is_the_limits_json_one() -> None:
    assert diagnostics.MAX_DIAGNOSTICS_BYTES == AGENT_STARTUP_DIAGNOSTICS_MAX_BYTES


def test_sanitize_removes_environment_values_and_credentials() -> None:
    environ = {
        "AWS_BEARER_TOKEN_BEDROCK": SECRET_VALUE,
        "PATH": "/opt/agents/custom/bin:/usr/bin",
        "SHORT": "1",
    }
    text = (
        f"error con {SECRET_VALUE} y Bearer abc.def-ghi y token=abc123 "
        f"{FAKE_ACCESS_KEY} sk-abcdefghijklmnopqrstuvwxyz "
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.firma /opt/agents/custom/bin"
    )
    cleaned = diagnostics.sanitize_diagnostics(text, environ)
    for leaked in (
        SECRET_VALUE,
        "abc.def-ghi",
        "abc123",
        FAKE_ACCESS_KEY,
        "sk-abcdefghijklmnopqrstuvwxyz",
        "eyJhbGciOiJIUzI1NiJ9",
    ):
        assert leaked not in cleaned
    assert "/opt/agents/custom/bin" in cleaned
    assert diagnostics.REDACTED in cleaned


def test_sanitize_strips_control_characters_and_keeps_text_readable() -> None:
    cleaned = diagnostics.sanitize_diagnostics("\x1b[31mTraceback\x1b[0m\r\nValueError: x\x00", {})
    assert cleaned == "Traceback\nValueError: x"


def test_sanitize_keeps_the_end_within_the_bound() -> None:
    lines = [f"linea {index} " + "dato " * 16 for index in range(200)]
    cleaned = diagnostics.sanitize_diagnostics("\n".join(lines), {})
    assert len(cleaned.encode("utf-8")) <= diagnostics.MAX_DIAGNOSTICS_BYTES
    assert cleaned.endswith(lines[-1].rstrip())
    assert lines[0] not in cleaned


def test_sanitize_is_idempotent_on_a_clean_message() -> None:
    assert diagnostics.sanitize_diagnostics("No module named 'agent'", {}) == (
        "No module named 'agent'"
    )


def test_child_environment_puts_the_venv_first_and_the_code_in_pythonpath(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("PYTHONPATH", "/otro")
    env = _common.child_environment(
        {"path_prepend": ["/opt/agents/custom/bin"], "app_dir": "/opt/agents/app"}
    )
    assert env["PATH"] == "/opt/agents/custom/bin:/usr/bin"
    assert env["PYTHONPATH"] == "/opt/agents/app:/otro"


def test_child_environment_without_options_is_the_host_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.delenv("PYTHONPATH", raising=False)
    env = _common.child_environment({})
    assert env["PATH"] == "/usr/bin"
    assert "PYTHONPATH" not in env


def test_python_is_the_host_interpreter_when_the_path_has_none() -> None:
    assert _common.child_argv(["python", "serve.py"], {"PATH": "/no/existe"}) == [
        sys.executable,
        "serve.py",
    ]


def test_python_on_the_path_is_left_alone(tmp_path: Path) -> None:
    fake = tmp_path / "python"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    assert _common.child_argv(["python", "-m", "agent"], {"PATH": str(tmp_path)}) == [
        "python",
        "-m",
        "agent",
    ]
    assert _common.child_argv(["node", "serve.js"], {"PATH": "/no/existe"}) == ["node", "serve.js"]


MODULE_APP = textwrap.dedent(
    """
    import json, os, sys
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            payload = json.dumps({
                "status": "Healthy",
                "cwd": os.getcwd(),
                "pythonpath": os.environ.get("PYTHONPATH", ""),
                "path": os.environ.get("PATH", ""),
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
    """
)


def test_the_app_runs_as_a_module_from_the_source_directory(tmp_path: Path) -> None:
    package = tmp_path / "agent"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "server.py").write_text(MODULE_APP)
    (package / "__main__.py").write_text(
        "import runpy, sys\nrunpy.run_module('agent.server', run_name='__main__')\n"
    )
    port = _free_port()
    driver = http_app.create(
        {
            "command": ["python", "-m", "agent", str(port)],
            "port": port,
            "health_path": "/health",
            "protocol": "raw",
            "app_dir": str(tmp_path),
            "path_prepend": ["/opt/agents/custom/bin"],
            "workdir": "/",
            "start_timeout_seconds": START_TIMEOUT_SECONDS,
        }
    )
    try:
        driver.warm()
        response = driver.forward("GET", "/health", {}, None)
        reply = json.loads(response.read())
    finally:
        driver.cancel()
    assert os.path.realpath(reply["cwd"]) == os.path.realpath(tmp_path)
    assert reply["pythonpath"].split(os.pathsep)[0] == str(tmp_path)
    assert reply["path"].split(os.pathsep)[0] == "/opt/agents/custom/bin"


def test_a_failed_startup_carries_the_end_of_stderr_without_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MI_CLAVE_DE_API", SECRET_VALUE)
    script = (
        "import sys\n"
        "for index in range(400):\n"
        "    print('ruido', index, file=sys.stderr)\n"
        f"print('ModuleNotFoundError: No module named agent token={SECRET_VALUE}',\n"
        "      file=sys.stderr)\n"
        "sys.exit(3)\n"
    )
    driver = http_app.create(
        {
            "command": [sys.executable, "-c", script],
            "port": _free_port(),
            "start_timeout_seconds": START_TIMEOUT_SECONDS,
        }
    )
    with pytest.raises(AgentFailure) as raised:
        driver.warm()
    assert raised.value.detail_code == "app_exited"
    shown = raised.value.diagnostics or ""
    assert "No module named agent" in shown
    assert SECRET_VALUE not in shown
    assert len(shown.encode("utf-8")) <= diagnostics.MAX_DIAGNOSTICS_BYTES
    assert "ruido 0 " not in shown


def test_a_missing_interpreter_fails_with_a_clear_exception() -> None:
    driver = http_app.create({"command": ["/no/existe/python", "serve.py"], "port": _free_port()})
    with pytest.raises(FileNotFoundError):
        driver.warm()


def test_stderr_tail_is_empty_when_not_requested() -> None:
    child = ChildProcess([sys.executable, "-c", "import sys; print('x', file=sys.stderr)"], env={})
    child.wait()
    assert child.stderr_text() == ""


def test_startup_failure_of_a_driver_exception_uses_the_class_and_the_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OTRA_CLAVE", SECRET_VALUE)
    code, detail = host_main.startup_failure(
        FileNotFoundError(f"[Errno 2] No such file: {SECRET_VALUE}"), diagnostics
    )
    assert code == "FileNotFoundError"
    assert detail is not None
    assert "No such file" in detail
    assert SECRET_VALUE not in detail


def test_startup_failure_of_an_agent_failure_uses_its_detail_code() -> None:
    assert host_main.startup_failure(AgentFailure("runtime_error", "app_exited"), diagnostics) == (
        "app_exited",
        None,
    )
    failure = AgentFailure("runtime_error", "app_exited", "Traceback\nValueError")
    assert host_main.startup_failure(failure, diagnostics) == (
        "app_exited",
        "Traceback\nValueError",
    )


def test_the_failed_marker_is_the_code_then_the_diagnostics(tmp_path: Path) -> None:
    config = serve.ServiceConfig(ready_marker=str(tmp_path / "host" / "ready"))
    serve.write_failed_marker(config, "app_exited", "Traceback\nValueError")
    marker = tmp_path / "host" / f"ready{serve.FAILED_SUFFIX}"
    assert marker.read_text(encoding="utf-8") == "app_exited\nTraceback\nValueError\n"
    serve.write_failed_marker(config, "app_exited")
    assert marker.read_text(encoding="utf-8") == "app_exited\n"


def test_a_host_whose_app_dies_leaves_the_cause_in_the_failed_marker(tmp_path: Path) -> None:
    marker = tmp_path / "host" / "ready"
    script = "import sys; print('No module named agent', file=sys.stderr); sys.exit(1)"
    config = tmp_path / "host.json"
    config.write_text(
        json.dumps(
            {
                "v": 1,
                "driver": "http-app",
                "options": {
                    "command": [sys.executable, "-c", script],
                    "port": _free_port(),
                    "start_timeout_seconds": START_TIMEOUT_SECONDS,
                },
                "ready_marker": str(marker),
            }
        )
    )
    result = subprocess.run(
        [sys.executable, str(HOST_DIR), "serve", str(config), "--port", str(_free_port())],
        capture_output=True,
        env={**os.environ},
        timeout=60,
        check=False,
    )
    assert result.returncode == 1
    code, _, detail = (
        (tmp_path / "host" / f"ready{serve.FAILED_SUFFIX}")
        .read_text(encoding="utf-8")
        .partition("\n")
    )
    assert code == "app_exited"
    assert "No module named agent" in detail


def test_app_usage_reads_every_known_spelling() -> None:
    assert http_app.app_usage({"input_tokens": 3, "output_tokens": 4}) == {
        "input": 3,
        "output": 4,
        "reasoning": 0,
        "cache_read": 0,
        "cache_write": 0,
    }
    chat = http_app.app_usage({"prompt_tokens": 7, "completion_tokens": 1})
    cached = http_app.app_usage({"input": 1, "cache_read_input_tokens": 9})
    assert chat is not None and chat["input"] == 7
    assert cached is not None and cached["cache_read"] == 9


@pytest.mark.parametrize("raw", [None, "x", [], {}, {"otro": 3}, {"input": True}, {"input": "3"}])
def test_app_usage_is_unknown_when_the_app_reports_nothing(raw: object) -> None:
    assert http_app.app_usage(raw) is None


USAGE_APP = textwrap.dedent(
    """
    import json, sys
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def log_message(self, *args): pass
        def _json(self, payload):
            data = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        def do_GET(self):
            self._json({"status": "Healthy"})
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
            if body["prompt"] == "con-uso":
                self._json({"text": "hola", "usage": {"input_tokens": 12, "output_tokens": 5}})
            else:
                self._json({"text": "hola"})

    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
    """
)


def _usage_driver(tmp_path: Path) -> Any:
    script = tmp_path / "usage_app.py"
    script.write_text(USAGE_APP)
    port = _free_port()
    return http_app.create(
        {
            "command": [sys.executable, str(script), str(port)],
            "port": port,
            "workdir": str(tmp_path),
            "start_timeout_seconds": START_TIMEOUT_SECONDS,
        }
    )


def test_an_app_that_reports_usage_populates_it(tmp_path: Path) -> None:
    driver = _usage_driver(tmp_path)
    try:
        events = _collect(driver, Request(prompt="con-uso"))
    finally:
        driver.cancel()
    finished = next(e for e in events if e["type"] == "step_finished")
    assert finished["usage"]["input"] == 12
    assert finished["usage"]["output"] == 5


def test_an_app_that_reports_no_usage_has_unknown_usage_not_zeros(tmp_path: Path) -> None:
    driver = _usage_driver(tmp_path)
    try:
        events = _collect(driver, Request(prompt="sin-uso"))
    finally:
        driver.cancel()
    finished = next(e for e in events if e["type"] == "step_finished")
    assert finished["usage"] is None


def test_the_command_driver_reports_unknown_usage_with_the_text_protocol(tmp_path: Path) -> None:
    driver = command.create(
        {
            "command": ["python", "-c", "print('hola')"],
            "protocol": "text",
            "workdir": str(tmp_path),
            "path_prepend": [str(tmp_path / "no-existe")],
        }
    )
    events = _collect(driver, Request(prompt="x"))
    finished = next(e for e in events if e["type"] == "step_finished")
    assert finished["usage"] is None
    assert next(e for e in events if e["type"] == "text")["text"] == "hola\n"


def test_the_tracker_tells_reported_from_unreported_usage() -> None:
    from rayito._agent._runner.agent_host.limits import Limits, LimitTracker

    unreported = LimitTracker(Limits())
    unreported.observe({"type": "step_finished", "usage": None})
    assert unreported.usage_reported is False
    reported = LimitTracker(Limits())
    reported.observe({"type": "step_finished", "usage": {"input": 1}})
    assert reported.usage_reported is True
    assert reported.total_tokens == 1


def test_the_result_body_says_whether_usage_was_reported() -> None:
    from types import SimpleNamespace

    from rayito._agent._runner.agent_host.limits import Limits, LimitTracker

    tracker = LimitTracker(Limits())
    tracker.observe({"type": "step_finished", "usage": None})
    invocation = SimpleNamespace(tracker=tracker, session_id="app_x")
    body = serve.result_body(invocation, {"type": "done"})  # type: ignore[arg-type]
    assert body["usage_reported"] is False
    assert body["usage"]["input"] == 0


def test_a_failed_marker_is_written_from_a_thread_safe_path(tmp_path: Path) -> None:
    config = serve.ServiceConfig(ready_marker=str(tmp_path / "ready"))
    writers = [
        threading.Thread(target=serve.write_failed_marker, args=(config, f"codigo_{i}"))
        for i in range(4)
    ]
    for writer in writers:
        writer.start()
    for writer in writers:
        writer.join()
    first_line = (tmp_path / f"ready{serve.FAILED_SUFFIX}").read_text().splitlines()[0]
    assert first_line.startswith("codigo_")
