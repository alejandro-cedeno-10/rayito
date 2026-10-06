"""Adaptador de OpenCode (`ai-agent-core`): configuración, script de
ejecución y traducción de eventos, contra los ficheros dorados que
comparte con TypeScript (`testdata/agent/`)."""

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
    InvalidArgumentException,
    McpLocal,
    McpRemote,
    SubAgent,
)
from rayito._agent._opencode import (
    OPENCODE_CONFIG_PATH,
    OPENCODE_INSTRUCTIONS_PATH,
    OpenCodeRuntime,
    OpenCodeState,
)
from rayito._agent._runtime import RunRequest
from rayito._agent._runtimes import resolve_runtime

TESTDATA = Path(__file__).parents[4] / "testdata" / "agent"
GATEWAY_URLS = {
    "bedrock": "http://127.0.0.1:18001",
    "anthropic": "http://127.0.0.1:18002",
    "openai": "http://127.0.0.1:18003/",
    "docs": "http://127.0.0.1:18004",
}


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
        model=AgentModel(provider="anthropic", id="claude-haiku-4-5", gateway="anthropic"),
        small_model="claude-haiku-4-5-mini",
        instructions="Responde en español.",
        permissions=AgentPermissions(
            default="deny", tools={"read": "allow", "bash": {"git *": "allow", "*": "deny"}}
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
        mcp={
            "local": McpLocal(
                command=["mcp-server", "--stdio"], envs={"MODO": "x"}, timeout_seconds=2.5
            ),
            "docs": McpRemote(gateway="docs", path="/mcp"),
        },
        raw_config={"tui": {"scroll_speed": 3}},
    )


CONFIG_CASES = {
    "bedrock": _bedrock_spec,
    "anthropic": _anthropic_spec,
    "openai-compatible": _openai_spec,
}


