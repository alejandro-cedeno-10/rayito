"""El runner de deepagents (`_runner/deepagents_runner.py`) con deepagents y
LangChain sustituidos por dobles en `sys.modules`: el protocolo JSONL, los
permisos, el uso de tokens, las sesiones y los fallos. La ejecución con el
venv real y un modelo falso es `make agent-runner-test` (Docker arm64)."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import types
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from rayito._agent._runner import deepagents_runner as runner
from rayito._limits import AGENT_PROTOCOL_VERSION, MAX_TOOL_OUTPUT_PREVIEW_BYTES

SESSION_PATTERN = r"rda_[0-9a-f]{32}"


@dataclass
class FakeMessage:
    content: Any = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage_metadata: dict[str, Any] | None = None
    response_metadata: dict[str, Any] = field(default_factory=dict)
    tool_call_id: str = ""
    name: str = ""
    status: str = "success"
    kind: str = "ai"


class FakeGraph:
    def __init__(self, chunks: list[tuple[Any, ...]], error: Exception | None = None) -> None:
        self.chunks = chunks
        self.error = error
        self.inputs: list[Any] = []

    def stream(self, inputs: Any, **kwargs: Any) -> Iterator[tuple[Any, ...]]:
        """Con `subgraphs=True` cada elemento lleva delante su espacio de
        nombres; los de dos elementos son del grafo principal."""
        self.inputs.append((inputs, kwargs))
        assert kwargs.get("subgraphs") is True
        for chunk in self.chunks:
            yield chunk if len(chunk) == 3 else ((), *chunk)
        if self.error is not None:
            raise self.error


@pytest.fixture
def stubs(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Módulos de doble: registran cómo los llama el runner."""
    calls: dict[str, Any] = {"models": [], "deep_agent": None}

    def _module(name: str, **attrs: Any) -> types.ModuleType:
        module = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(module, key, value)
        monkeypatch.setitem(sys.modules, name, module)
        return module

    def _model(cls_name: str) -> type:
        def __init__(self: Any, **kwargs: Any) -> None:
            calls["models"].append((cls_name, kwargs))
            self.kwargs = kwargs

        return type(cls_name, (), {"__init__": __init__})

    class AgentMiddleware:
        pass

    class ToolMessage:
        def __init__(self, **kwargs: Any) -> None:
            self.__dict__.update(kwargs)

    class HumanMessage(FakeMessage):
        def __init__(self, content: str) -> None:
            super().__init__(content=content, kind="human")

    def messages_to_dict(messages: list[Any]) -> list[dict[str, Any]]:
        return [{"kind": m.kind, "content": m.content} for m in messages]

    def messages_from_dict(data: list[dict[str, Any]]) -> list[Any]:
        return [FakeMessage(content=d["content"], kind=d["kind"]) for d in data]

    def create_deep_agent(**kwargs: Any) -> Any:
        calls["deep_agent"] = kwargs
        return calls["graph"]

    _module("langchain_aws", ChatBedrockConverse=_model("ChatBedrockConverse"))
    _module("langchain_anthropic", ChatAnthropic=_model("ChatAnthropic"))
    _module("langchain_openai", ChatOpenAI=_model("ChatOpenAI"))
    _module("langchain_google_genai", ChatGoogleGenerativeAI=_model("ChatGoogleGenerativeAI"))
    _module("langchain")
    _module("langchain.agents")
    _module("langchain.agents.middleware", AgentMiddleware=AgentMiddleware)
    _module("langchain_core")
    _module(
        "langchain_core.messages",
        ToolMessage=ToolMessage,
        HumanMessage=HumanMessage,
        messages_to_dict=messages_to_dict,
        messages_from_dict=messages_from_dict,
    )
    _module("deepagents", create_deep_agent=create_deep_agent)
    _module(
        "deepagents.backends",
        LocalShellBackend=lambda root_dir, virtual_mode: ("backend", root_dir, virtual_mode),
    )
    _module("deepagents.graph", append_prompt_caching_middleware=lambda middleware: "original")
    _module("deepagents.middleware")
    _module(
        "deepagents.middleware.subagents",
        GENERAL_PURPOSE_SUBAGENT={
            "name": "general-purpose",
            "description": "General",
            "system_prompt": "Ayuda.",
        },
    )
    return calls


