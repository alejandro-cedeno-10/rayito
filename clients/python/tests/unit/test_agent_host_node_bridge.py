"""El driver `node-callable` con el puente de Node de verdad (0.14 D3): el host
en modo `task` lanza `node node_bridge.mjs`, que ejecuta un agente de
JavaScript (un `callable`, un grafo de LangGraph.js de doble y un agente del AI
SDK de doble) y devuelve el protocolo v1 por stdout. Se salta si no hay un Node
capaz de cargar módulos ES (22 o posterior). La traducción de cada framework se
prueba una vez, en TypeScript (`agent-node-bridge.test.ts`); aquí sólo el
contrato entre el host y el puente."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from rayito._agent import _runner
from rayito._agent._recipes import NODE_BRIDGE_NAME

HOST_DIR = Path(_runner.__file__).parent / "agent_host"
BRIDGE = Path(_runner.__file__).parent.parent / "_assets" / NODE_BRIDGE_NAME
MINIMUM_NODE_MAJOR = 22
SESSION = "rda_" + "e" * 32
REQUEST = {"v": 1, "prompt": "suma 2 y 3", "session_id": SESSION, "model": None, "reasoning": False}


def _node() -> str | None:
    path = shutil.which("node")
    if path is None:
        return None
    version = subprocess.run([path, "--version"], capture_output=True, text=True, check=False)
    match = re.match(r"v(\d+)\.", version.stdout)
    return path if match and int(match.group(1)) >= MINIMUM_NODE_MAJOR else None


pytestmark = pytest.mark.skipif(_node() is None, reason="hace falta Node 22 o posterior")

CALLABLE_AGENT = """
export const handler = ({ prompt, session_id }) => ({
  text: `eco: ${prompt} (${session_id})`,
  usage: { input: 4, output: 2 },
});
"""
GRAPH_AGENT = """
const reply = {
  agent: { messages: [{ type: "ai", content: "La suma es 5", tool_calls: [],
    usage_metadata: { input_tokens: 9, output_tokens: 3 } }] },
};
export const graph = {
  async stream(input, options) {
    globalThis.seen = { input, options };
    return (async function* () { yield reply; })();
  },
};
"""
AI_AGENT = """
export const agent = {
  async stream({ messages }) {
    const parts = [
      { type: "start-step" },
      { type: "text-delta", text: `recibí ${messages.length} mensaje` },
      { type: "finish-step", finishReason: "stop", usage: { inputTokens: 2, outputTokens: 1 } },
    ];
    return { fullStream: (async function* () { yield* parts; })() };
  },
};
"""


def _run_bridge_through_the_host(
    tmp_path: Path, source: str, adapter: str, **config: object
) -> list[dict[str, Any]]:
    app = tmp_path / "app"
    app.mkdir()
    (app / "agent.mjs").write_text(source, encoding="utf-8")
    bridge_config = tmp_path / "config.json"
    bridge_config.write_text(
        json.dumps(
            {
                "bridge": str(BRIDGE),
                "adapter": adapter,
                "entrypoint": "agent.mjs",
                "app_dir": str(app),
                "workdir": str(tmp_path),
                "sessions_dir": str(tmp_path / "sessions"),
                **config,
            }
        ),
        encoding="utf-8",
    )
    host_json = tmp_path / "host.json"
    host_json.write_text(
        json.dumps(
            {
                "schema": "rayito.agent-host/1",
                "v": 1,
                "driver": "node-callable",
                "options": {"config_path": str(bridge_config)},
            }
        ),
        encoding="utf-8",
    )
    completed = subprocess.run(
        [sys.executable, str(HOST_DIR), "task", str(host_json)],
        input=json.dumps(REQUEST),
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "PYTHONPATH": ""},
        check=False,
    )
    return [json.loads(line) for line in completed.stdout.splitlines()]


def _kinds(lines: list[dict[str, Any]]) -> list[str]:
    return [line["type"] for line in lines]


def test_a_callable_agent_runs_through_the_host(tmp_path: Path) -> None:
    lines = _run_bridge_through_the_host(tmp_path, CALLABLE_AGENT, "callable")
    assert _kinds(lines) == ["session", "step_started", "text", "step_finished", "done"]
    assert lines[2]["text"] == f"eco: suma 2 y 3 ({SESSION})"
    assert lines[3]["usage"]["input"] == 4
    assert lines[-1]["session_id"] == SESSION


def test_a_langgraph_graph_runs_through_the_host(tmp_path: Path) -> None:
    lines = _run_bridge_through_the_host(tmp_path, GRAPH_AGENT, "langgraph")
    assert _kinds(lines) == ["session", "step_started", "text", "step_finished", "done"]
    assert lines[2]["text"] == "La suma es 5"
    assert lines[3]["usage"]["output"] == 3
    saved = json.loads((tmp_path / "sessions" / f"{SESSION}.json").read_text("utf-8"))
    assert saved == [
        {"role": "user", "content": "suma 2 y 3"},
        {"role": "assistant", "content": "La suma es 5"},
    ]


def test_an_ai_sdk_agent_runs_through_the_host(tmp_path: Path) -> None:
    lines = _run_bridge_through_the_host(tmp_path, AI_AGENT, "vercel-ai")
    assert _kinds(lines) == [
        "session",
        "step_started",
        "text_delta",
        "text",
        "step_finished",
        "done",
    ]
    assert lines[3]["text"] == "recibí 1 mensaje"


def test_a_missing_export_is_a_runtime_error_with_a_closed_code(tmp_path: Path) -> None:
    lines = _run_bridge_through_the_host(
        tmp_path, CALLABLE_AGENT, "callable", entrypoint="agent.mjs#nope"
    )
    assert lines[-1]["type"] == "agent_failed"
    assert (lines[-1]["reason"], lines[-1]["detail_code"]) == (
        "runtime_error",
        "entrypoint_not_found",
    )


def test_an_unknown_adapter_never_reaches_the_user_code(tmp_path: Path) -> None:
    lines = _run_bridge_through_the_host(tmp_path, CALLABLE_AGENT, "crewai")
    assert lines[-1]["detail_code"] == "unknown_adapter"
    assert "text" not in _kinds(lines)


ENV_AGENT = """
export const handler = () => ({
  text: [process.env.AWS_ACCESS_KEY_ID, process.env.AWS_SECRET_ACCESS_KEY,
    process.env.AWS_SESSION_TOKEN ?? "sin-token", process.env.OTRA ?? "sin-otra"].join("|"),
});
"""


def _run_bridge_directly(tmp_path: Path, request: dict[str, Any]) -> list[dict[str, Any]]:
    app = tmp_path / "app"
    app.mkdir()
    (app / "agent.mjs").write_text(ENV_AGENT, encoding="utf-8")
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "adapter": "callable",
                "entrypoint": "agent.mjs",
                "app_dir": str(app),
                "workdir": str(tmp_path),
                "sessions_dir": str(tmp_path / "sessions"),
            }
        ),
        encoding="utf-8",
    )
    environment = {key: value for key, value in os.environ.items() if not key.startswith("AWS_")}
    completed = subprocess.run(
        [str(_node()), str(BRIDGE), str(config)],
        input=json.dumps(request),
        capture_output=True,
        text=True,
        timeout=60,
        env=environment,
        check=False,
    )
    return [json.loads(line) for line in completed.stdout.splitlines()]


def test_the_credentials_the_host_passes_reach_the_agent_environment(tmp_path: Path) -> None:
    request = {
        **REQUEST,
        "aws_env": {
            "AWS_ACCESS_KEY_ID": "AKIDEXAMPLE",
            "AWS_SECRET_ACCESS_KEY": "s",
            "AWS_SESSION_TOKEN": "t",
        },
    }
    lines = _run_bridge_directly(tmp_path, request)
    text = next(line["text"] for line in lines if line["type"] == "text")
    assert text == "AKIDEXAMPLE|s|t|sin-otra"
    assert lines[-1]["type"] == "done"


def test_only_the_three_credential_variables_are_accepted(tmp_path: Path) -> None:
    request = {
        **REQUEST,
        "aws_env": {"AWS_ACCESS_KEY_ID": "AKIDEXAMPLE", "AWS_SECRET_ACCESS_KEY": "s", "OTRA": "x"},
    }
    lines = _run_bridge_directly(tmp_path, request)
    text = next(line["text"] for line in lines if line["type"] == "text")
    assert text == "AKIDEXAMPLE|s|sin-token|sin-otra"


def test_without_credentials_the_environment_is_untouched(tmp_path: Path) -> None:
    lines = _run_bridge_directly(tmp_path, REQUEST)
    text = next(line["text"] for line in lines if line["type"] == "text")
    assert text == "||sin-token|sin-otra"
