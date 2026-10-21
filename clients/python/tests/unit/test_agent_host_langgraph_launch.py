"""Cómo arranca el host `langgraph dev` con `langgraph-api` (ADR-033 D5,
`AWS_API_NOTES.md` Q189): el código copiado (`app_dir`) es de sólo lectura y
`langgraph dev` escribe su estado (`.langgraph_api/`) en su directorio de
trabajo y resuelve allí las rutas de `langgraph.json`, así que el host copia
la configuración a `workdir` con las rutas absolutas y arranca la app en
`workdir`. Las pruebas usan un servidor falso en el loopback que dice dónde
arrancó y con qué argumentos; nunca `langgraph dev`. El host se prueba una
vez, aquí (`parity-checklist.md`)."""

from __future__ import annotations

import json
import os
import stat
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from rayito._agent._runner.agent_host.drivers import http_app
from rayito._agent._runner.agent_host.protocol import Request, RunState
from tests.unit.agent_host_support import APP_START_TIMEOUT_SECONDS, free_port

LANGGRAPH_JSON = {
    "dependencies": ["."],
    "graphs": {
        "agent": "./src/agent.py:graph",
        "triage": {"path": "triage.py:graph", "description": "triaje"},
        "module": "tickets.graphs:echo",
        "absolute": "/srv/other.py:graph",
    },
    "env": ".env",
    "python_version": "3.12",
}

FAKE_LANGGRAPH_DEV = """
import json, os, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, payload):
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._send({"ok": True})

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        config = sys.argv[sys.argv.index("--config") + 1]
        seen = {"cwd": os.getcwd(), "config": config, "graphs": json.load(open(config))["graphs"]}
        self._send({"messages": [{"type": "ai", "content": json.dumps(seen)}]})


ThreadingHTTPServer(("127.0.0.1", int(os.environ["APP_PORT"])), Handler).serve_forever()
"""


def _read_only_app(tmp_path: Path) -> Path:
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    (app_dir / "langgraph.json").write_text(json.dumps(LANGGRAPH_JSON), encoding="utf-8")
    app_dir.chmod(stat.S_IRUSR | stat.S_IXUSR)
    return app_dir


def test_relative_paths_become_absolute_against_the_app_dir() -> None:
    absolute = http_app.langgraph_absolute_config(LANGGRAPH_JSON, "/opt/agents/app")
    assert absolute == {
        "dependencies": ["/opt/agents/app"],
        "graphs": {
            "agent": "/opt/agents/app/src/agent.py:graph",
            "triage": {"path": "/opt/agents/app/triage.py:graph", "description": "triaje"},
            "module": "tickets.graphs:echo",
            "absolute": "/srv/other.py:graph",
        },
        "env": "/opt/agents/app/.env",
        "python_version": "3.12",
    }


def test_an_env_mapping_and_unknown_shapes_are_kept() -> None:
    config = {"graphs": {"odd": 3}, "env": {"MODE": "dev"}, "dependencies": [{"x": 1}]}
    assert http_app.langgraph_absolute_config(config, "/app") == config


@pytest.mark.parametrize("inline", [False, True])
def test_the_config_is_copied_to_the_workdir_with_absolute_paths(
    tmp_path: Path, inline: bool
) -> None:
    app_dir = _read_only_app(tmp_path)
    workdir = tmp_path / "work"
    workdir.mkdir()
    flag = ["--config=langgraph.json"] if inline else ["--config", "langgraph.json"]
    launch = http_app.langgraph_launch(
        ["langgraph", "dev", "--port", "8000", *flag], str(app_dir), str(workdir)
    )
    assert launch is not None
    argv, cwd = launch
    copy = workdir / http_app.LANGGRAPH_CONFIG_COPY
    expected_flag = [f"--config={copy}"] if inline else ["--config", str(copy)]
    assert (argv, cwd) == (["langgraph", "dev", "--port", "8000", *expected_flag], str(workdir))
    written = json.loads(copy.read_text("utf-8"))
    assert written == http_app.langgraph_absolute_config(LANGGRAPH_JSON, str(app_dir))


@pytest.mark.parametrize(
    "command",
    [["langgraph", "dev"], ["langgraph", "dev", "--config", "missing.json"], ["x", "--config"]],
)
def test_without_a_readable_config_the_app_starts_as_before(
    tmp_path: Path, command: list[str]
) -> None:
    app_dir = _read_only_app(tmp_path)
    assert http_app.langgraph_launch(command, str(app_dir), str(tmp_path)) is None


def test_a_workdir_that_cannot_be_written_keeps_the_usual_start(tmp_path: Path) -> None:
    app_dir = _read_only_app(tmp_path)
    workdir = tmp_path / "locked"
    workdir.mkdir()
    workdir.chmod(stat.S_IRUSR | stat.S_IXUSR)
    command = ["langgraph", "dev", "--config", "langgraph.json"]
    assert http_app.langgraph_launch(command, str(app_dir), str(workdir)) is None


def _driver(tmp_path: Path, protocol: str, **extra: Any) -> http_app.Driver:
    script = tmp_path / "fake_langgraph_dev.py"
    script.write_text(textwrap.dedent(FAKE_LANGGRAPH_DEV), encoding="utf-8")
    return http_app.Driver(
        {
            "command": [sys.executable, str(script), "--config", "langgraph.json"],
            "port": free_port(),
            "port_env": "APP_PORT",
            "protocol": protocol,
            "health_path": "/ok",
            "start_timeout_seconds": APP_START_TIMEOUT_SECONDS,
            **extra,
        }
    )


def _seen(driver: http_app.Driver) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    try:
        driver.run(Request(prompt="hola"), events.append, RunState())
    finally:
        driver.cancel()
    text = next(event["text"] for event in events if event["type"] == "text")
    return dict(json.loads(text))


def test_langgraph_dev_starts_in_the_workdir_with_the_copied_config(tmp_path: Path) -> None:
    app_dir = _read_only_app(tmp_path)
    workdir = tmp_path / "work"
    workdir.mkdir()
    seen = _seen(
        _driver(
            tmp_path,
            "langgraph-api",
            assistant_id="agent",
            app_dir=str(app_dir),
            workdir=str(workdir),
        )
    )
    assert os.path.realpath(seen["cwd"]) == os.path.realpath(workdir)
    assert seen["config"] == str(workdir / http_app.LANGGRAPH_CONFIG_COPY)
    assert seen["graphs"]["agent"] == f"{app_dir}/src/agent.py:graph"


def test_without_a_workdir_the_app_still_starts_in_the_app_dir(tmp_path: Path) -> None:
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    (app_dir / "langgraph.json").write_text(json.dumps(LANGGRAPH_JSON), encoding="utf-8")
    seen = _seen(_driver(tmp_path, "langgraph-api", assistant_id="agent", app_dir=str(app_dir)))
    assert os.path.realpath(seen["cwd"]) == os.path.realpath(app_dir)
    assert seen["config"] == "langgraph.json"
