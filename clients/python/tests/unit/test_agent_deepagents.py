"""Adaptador de deepagents (`ai-agent-deepagents`): configuración del
runner, script de ejecución y traducción del protocolo JSONL v1, contra los
ficheros dorados que comparte con TypeScript (`testdata/agent/`)."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any, cast

import pytest

from rayito import (
    AgentFailed,
    AgentModel,
    AgentPermissions,
    AgentSpec,
    DeepAgents,
    InvalidArgumentException,
    McpLocal,
    SubAgent,
    ToolCall,
)
from rayito._agent._deepagents import (
    DEEPAGENTS_CONFIG_PATH,
    DeepAgentsState,
)
from rayito._agent._runtime import RunRequest
from rayito._agent._runtimes import AGENT_RUNTIMES, resolve_runtime

TESTDATA = Path(__file__).parents[4] / "testdata" / "agent"
GATEWAY_URLS = {
    "bedrock": "http://127.0.0.1:18001",
    "anthropic": "http://127.0.0.1:18002",
    "openai": "http://127.0.0.1:18003/",
}
SESSION_ID = "rda_0000000000000000000000000000000a"


def _bedrock_spec() -> AgentSpec:
    return AgentSpec(
        model=AgentModel(
            provider="bedrock",
            id="us.anthropic.claude-haiku-4-5-20251001-v1:0",
            gateway="bedrock",
            region="us-east-1",
        )
    )


def _anthropic_spec() -> AgentSpec:
    return AgentSpec(
        model=AgentModel(
            provider="anthropic", id="claude-haiku-4-5", gateway="anthropic", prompt_caching=False
        ),
        instructions="Responde en español.",
        permissions=AgentPermissions(
            default="deny",
            tools={"read": "allow", "edit": "allow", "bash": {"git *": "allow", "*": "deny"}},
        ),
    )


def _openai_spec() -> AgentSpec:
    return AgentSpec(
        model=AgentModel(
            provider="openai-compatible", id="modelo-a", gateway="openai", base_path="/v1"
        ),
        agents={
            "revisor": SubAgent(
                description="Revisa cambios",
                instructions="Revisa el diff.",
                model="modelo-b",
                permissions=AgentPermissions(tools={"edit": "deny"}),
            )
        },
    )


CONFIG_CASES = {
    "bedrock": _bedrock_spec,
    "anthropic": _anthropic_spec,
    "openai-compatible": _openai_spec,
}


@pytest.mark.parametrize("case", sorted(CONFIG_CASES))
def test_config_matches_golden(case: str) -> None:
    golden = json.loads((TESTDATA / "deepagents-config.json").read_text(encoding="utf-8"))[case]
    files = DeepAgents(golden["entrypoint"]).build_config(
        CONFIG_CASES[case](), gateway_urls=GATEWAY_URLS, workdir="/home/user/proyecto"
    )
    (item,) = files.files
    assert item.path == DEEPAGENTS_CONFIG_PATH
    assert item.data.decode("utf-8") == golden["config_json"]
    assert item.mode == golden["mode"]
    assert files.config_sha256 == golden["config_sha256"]


def test_registry_has_deepagents() -> None:
    assert isinstance(AGENT_RUNTIMES["deepagents"], DeepAgents)
    assert resolve_runtime("deepagents").name == "deepagents"
    custom = DeepAgents("pkg.mod:build")
    assert resolve_runtime(custom) is custom


@pytest.mark.parametrize("entrypoint", ["", "pkg", "pkg.mod:", "1pkg:build", "pkg:mod:x", 3])
def test_entrypoint_is_validated(entrypoint: object) -> None:
    with pytest.raises(InvalidArgumentException):
        DeepAgents(cast(Any, entrypoint))


@pytest.mark.parametrize(
    "spec",
    [
        AgentSpec(model=_bedrock_spec().model, mcp={"local": McpLocal(command=["mcp-server"])}),
        AgentSpec(model=_bedrock_spec().model, raw_config={"tui": {"x": 1}}),
        AgentSpec(
            model=_bedrock_spec().model, permissions=AgentPermissions(tools={"webfetch": "allow"})
        ),
        AgentSpec(
            model=_bedrock_spec().model,
            permissions=AgentPermissions(tools={"read": {"*.env": "deny"}}),
        ),
    ],
    ids=["mcp", "raw_config", "unknown-tool", "patterns-outside-bash"],
)
def test_config_rejects_what_deepagents_cannot_do(spec: AgentSpec) -> None:
    with pytest.raises(InvalidArgumentException):
        DeepAgents().build_config(spec, gateway_urls=GATEWAY_URLS, workdir="/home/user")


def test_config_requires_every_gateway() -> None:
    with pytest.raises(InvalidArgumentException):
        DeepAgents().build_config(
            _bedrock_spec(), gateway_urls={"otra": "http://127.0.0.1:1"}, workdir="/home/user"
        )


@pytest.mark.parametrize(
    ("case", "request_kwargs"),
    [
        ("new", {}),
        (
            "resume",
            {
                "session_id": SESSION_ID,
                "model": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
                "reasoning": True,
            },
        ),
    ],
)
def test_run_command_matches_golden(case: str, request_kwargs: dict[str, Any]) -> None:
    golden = json.loads((TESTDATA / "deepagents-run-commands.json").read_text(encoding="utf-8"))[
        case
    ]
    command = DeepAgents().command(
        RunRequest(spec=_bedrock_spec(), prompt="hola ñ", workdir="/home/user", **request_kwargs)
    )
    assert command.script == golden["script"]
    assert dict(command.envs) == golden["envs"]
    assert command.stdin == golden["stdin"].encode("utf-8")
    assert "hola" not in command.script


@pytest.mark.parametrize(
    "request_kwargs",
    [
        {"session_id": "ses_otro"},
        {"model": "con espacios"},
    ],
)
def test_run_command_rejects(request_kwargs: dict[str, Any]) -> None:
    with pytest.raises(InvalidArgumentException):
        DeepAgents().command(
            RunRequest(spec=_bedrock_spec(), prompt="x", workdir="/home/user", **request_kwargs)
        )


def test_events_match_golden() -> None:
    expected = json.loads((TESTDATA / "rayito-protocol-v1-expected.json").read_text("utf-8"))
    runtime = DeepAgents()
    state = runtime.new_state()
    events: list[dict[str, Any]] = []
    for line in (TESTDATA / "rayito-protocol-v1.jsonl").read_bytes().splitlines():
        events.extend(dataclasses.asdict(event) for event in runtime.parse_line(line, state))
    assert events == expected["events"]
    assert isinstance(state, DeepAgentsState)
    assert state.session_id == expected["session_id"]
    assert state.ignored_lines == expected["ignored_lines"]
    assert dataclasses.asdict(runtime.finish(state, 0)) == expected["done"]


@pytest.mark.parametrize(
    ("line", "reason", "detail"),
    [
        (
            b'{"v":1,"type":"agent_failed","reason":"model_error","detail_code":"ThrottlingException"}',
            "model_error",
            "ThrottlingException",
        ),
        (
            b'{"v":1,"type":"agent_failed","reason":"max_steps","detail_code":"x y"}',
            "protocol_error",
            None,
        ),
        (b'{"v":1,"type":"rayito.busy"}', "busy", None),
        (b'{"v":1,"type":"rayito.runtime_missing"}', "runtime_missing", None),
    ],
)
def test_failure_lines_are_terminal(line: bytes, reason: str, detail: str | None) -> None:
    runtime = DeepAgents()
    state = runtime.new_state()
    (event,) = runtime.parse_line(line, state)
    assert isinstance(event, AgentFailed)
    assert (event.reason, event.detail_code) == (reason, detail)
    assert runtime.finish(state, 0) == event


def test_finish_without_done_is_a_protocol_error() -> None:
    runtime = DeepAgents()
    state = runtime.new_state()
    runtime.parse_line(f'{{"v":1,"type":"session","session_id":"{SESSION_ID}"}}'.encode(), state)
    failed = runtime.finish(state, 0)
    assert isinstance(failed, AgentFailed)
    assert (failed.reason, failed.session_id) == ("protocol_error", SESSION_ID)
    crashed = runtime.finish(state, 137)
    assert isinstance(crashed, AgentFailed)
    assert (crashed.reason, crashed.exit_code) == ("runtime_error", 137)


def test_oversized_tool_output_is_truncated() -> None:
    runtime = DeepAgents()
    state = runtime.new_state()
    line = json.dumps(
        {
            "v": 1,
            "type": "tool_call",
            "call_id": "c",
            "name": "ls",
            "status": "completed",
            "output": "x" * 70_000,
        }
    ).encode()
    (event,) = runtime.parse_line(line, state)
    assert isinstance(event, ToolCall)
    assert event.output_truncated is True


def test_template_and_warmup() -> None:
    runtime = DeepAgents()
    assert runtime.template_steps() == ()
    (step,) = runtime.warmup_steps()
    assert "import deepagents" in step.cmd
    assert not step.background