def _config(tmp_path: Path, **overrides: Any) -> dict[str, Any]:
    config: dict[str, Any] = {
        "v": 1,
        "provider": "bedrock",
        "model": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        "region": "us-east-1",
        "base_url": "http://127.0.0.1:18001",
        "credential_placeholder": "placeholder-not-a-secret",
        "prompt_caching": True,
        "instructions": "Sé breve.",
        "workdir": str(tmp_path),
        "sessions_dir": str(tmp_path / "sessions"),
        "entrypoint": None,
        "permissions": {"default": "allow", "tools": {"execute": {"*": "deny", "git *": "allow"}}},
        "subagents": [],
    }
    config.update(overrides)
    return config


def _request(**overrides: Any) -> dict[str, Any]:
    request: dict[str, Any] = {
        "v": 1,
        "prompt": "hola",
        "session_id": None,
        "model": None,
        "reasoning": False,
    }
    request.update(overrides)
    return request


def _run(config: dict[str, Any], request: dict[str, Any]) -> list[dict[str, Any]]:
    out = io.StringIO()
    assert runner.run(config, request, runner.Emitter(out)) == 0
    return [json.loads(line) for line in out.getvalue().splitlines()]


MODEL_META = {"langgraph_node": "model", "langgraph_checkpoint_ns": "model:1"}


def _two_step_chunks() -> list[tuple[Any, ...]]:
    first = FakeMessage(
        content=[{"type": "text", "text": "voy"}],
        tool_calls=[{"name": "execute", "args": {"command": "ls"}, "id": "c1"}],
        usage_metadata={
            "input_tokens": 5000,
            "output_tokens": 20,
            "input_token_details": {"cache_read": 4096, "cache_creation": 4},
        },
    )
    tool = FakeMessage(
        content="x" * (runner.MAX_TOOL_OUTPUT_BYTES + 10),
        tool_call_id="c1",
        name="execute",
        status="error",
        kind="tool",
    )
    final = FakeMessage(
        content="listo",
        usage_metadata={"input_tokens": 30, "output_tokens": 3},
        response_metadata={"stopReason": "end_turn"},
    )
    return [
        ("messages", (FakeMessage(content="vo"), MODEL_META)),
        (
            "messages",
            (
                FakeMessage(content="sub"),
                {"langgraph_node": "model", "langgraph_checkpoint_ns": "tools:1|model:2"},
            ),
        ),
        ("updates", {"model": {"messages": [first]}}),
        ("updates", {"tools": {"messages": [tool]}, "Middleware.after_model": None}),
        ("updates", {"model": {"messages": [final]}}),
    ]


def test_constants_match_limits() -> None:
    assert runner.PROTOCOL_VERSION == AGENT_PROTOCOL_VERSION
    assert runner.MAX_TOOL_OUTPUT_BYTES == MAX_TOOL_OUTPUT_PREVIEW_BYTES


@pytest.mark.parametrize(
    ("tool", "args", "expected"),
    [
        ("read_file", {}, "deny"),
        ("ls", {}, "allow"),
        ("execute", {"command": "git status"}, "allow"),
        ("execute", {"command": "rm -rf /"}, "deny"),
        ("execute", {}, "deny"),
    ],
)
def test_decide(tool: str, args: dict[str, Any], expected: str) -> None:
    rules = {
        "default": "allow",
        "tools": {"read_file": "deny", "execute": {"*": "deny", "git *": "allow"}},
    }
    assert runner.decide(rules, tool, args) == expected


def test_decide_without_matching_pattern_uses_default() -> None:
    rules = {"default": "deny", "tools": {"execute": {"git *": "allow"}}}
    assert runner.decide(rules, "execute", {"command": "ls"}) == "deny"