def _golden(case: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(
        (TESTDATA / "opencode-config" / f"{case}.json").read_text(encoding="utf-8")
    )
    return data


@pytest.mark.parametrize("case", sorted(CONFIG_CASES))
def test_config_matches_golden(case: str) -> None:
    golden = _golden(case)
    files = OpenCodeRuntime().build_config(
        CONFIG_CASES[case](), gateway_urls=GATEWAY_URLS, workdir="/home/user"
    )
    by_path = {item.path: item.data.decode("utf-8") for item in files.files}
    assert by_path[OPENCODE_CONFIG_PATH] == golden["opencode_json"]
    assert by_path.get(OPENCODE_INSTRUCTIONS_PATH) == golden["agents_md"]
    assert files.config_sha256 == golden["config_sha256"]


def test_config_never_carries_a_credential_but_the_placeholder() -> None:
    files = OpenCodeRuntime().build_config(
        _anthropic_spec(), gateway_urls=GATEWAY_URLS, workdir="/home/user"
    )
    config = json.loads(files.files[0].data)
    assert config["provider"]["anthropic"]["options"]["apiKey"] == "placeholder-not-a-secret"


def test_config_requires_every_gateway() -> None:
    with pytest.raises(InvalidArgumentException):
        OpenCodeRuntime().build_config(
            _bedrock_spec(), gateway_urls={"otra": "http://127.0.0.1:1"}, workdir="/home/user"
        )


@pytest.mark.parametrize(
    ("case", "request_kwargs"),
    [
        ("auto", {"attach": "auto"}),
        (
            "always-resume",
            {
                "attach": True,
                "session_id": "ses_0000000000000000000000000a",
                "model": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
                "reasoning": True,
                "workdir": "/home/user/mi proyecto's",
            },
        ),
        ("never", {"attach": False}),
    ],
)
def test_run_command_matches_golden(case: str, request_kwargs: dict[str, Any]) -> None:
    golden = json.loads((TESTDATA / "opencode-run-commands.json").read_text(encoding="utf-8"))[case]
    kwargs: dict[str, Any] = {"workdir": "/home/user", **request_kwargs}
    command = OpenCodeRuntime().command(RunRequest(spec=_bedrock_spec(), prompt="hola ñ", **kwargs))
    assert command.script == golden["script"]
    assert dict(command.envs) == golden["envs"]
    assert command.stdin == "hola ñ".encode()
    assert "hola" not in command.script


def test_run_command_rejects_unknown_attach() -> None:
    with pytest.raises(InvalidArgumentException):
        OpenCodeRuntime().command(
            RunRequest(spec=_bedrock_spec(), prompt="x", workdir="/home/user", attach=cast(Any, 1))
        )


def _event_dict(event: object) -> dict[str, Any]:
    data = dataclasses.asdict(event)  # type: ignore[call-overload]
    return {key: value for key, value in data.items()}


def test_events_match_golden() -> None:
    expected = json.loads((TESTDATA / "expected-events.json").read_text(encoding="utf-8"))
    runtime = OpenCodeRuntime()
    state = runtime.new_state()
    events: list[dict[str, Any]] = []
    for line in (TESTDATA / "opencode-v1.18.34-events.jsonl").read_bytes().splitlines():
        events.extend(_event_dict(event) for event in runtime.parse_line(line, state))
    assert events == expected["events"]
    assert isinstance(state, OpenCodeState)
    assert state.attached is expected["attached"]
    assert state.ignored_lines == expected["ignored_lines"]
    assert _event_dict(runtime.finish(state, 0)) == expected["done"]


@pytest.mark.parametrize(
    ("line", "reason", "detail"),
    [
        (
            b'{"type":"error","sessionID":"ses_1","error":{"name":"APIError","data":{"message":"x"}}}',
            "model_error",
            "APIError",
        ),
        (b'{"type":"rayito.busy"}', "busy", None),
        (b'{"type":"rayito.runtime_missing"}', "runtime_missing", None),
    ],
)
def test_failure_lines_are_terminal(line: bytes, reason: str, detail: str | None) -> None:
    runtime = OpenCodeRuntime()
    state = runtime.new_state()
    (event,) = runtime.parse_line(line, state)
    assert isinstance(event, AgentFailed)
    assert (event.reason, event.detail_code) == (reason, detail)
    assert runtime.finish(state, 0) == event


def test_finish_without_error_events() -> None:
    runtime = OpenCodeRuntime()
    state = runtime.new_state()
    failed = runtime.finish(state, 0)
    assert isinstance(failed, AgentFailed) and failed.reason == "protocol_error"
    runtime.parse_line(b'{"type":"step_start","sessionID":"ses_1","part":{}}', state)
    crashed = runtime.finish(state, 3)
    assert isinstance(crashed, AgentFailed)
    assert (crashed.reason, crashed.exit_code, crashed.session_id) == ("runtime_error", 3, "ses_1")


def test_tool_output_is_truncated() -> None:
    runtime = OpenCodeRuntime()
    state = runtime.new_state()
    line = json.dumps(
        {
            "type": "tool_use",
            "sessionID": "ses_1",
            "part": {
                "callID": "c",
                "tool": "bash",
                "state": {"status": "completed", "input": {}, "output": "a" * 70_000},
            },
        }
    ).encode()
    (event,) = runtime.parse_line(line, state)
    assert _event_dict(event)["output_truncated"] is True


def test_abort_command_only_when_attached() -> None:
    runtime = OpenCodeRuntime()
    state = runtime.new_state()
    runtime.parse_line(b'{"type":"step_start","sessionID":"ses_1","part":{}}', state)
    assert runtime.abort_command(state) is None
    runtime.parse_line(b'{"type":"rayito.attached"}', state)
    command = runtime.abort_command(state)
    assert command is not None
    assert "'http://127.0.0.1:4096/session/ses_1/abort'" in command
    unsafe = runtime.new_state()
    runtime.parse_line(b'{"type":"rayito.attached","sessionID":"ses;rm"}', unsafe)
    assert runtime.abort_command(unsafe) is None


def test_warmup_steps() -> None:
    runtime = OpenCodeRuntime()
    assert [step.background for step in runtime.warmup_steps(serve=False)] == [False]
    serve = runtime.warmup_steps(serve=True)[-1]
    assert serve.background
    assert "opencode serve --hostname 127.0.0.1 --port 4096" in serve.cmd
    assert "--password" not in serve.cmd
    # Attached runs reuse the server env: the Bedrock placeholder must live there.
    assert "AWS_BEARER_TOKEN_BEDROCK=placeholder-not-a-secret exec opencode serve" in serve.cmd
    assert runtime.template_steps() == ()


def test_registry_resolves_opencode() -> None:
    assert isinstance(resolve_runtime("opencode"), OpenCodeRuntime)