def test_run_streams_protocol_and_saves_the_session(stubs: dict[str, Any], tmp_path: Path) -> None:
    stubs["graph"] = FakeGraph(_two_step_chunks())
    events = _run(_config(tmp_path), _request())
    assert [e["type"] for e in events] == [
        "session",
        "step_started",
        "text_delta",
        "text",
        "step_finished",
        "tool_call",
        "step_started",
        "text",
        "step_finished",
        "done",
    ]
    assert all(e["v"] == 1 for e in events)
    session_id = events[0]["session_id"]
    assert __import__("re").fullmatch(SESSION_PATTERN, session_id)
    assert events[4]["usage"] == {
        "input": 900,
        "output": 20,
        "reasoning": 0,
        "cache_read": 4096,
        "cache_write": 4,
    }
    tool = events[5]
    assert (tool["call_id"], tool["status"], tool["input"]) == ("c1", "error", {"command": "ls"})
    assert tool["output_truncated"] is True
    assert len(tool["output"]) == runner.MAX_TOOL_OUTPUT_BYTES
    assert events[8]["finish_reason"] == "end_turn"
    assert events[-1] == {"v": 1, "type": "done", "session_id": session_id}
    saved = json.loads((tmp_path / "sessions" / f"{session_id}.json").read_text())
    assert [m["kind"] for m in saved] == ["human", "ai", "tool", "ai"]
    assert (tmp_path / "sessions" / f"{session_id}.json").stat().st_mode & 0o777 == 0o600

    deep_agent = stubs["deep_agent"]
    assert deep_agent["system_prompt"] == "Sé breve."
    assert deep_agent["backend"] == ("backend", str(tmp_path), False)
    assert len(deep_agent["middleware"]) == 1
    ((_, model_kwargs),) = stubs["models"]
    assert model_kwargs == {
        "model": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        "region_name": "us-east-1",
        "endpoint_url": "http://127.0.0.1:18001",
    }
    _, kwargs = stubs["graph"].inputs[0]
    assert kwargs["stream_mode"] == ["messages", "updates"]
    assert kwargs["config"]["recursion_limit"] == runner.RECURSION_LIMIT

    stubs["graph"] = FakeGraph(
        [("updates", {"model": {"messages": [FakeMessage(content="otra")]}})]
    )
    resumed = _run(_config(tmp_path), _request(session_id=session_id, prompt="sigue"))
    assert resumed[0]["session_id"] == session_id
    history = stubs["graph"].inputs[0][0]["messages"]
    assert [m.content for m in history][-1] == "sigue"
    assert len(history) == 5


def test_reasoning_only_when_requested(stubs: dict[str, Any], tmp_path: Path) -> None:
    message = FakeMessage(
        content=[{"type": "reasoning", "reasoning": "pienso"}, {"type": "text", "text": "ok"}]
    )
    stubs["graph"] = FakeGraph([("updates", {"model": {"messages": [message]}})])
    assert "reasoning" not in [e["type"] for e in _run(_config(tmp_path), _request())]
    stubs["graph"] = FakeGraph([("updates", {"model": {"messages": [message]}})])
    events = _run(_config(tmp_path), _request(reasoning=True))
    assert {"v": 1, "type": "reasoning", "text": "pienso"} in events


@pytest.mark.parametrize(
    ("provider", "cls", "expected"),
    [
        (
            "anthropic",
            "ChatAnthropic",
            {"model": "m", "base_url": "http://gw", "api_key": "placeholder-not-a-secret"},
        ),
        (
            "openai-compatible",
            "ChatOpenAI",
            {"model": "m", "base_url": "http://gw", "api_key": "placeholder-not-a-secret"},
        ),
        (
            "openai",
            "ChatOpenAI",
            {
                "model": "m",
                "base_url": "http://gw",
                "api_key": "placeholder-not-a-secret",
                "use_responses_api": True,
            },
        ),
        (
            "google",
            "ChatGoogleGenerativeAI",
            {"model": "m", "base_url": "http://gw", "google_api_key": "placeholder-not-a-secret"},
        ),
    ],
)
def test_models_go_through_the_gateway(
    stubs: dict[str, Any], provider: str, cls: str, expected: dict[str, Any]
) -> None:
    config = {
        "provider": provider,
        "base_url": "http://gw",
        "credential_placeholder": "placeholder-not-a-secret",
    }
    runner.build_model(config, "m")
    assert stubs["models"] == [(cls, expected)]


def test_prompt_caching_off_replaces_deepagents_helper(
    stubs: dict[str, Any], tmp_path: Path
) -> None:
    stubs["graph"] = FakeGraph([])
    _run(_config(tmp_path, prompt_caching=False), _request())
    assert sys.modules["deepagents.graph"].append_prompt_caching_middleware([]) is None


def test_subagents_get_their_model_and_permissions(stubs: dict[str, Any], tmp_path: Path) -> None:
    stubs["graph"] = FakeGraph([])
    sub = {
        "name": "revisor",
        "description": "Revisa",
        "system_prompt": "Revisa el diff.",
        "model": "modelo-b",
        "permissions": {"default": "deny", "tools": {}},
    }
    _run(_config(tmp_path, subagents=[sub]), _request(model="modelo-c"))
    built, general = stubs["deep_agent"]["subagents"]
    assert (built["name"], built["system_prompt"]) == ("revisor", "Revisa el diff.")
    assert len(built["middleware"]) == 1
    assert general["name"] == "general-purpose"
    assert [kwargs["model"] for _, kwargs in stubs["models"]] == ["modelo-c", "modelo-b"]


def test_permission_middleware_denies_with_a_tool_message(stubs: dict[str, Any]) -> None:
    middleware = runner.permission_middleware({"default": "deny", "tools": {"ls": "allow"}})
    request = types.SimpleNamespace(tool_call={"name": "execute", "args": {}, "id": "c9"})
    denied = middleware.wrap_tool_call(request, lambda r: "ejecutada")
    assert (denied.status, denied.tool_call_id) == ("error", "c9")
    assert denied.content == runner.DENIED_TOOL_MESSAGE
    allowed = types.SimpleNamespace(tool_call={"name": "ls", "args": {}, "id": "c8"})
    assert middleware.wrap_tool_call(allowed, lambda r: "ejecutada") == "ejecutada"


def test_entrypoint_receives_the_context(
    stubs: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "equipo_agentes.py").write_text(
        "SEEN = []\n"
        "def build(ctx):\n"
        "    SEEN.append(ctx)\n"
        "    print('ruido del usuario')\n"
        "    return GRAPH\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    module = __import__("equipo_agentes")
    module.GRAPH = FakeGraph([])  # type: ignore[attr-defined]
    events = _run(_config(tmp_path, entrypoint="equipo_agentes:build"), _request())
    assert events[-1]["type"] == "done"
    (ctx,) = module.SEEN
    assert ctx.workdir == str(tmp_path)
    assert ctx.instructions == "Sé breve."
    assert stubs["deep_agent"] is None
    monkeypatch.delitem(sys.modules, "equipo_agentes")


@pytest.mark.parametrize(
    ("overrides", "request_overrides", "detail"),
    [
        ({"entrypoint": "no_existe_mod:build"}, {}, "entrypoint_not_found"),
        ({}, {"session_id": "rda_" + "0" * 32}, "session_not_found"),
        ({}, {"session_id": "../../etc/passwd"}, "invalid_session_id"),
        ({}, {"v": 2}, "invalid_request"),
    ],
)
def test_runner_errors_are_terminal(
    stubs: dict[str, Any],
    tmp_path: Path,
    overrides: dict[str, Any],
    request_overrides: dict[str, Any],
    detail: str,
) -> None:
    stubs["graph"] = FakeGraph([])
    events = _run(_config(tmp_path, **overrides), _request(**request_overrides))
    assert events[-1]["type"] == "agent_failed"
    assert events[-1]["detail_code"] == detail


def test_model_exceptions_are_classified(stubs: dict[str, Any], tmp_path: Path) -> None:
    botocore_error = type(
        "ThrottlingException", (Exception,), {"__module__": "botocore.errorfactory"}
    )
    stubs["graph"] = FakeGraph([], error=botocore_error("texto del proveedor con sk-123"))
    failed = _run(_config(tmp_path), _request())[-1]
    assert (failed["reason"], failed["detail_code"]) == ("model_error", "ThrottlingException")
    assert "sk-123" not in json.dumps(failed)
    stubs["graph"] = FakeGraph([], error=KeyError("x"))
    failed = _run(_config(tmp_path), _request())[-1]
    assert (failed["reason"], failed["detail_code"]) == ("runtime_error", "KeyError")


def test_main_keeps_stdout_for_the_protocol(tmp_path: Path) -> None:
    """Un proceso de verdad: sin configuración legible, una sola línea de
    protocolo en stdout y nada más."""
    result = subprocess.run(
        [sys.executable, runner.__file__, str(tmp_path / "no-existe.json")],
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


def _denies_execute(middleware: Any) -> bool:
    request = types.SimpleNamespace(
        tool_call={"name": "execute", "args": {"command": "rm x"}, "id": "c"}
    )
    return bool(middleware.wrap_tool_call(request, lambda r: "ran") != "ran")


def test_subagent_without_permissions_inherits_the_main_rules(
    stubs: dict[str, Any], tmp_path: Path
) -> None:
    stubs["graph"] = FakeGraph([])
    sub = {
        "name": "ayudante",
        "description": "Ayuda",
        "system_prompt": "Ayuda.",
        "permissions": None,
    }
    _run(_config(tmp_path, subagents=[sub]), _request())
    built, general = stubs["deep_agent"]["subagents"]
    assert built["name"] == "ayudante"
    assert _denies_execute(built["middleware"][0])
    assert general["name"] == "general-purpose"
    assert (general["description"], general["system_prompt"]) == ("General", "Ayuda.")
    assert _denies_execute(general["middleware"][0])


def test_user_general_purpose_subagent_replaces_the_default(
    stubs: dict[str, Any], tmp_path: Path
) -> None:
    stubs["graph"] = FakeGraph([])
    sub = {
        "name": "general-purpose",
        "description": "Mío",
        "system_prompt": "Mío.",
        "permissions": None,
    }
    _run(_config(tmp_path, subagents=[sub]), _request())
    (built,) = stubs["deep_agent"]["subagents"]
    assert built["description"] == "Mío"


def test_subagent_usage_is_added_to_the_next_main_step(
    stubs: dict[str, Any], tmp_path: Path
) -> None:
    first = FakeMessage(
        content="",
        tool_calls=[{"name": "task", "args": {"subagent_type": "general-purpose"}, "id": "t1"}],
        usage_metadata={"input_tokens": 10, "output_tokens": 1},
    )
    nested = FakeMessage(
        content="hecho",
        usage_metadata={
            "input_tokens": 100,
            "output_tokens": 7,
            "input_token_details": {"cache_read": 40},
        },
    )
    tool = FakeMessage(content="hecho", tool_call_id="t1", name="task", kind="tool")
    final = FakeMessage(content="listo", usage_metadata={"input_tokens": 20, "output_tokens": 2})
    stubs["graph"] = FakeGraph(
        [
            ("updates", {"model": {"messages": [first]}}),
            (
                ("tools:1",),
                "messages",
                (nested, {"langgraph_node": "model", "langgraph_checkpoint_ns": "tools:1|model:2"}),
            ),
            (("tools:1",), "updates", {"model": {"messages": [nested]}}),
            ("updates", {"tools": {"messages": [tool]}}),
            ("updates", {"model": {"messages": [final]}}),
        ]
    )
    events = _run(_config(tmp_path), _request())
    usages = [event["usage"] for event in events if event["type"] == "step_finished"]
    assert [(u["input"], u["output"], u["cache_read"]) for u in usages] == [(10, 1, 0), (80, 9, 40)]
    assert all(event.get("text") != "hecho" for event in events if event["type"] == "text_delta")
