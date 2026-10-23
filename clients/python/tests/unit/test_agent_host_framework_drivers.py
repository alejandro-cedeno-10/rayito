"""Drivers de frameworks del host del agente (`strands`, `strands-ts`,
`hermes`, `callable` y `command`; `agent-runtimes-byo`, ADR-028): cada uno
contra módulos de doble en `sys.modules` o contra procesos de doble reales,
y los traductores contra los eventos grabados de Strands y de Hermes
(`testdata/agent/`). El host se prueba una vez, aquí, en Python."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import threading
import types
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from rayito._agent._runner.agent_host.drivers import (
    _common,
    hermes,
    node_callable,
    strands,
    strands_ts,
)
from rayito._agent._runner.agent_host.drivers import (
    callable as callable_driver,
)
from rayito._agent._runner.agent_host.drivers import (
    command as command_driver,
)
from rayito._agent._runner.agent_host.protocol import AgentFailure, Request, RunState

TESTDATA = Path(__file__).parents[4] / "testdata" / "agent"
HOST_DIR = Path(strands.__file__).parents[1]
SESSION_ID = "rda_0000000000000000000000000000000a"
EXPECTED_STRANDS = json.loads((TESTDATA / "strands-expected-events.json").read_text("utf-8"))
EXPECTED_HERMES = json.loads((TESTDATA / "hermes-expected-events.json").read_text("utf-8"))


USER_MODULES = ("mi_funcion", "mi_agente", "rota", "no_existe", "nada")


@pytest.fixture(autouse=True)
def _forget_user_modules() -> Iterator[None]:
    for name in USER_MODULES:
        sys.modules.pop(name, None)
    yield
    for name in USER_MODULES:
        sys.modules.pop(name, None)


def _jsonl(name: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (TESTDATA / name).read_text("utf-8").splitlines() if line]


def _request(prompt: str = "hola", **fields: Any) -> Request:
    return Request(prompt=prompt, **fields)


def _collect(driver: Any, request: Request) -> tuple[list[dict[str, Any]], RunState]:
    events: list[dict[str, Any]] = []
    state = RunState()
    driver.run(request, events.append, state)
    return events, state


# ---- Strands: traductor y construcción del modelo ----


@pytest.mark.parametrize("reasoning", [False, True])
def test_strands_translator_matches_the_recorded_events(reasoning: bool) -> None:
    translator = strands.Translator(reasoning=reasoning)
    events = [e for item in _jsonl("strands-python-events.jsonl") for e in translator.feed(item)]
    assert events == EXPECTED_STRANDS["reasoning" if reasoning else "plain"]


def test_strands_translator_truncates_a_huge_tool_output() -> None:
    translator = strands.Translator(reasoning=False)
    huge = "x" * 70000
    translator.feed(
        {
            "message": {
                "role": "assistant",
                "content": [{"toolUse": {"toolUseId": "t", "name": "n", "input": {}}}],
            }
        }
    )
    [call] = translator.feed(
        {
            "message": {
                "role": "user",
                "content": [
                    {
                        "toolResult": {
                            "toolUseId": "t",
                            "status": "error",
                            "content": [{"text": huge}],
                        }
                    }
                ],
            }
        }
    )
    assert call["status"] == "error" and call["output_truncated"] is True
    assert len(call["output"]) == 65536


def _fake_modules(monkeypatch: pytest.MonkeyPatch, **classes: tuple[str, str, type]) -> None:
    for module_name, _attr, cls in classes.values():
        module = sys.modules.get(module_name) or types.ModuleType(module_name)
        setattr(module, cls.__name__, cls)
        monkeypatch.setitem(sys.modules, module_name, module)


class _Recorder:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


def _model_classes(monkeypatch: pytest.MonkeyPatch) -> None:
    for module_name, attr in (
        ("strands.models.bedrock", "BedrockModel"),
        ("strands.models.anthropic", "AnthropicModel"),
        ("strands.models.openai", "OpenAIModel"),
        ("strands.models.gemini", "GeminiModel"),
    ):
        module = types.ModuleType(module_name)
        setattr(module, attr, type(attr, (_Recorder,), {}))
        monkeypatch.setitem(sys.modules, module_name, module)
    model_module = types.ModuleType("strands.models.model")
    model_module.CacheConfig = type("CacheConfig", (_Recorder,), {})  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "strands.models.model", model_module)


GATEWAY = {
    "base_url": "http://127.0.0.1:18001/v1",
    "credential_placeholder": "placeholder-not-a-secret",
}


def test_strands_models_go_through_the_gateway_with_the_placeholder_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _model_classes(monkeypatch)
    bedrock = strands.build_model(
        {"provider": "bedrock", "region": "us-east-1", "prompt_caching": True, **GATEWAY}, "m"
    )
    assert bedrock.kwargs["endpoint_url"] == GATEWAY["base_url"]
    assert bedrock.kwargs["api_key"] == GATEWAY["credential_placeholder"]
    assert bedrock.kwargs["cache_config"].kwargs == {"strategy": "auto"}
    no_cache = strands.build_model(
        {"provider": "bedrock", "region": "us-east-1", "prompt_caching": False, **GATEWAY}, "m"
    )
    assert "cache_config" not in no_cache.kwargs
    anthropic = strands.build_model({"provider": "anthropic", **GATEWAY}, "m")
    assert anthropic.kwargs["client_args"] == {
        "base_url": GATEWAY["base_url"],
        "api_key": GATEWAY["credential_placeholder"],
    }
    assert anthropic.kwargs["max_tokens"] == strands.ANTHROPIC_MAX_TOKENS
    for provider in ("openai", "openai-compatible"):
        openai = strands.build_model({"provider": provider, **GATEWAY}, "m")
        assert openai.kwargs["client_args"]["base_url"] == GATEWAY["base_url"]
    google = strands.build_model({"provider": "google", **GATEWAY}, "m")
    assert google.kwargs["client_args"] == {
        "api_key": GATEWAY["credential_placeholder"],
        "http_options": {"base_url": GATEWAY["base_url"]},
    }
    with pytest.raises(AgentFailure, match="unknown_provider"):
        strands.build_model({"provider": "azure", **GATEWAY}, "m")


def test_without_a_gateway_the_client_keeps_its_standard_endpoint_and_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _model_classes(monkeypatch)
    config = {"base_url": None, "credential_placeholder": None, "prompt_caching": False}
    assert (
        "endpoint_url"
        not in strands.build_model(
            {"provider": "bedrock", "region": "us-east-1", **config}, "m"
        ).kwargs
    )
    assert strands.build_model({"provider": "openai", **config}, "m").kwargs["client_args"] is None


# ---- Strands: agente, permisos y sesiones con un `strands` de doble ----


class _Hooks:
    def __init__(self) -> None:
        self.callbacks: list[tuple[type, Callable[[Any], None]]] = []

    def add_callback(self, event_type: type, callback: Callable[[Any], None]) -> None:
        self.callbacks.append((event_type, callback))


class _FakeAgent:
    """Un `Agent` de Strands de doble: reproduce los eventos grabados."""

    created: list[_FakeAgent] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.messages: list[Any] = []
        self.hooks = _Hooks()
        self.prompts: list[str] = []
        _FakeAgent.created.append(self)

    async def stream_async(self, prompt: str, **kwargs: Any) -> Any:
        self.prompts.append(prompt)
        assert "cancel_signal" in kwargs
        self.messages.append({"role": "user", "content": [{"text": prompt}]})
        for item in _jsonl("strands-python-events.jsonl"):
            yield item
        self.messages.append({"role": "assistant", "content": [{"text": "listo"}]})


class _BeforeToolCallEvent:
    def __init__(self, name: str, arguments: dict[str, Any]) -> None:
        self.tool_use = {"name": name, "input": arguments}
        self.cancel_tool: bool | str = False


@pytest.fixture
def fake_strands(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    _FakeAgent.created.clear()
    _model_classes(monkeypatch)
    package = types.ModuleType("strands")
    package.Agent = _FakeAgent  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "strands", package)
    hooks = types.ModuleType("strands.hooks")
    hooks.BeforeToolCallEvent = _BeforeToolCallEvent  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "strands.hooks", hooks)
    harness_calls: list[dict[str, Any]] = []
    harness = types.ModuleType("strands_harness")

    def create_harness(**kwargs: Any) -> _FakeAgent:
        harness_calls.append(kwargs)
        return _FakeAgent(**kwargs)

    harness.create_harness = create_harness  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "strands_harness", harness)
    return {"tmp": tmp_path, "harness_calls": harness_calls}


def _strands_config(tmp: Path, **overrides: Any) -> dict[str, Any]:
    return {
        "v": 1,
        "provider": "openai-compatible",
        "client": "OpenAIModel",
        "model": "modelo-a",
        "region": None,
        **GATEWAY,
        "prompt_caching": True,
        "instructions": "Eres útil.",
        "workdir": str(tmp / "work"),
        "app_dir": str(tmp / "app"),
        "state_dir": str(tmp / "state"),
        "sessions_dir": str(tmp / "state" / "sessions"),
        "memory_dir": str(tmp / "state" / "memory"),
        "entrypoint": None,
        "harness": False,
        "builtin_tools": None,
        "permission_aliases": {},
        "permissions": {"default": "allow", "tools": {}},
        **overrides,
    }


def test_strands_driver_runs_the_default_agent_and_persists_the_session(
    fake_strands: dict[str, Any],
) -> None:
    config = _strands_config(fake_strands["tmp"])
    driver = strands.create(config)
    events, state = _collect(driver, _request("suma 2 y 3"))
    assert events[0] == {"v": 1, "type": "session", "session_id": state.session_id}
    assert [e["type"] for e in events[1:]] == [e["type"] for e in EXPECTED_STRANDS["plain"]]
    agent = _FakeAgent.created[0]
    assert agent.kwargs["tools"] == [] and agent.kwargs["callback_handler"] is None
    assert agent.kwargs["system_prompt"] == "Eres útil."
    saved = Path(config["sessions_dir"]) / f"{state.session_id}.json"
    assert oct(saved.stat().st_mode & 0o777) == "0o600"
    resumed_events, resumed = _collect(driver, _request("otra", session_id=state.session_id))
    assert resumed.session_id == state.session_id
    assert resumed_events[0]["session_id"] == state.session_id


def test_strands_driver_resumes_with_the_saved_history_and_rejects_unknown_sessions(
    fake_strands: dict[str, Any],
) -> None:
    config = _strands_config(fake_strands["tmp"])
    driver = strands.create(config)
    _, first = _collect(driver, _request("uno"))
    history_before = json.loads(
        (Path(config["sessions_dir"]) / f"{first.session_id}.json").read_text()
    )
    _collect(driver, _request("dos", session_id=first.session_id))
    assert _FakeAgent.created[1].messages[: len(history_before)] == history_before
    with pytest.raises(AgentFailure, match="session_not_found"):
        _collect(driver, _request("tres", session_id="rda_" + "1" * 32))
    with pytest.raises(AgentFailure, match="invalid_session_id"):
        _collect(driver, _request("cuatro", session_id="otra-forma"))


def test_strands_harness_gets_the_safe_builtin_tools_and_state_dirs(
    fake_strands: dict[str, Any],
) -> None:
    tools = {"*": False, "shell": True, "programmatic_tool_caller": False, "web_search": False}
    config = _strands_config(
        fake_strands["tmp"],
        harness=True,
        builtin_tools=tools,
        provider="bedrock",
        region="us-east-1",
    )
    events, state = _collect(strands.create(config), _request())
    [call] = fake_strands["harness_calls"]
    assert call["builtin_tools"] == tools
    assert call["session"] == {"id": state.session_id, "dir": config["sessions_dir"]}
    assert call["memory"] == {"dir": config["memory_dir"]}
    assert call["callback_handler"] is None
    assert not (Path(config["sessions_dir"]) / f"{state.session_id}.json").exists()
    assert events[-1]["type"] == "step_finished"


def test_the_permission_hook_denies_tools_and_applies_command_patterns(
    fake_strands: dict[str, Any],
) -> None:
    rules = {
        "default": "allow",
        "tools": {"bash": {"*": "deny", "git *": "allow"}, "edit": "deny"},
    }
    agent = _FakeAgent()
    strands.register_permissions(agent, rules, {"shell": "bash", "write": "edit"})
    strands.register_permissions(agent, rules, {"shell": "bash", "write": "edit"})
    assert len(agent.hooks.callbacks) == 1
    callback = agent.hooks.callbacks[0][1]

    def verdict(name: str, arguments: dict[str, Any]) -> bool | str:
        event = _BeforeToolCallEvent(name, arguments)
        callback(event)
        return event.cancel_tool

    assert verdict("shell", {"command": "git status"}) is False
    assert verdict("shell", {"command": "rm -rf x"}) == _common.DENIED_TOOL_MESSAGE
    assert verdict("write", {"path": "a"}) == _common.DENIED_TOOL_MESSAGE
    assert verdict("read", {"path": "a"}) is False


def test_strands_entrypoint_instance_and_factory(
    fake_strands: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    app = fake_strands["tmp"] / "app"
    app.mkdir()
    (app / "mi_agente.py").write_text(
        textwrap.dedent(
            """
            from strands import Agent
            agent = Agent(name="instancia")
            def build(ctx):
                return Agent(model=ctx.model, name="fabrica", instructions=ctx.instructions)
            """
        )
    )
    monkeypatch.syspath_prepend(str(app))
    config = _strands_config(fake_strands["tmp"], entrypoint="mi_agente:build")
    _collect(strands.create(config), _request())
    assert _FakeAgent.created[-1].kwargs["name"] == "fabrica"
    assert _FakeAgent.created[-1].kwargs["model"].kwargs["model_id"] == "modelo-a"
    instance = config | {"entrypoint": "mi_agente:agent"}
    _collect(strands.create(instance), _request())
    assert _FakeAgent.created[-2].kwargs["name"] == "instancia"
    missing = config | {"entrypoint": "no_existe:build"}
    with pytest.raises(AgentFailure, match="entrypoint_not_found"):
        _collect(strands.create(missing), _request())


def test_strands_warm_fails_on_an_entrypoint_that_does_not_exist(
    fake_strands: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    app = fake_strands["tmp"] / "app"
    app.mkdir()
    (app / "agente_warm.py").write_text("def build(ctx):\n    return None\n")
    config = _strands_config(fake_strands["tmp"], entrypoint="agente_warm:build")

    strands.create(config).warm()
    with pytest.raises(AgentFailure, match="entrypoint_not_found"):
        strands.create(config | {"entrypoint": "agente_warm:no_existe"}).warm()
    with pytest.raises(AgentFailure, match="entrypoint_not_found"):
        strands.create(config | {"entrypoint": "modulo_que_no_existe:build"}).warm()
    monkeypatch.delitem(sys.modules, "agente_warm")


def test_strands_serves_concurrent_sessions_of_a_shared_agent_one_at_a_time(
    fake_strands: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    app = fake_strands["tmp"] / "app"
    app.mkdir()
    (app / "mi_agente.py").write_text("from strands import Agent\nagent = Agent()\n")
    monkeypatch.syspath_prepend(str(app))
    active: list[int] = []
    overlaps: list[int] = []
    starts: list[list[Any]] = []

    async def exclusive(self: Any, prompt: str, **kwargs: Any) -> AsyncIterator[Any]:
        if active:
            overlaps.append(len(active))
        active.append(1)
        starts.append(list(self.messages))
        await asyncio.sleep(0.05)
        self.messages.append({"role": "user", "content": [{"text": prompt}]})
        active.pop()
        yield {}

    monkeypatch.setattr(_FakeAgent, "stream_async", exclusive)
    driver = strands.create(_strands_config(fake_strands["tmp"], entrypoint="mi_agente:agent"))
    threads = [
        threading.Thread(target=_collect, args=(driver, _request(f"sesion {n}"))) for n in range(3)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert overlaps == []
    assert starts == [[], [], []]


def test_strands_model_errors_are_classified_by_origin_without_the_message(
    fake_strands: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    boom = type("ClientError", (Exception,), {"__module__": "botocore.exceptions"})

    async def explode(self: Any, prompt: str, **kwargs: Any) -> AsyncIterator[Any]:
        if prompt:
            raise RuntimeError("detalle con el prompt secreto") from boom("clave sk-123")
        yield {}

    monkeypatch.setattr(_FakeAgent, "stream_async", explode)
    with pytest.raises(AgentFailure) as raised:
        _collect(strands.create(_strands_config(fake_strands["tmp"])), _request())
    assert raised.value.reason == "model_error"
    assert raised.value.detail_code == "runtimeerror"
    assert "secreto" not in str(raised.value) and "sk-123" not in str(raised.value)

    async def plain(self: Any, prompt: str, **kwargs: Any) -> AsyncIterator[Any]:
        if prompt:
            raise ValueError("x")
        yield {}

    monkeypatch.setattr(_FakeAgent, "stream_async", plain)
    with pytest.raises(AgentFailure) as other:
        _collect(strands.create(_strands_config(fake_strands["tmp"])), _request())
    assert other.value.reason == "runtime_error"


def test_strands_warm_imports_the_framework(fake_strands: dict[str, Any]) -> None:
    strands.create(_strands_config(fake_strands["tmp"], harness=True)).warm()


def test_strands_cancel_sets_the_cancel_signal(fake_strands: dict[str, Any]) -> None:
    driver = strands.create(_strands_config(fake_strands["tmp"]))
    driver.cancel()
    assert driver._cancel.is_set()


def test_the_node_bridge_shares_its_constants_with_the_host() -> None:
    """El puente (JavaScript, sin `rayito`) repite cuatro valores del host:
    un cambio en uno sin el otro rompe esta prueba."""
    from rayito._agent._runner.agent_host import protocol

    source = (Path(strands.__file__).parents[3] / "_assets" / "node_bridge.mjs").read_text("utf-8")

    def constant(name: str) -> str:
        match = re.search(rf"export const {name} = (.+);", source)
        assert match is not None, name
        return match.group(1)

    assert int(constant("PROTOCOL_VERSION")) == protocol.PROTOCOL_VERSION
    assert int(constant("MAX_TOOL_OUTPUT_BYTES")) == protocol.MAX_TOOL_OUTPUT_BYTES
    assert int(constant("ANTHROPIC_MAX_TOKENS")) == strands.ANTHROPIC_MAX_TOKENS
    assert json.loads(constant("DENIED_TOOL_MESSAGE")) == _common.DENIED_TOOL_MESSAGE


# ---- Hermes ----


@pytest.mark.parametrize(
    ("scenario", "run_id", "reasoning"),
    [
        ("ok", "run_00000000000000000000000000000001", False),
        ("ok_reasoning", "run_00000000000000000000000000000001", True),
        ("failed", "run_00000000000000000000000000000002", False),
    ],
)
def test_hermes_translator_matches_the_recorded_events(
    scenario: str, run_id: str, reasoning: bool
) -> None:
    translator = hermes.Translator(reasoning=reasoning)
    events: list[dict[str, Any]] = []
    for item in _jsonl("hermes-v0.22.2-events.jsonl"):
        if item["run_id"] == run_id:
            events += translator.feed(item)
            if translator.terminal:
                break
    assert events == EXPECTED_HERMES[scenario]
    if scenario == "failed":
        assert translator.failure is not None
        assert {
            "reason": translator.failure.reason,
            "detail_code": translator.failure.detail_code,
        } == EXPECTED_HERMES["failure"]
        assert "Checksum" not in str(translator.failure)


def test_hermes_translator_maps_cancelled_and_interim_events() -> None:
    translator = hermes.Translator(reasoning=False)
    assert translator.feed(
        {"event": "message.interim", "text": "pensando", "already_streamed": False}
    ) == [{"v": 1, "type": "text_delta", "text": "pensando"}]
    assert (
        translator.feed({"event": "message.interim", "text": "x", "already_streamed": True}) == []
    )
    translator.feed({"event": "run.cancelled"})
    assert translator.failure is not None and translator.failure.detail_code == "run_cancelled"


def _hermes_config(tmp: Path, **overrides: Any) -> dict[str, Any]:
    return {
        "v": 1,
        "provider": "openai-compatible",
        "client": "custom",
        "model": "modelo-a",
        "region": None,
        **GATEWAY,
        "workdir": str(tmp / "work"),
        "state_dir": str(tmp / "state"),
        "home_dir": str(tmp / "home"),
        "toolsets": ["terminal", "file"],
        "skills_dir": None,
        "python": sys.executable,
        "source_dir": str(tmp / "src"),
        "api_port": 0,
        **overrides,
    }


def test_hermes_config_has_the_measured_keys_and_everything_external_off(tmp_path: Path) -> None:
    document = hermes.hermes_config(_hermes_config(tmp_path, skills_dir="/opt/agents/app/skills"))
    assert document["model"] == {
        "provider": "custom",
        "default": "modelo-a",
        "base_url": GATEWAY["base_url"],
        "api_key": GATEWAY["credential_placeholder"],
    }
    assert document["platform_toolsets"] == {"api_server": ["terminal", "file"]}
    assert document["skills"] == {
        "creation_nudge_interval": 0,
        "external_dirs": ["/opt/agents/app/skills"],
    }
    assert document["updates"] == {"check": False}
    assert document["telemetry"]["shared_metrics"] == {"enabled": False, "send": False}
    assert document["auth"] == {"adopt_external_logins": False}
    assert document["auxiliary"]["title_generation"] == {"enabled": False}
    spike = json.loads((TESTDATA / "hermes-v0.22.2-spike.json").read_text("utf-8"))
    assert set(spike["config"]["off_switches"]) == {
        "updates.check",
        "telemetry.shared_metrics.enabled",
        "telemetry.shared_metrics.send",
        "auth.adopt_external_logins",
        "auxiliary.title_generation.enabled",
    }
    bedrock = hermes.hermes_config(
        _hermes_config(
            tmp_path, client="bedrock", provider="bedrock", region="us-east-1", base_url=None
        )
    )
    assert bedrock["model"] == {"provider": "bedrock", "default": "modelo-a"}
    assert bedrock["bedrock"] == {"region": "us-east-1"}


def test_hermes_home_is_private_and_the_child_env_carries_the_key_only_there(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "placeholder-not-a-secret")
    config = _hermes_config(tmp_path)
    hermes.write_home(config)
    home = tmp_path / "home"
    assert oct(home.stat().st_mode & 0o777) == "0o700"
    assert oct((home / "config.yaml").stat().st_mode & 0o777) == "0o600"
    assert json.loads((home / "config.yaml").read_text())["model"]["provider"] == "custom"
    driver = hermes.create(config)
    env = hermes.child_env(config, driver._api_key)
    assert len(env["API_SERVER_KEY"]) >= 16
    assert env["API_SERVER_HOST"] == "127.0.0.1"
    assert env["HERMES_IGNORE_USER_CONFIG"] == "1" and env["TERMINAL_ENV"] == "local"
    assert env["HERMES_HOME"] == str(home) and env["PYTHONPATH"] == config["source_dir"]
    assert env["ANTHROPIC_API_KEY"] == "placeholder-not-a-secret"
    assert driver._api_key not in (home / "config.yaml").read_text()
    assert hermes.create(config)._api_key != driver._api_key


def _free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def fake_hermes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Callable[[str], dict[str, Any]]]:
    source = tmp_path / "src" / "hermes_cli"
    source.mkdir(parents=True)
    (source / "__init__.py").write_text("")
    shutil.copy(Path(__file__).with_name("fake_hermes_gateway.py"), source / "main.py")
    (tmp_path / "work").mkdir()
    drivers: list[Any] = []

    def prepare(scenario: str, **overrides: Any) -> dict[str, Any]:
        run_id = {
            "ok": "run_00000000000000000000000000000001",
            "failed": "run_00000000000000000000000000000002",
        }[scenario]
        events = [e for e in _jsonl("hermes-v0.22.2-events.jsonl") if e["run_id"] == run_id]
        (tmp_path / "events.json").write_text(json.dumps(events))
        monkeypatch.setenv("FAKE_HERMES_LOG", str(tmp_path / "log.json"))
        monkeypatch.setenv("FAKE_HERMES_EVENTS", str(tmp_path / "events.json"))
        config = _hermes_config(tmp_path, api_port=_free_port(), **overrides)
        driver = hermes.create(config)
        drivers.append(driver)
        return {"config": config, "driver": driver, "log": tmp_path / "log.json"}

    yield prepare
    for driver in drivers:
        driver.cancel()


def test_hermes_driver_runs_a_task_against_a_fake_gateway(
    fake_hermes: Callable[[str], dict[str, Any]],
) -> None:
    setup = fake_hermes("ok")
    events, state = _collect(setup["driver"], _request("el prompt secreto", session_id=SESSION_ID))
    assert state.session_id == SESSION_ID
    assert events[0] == {"v": 1, "type": "session", "session_id": SESSION_ID}
    assert events[1] == {"v": 1, "type": "step_started", "index": 1}
    assert events[2:] == EXPECTED_HERMES["ok"]
    record = json.loads(setup["log"].read_text())
    assert record["host"] == "127.0.0.1"
    assert record["argv"][0].endswith("main.py") or "hermes_cli" in record["argv"][0]
    assert record["bodies"] == [{"input": "el prompt secreto", "session_id": SESSION_ID}]
    assert "el prompt secreto" not in " ".join(record["argv"])
    assert record["cwd"] == setup["config"]["workdir"]
    assert record["home"] == setup["config"]["home_dir"]


def test_hermes_driver_sends_the_instructions_as_the_ephemeral_system_prompt(
    tmp_path: Path, fake_hermes: Callable[..., dict[str, Any]]
) -> None:
    setup = fake_hermes("ok", instructions="Responde en español.")
    _collect(setup["driver"], _request("hola", session_id=SESSION_ID))
    record = json.loads(setup["log"].read_text())
    assert record["bodies"] == [
        {"input": "hola", "session_id": SESSION_ID, "instructions": "Responde en español."}
    ]


def test_hermes_driver_reports_a_failed_run_without_the_provider_text(
    fake_hermes: Callable[[str], dict[str, Any]],
) -> None:
    setup = fake_hermes("failed")
    with pytest.raises(AgentFailure) as raised:
        _collect(setup["driver"], _request())
    assert raised.value.reason == EXPECTED_HERMES["failure"]["reason"]
    assert raised.value.detail_code == EXPECTED_HERMES["failure"]["detail_code"]


def test_hermes_driver_fails_clearly_when_the_gateway_never_answers(tmp_path: Path) -> None:
    (tmp_path / "work").mkdir()
    (tmp_path / "src" / "hermes_cli").mkdir(parents=True)
    (tmp_path / "src" / "hermes_cli" / "__init__.py").write_text("")
    (tmp_path / "src" / "hermes_cli" / "main.py").write_text("raise SystemExit(3)")
    driver = hermes.create(_hermes_config(tmp_path, api_port=_free_port()))
    with pytest.raises(AgentFailure, match="hermes_exited"):
        driver.warm()


# ---- callable ----


def _callable_config(tmp: Path, **overrides: Any) -> dict[str, Any]:
    return {
        "entrypoint": "mi_funcion:responde",
        "app_dir": str(tmp),
        "workdir": str(tmp),
        **overrides,
    }


@pytest.mark.parametrize(
    ("body", "texts", "text"),
    [
        ("return 'hola ' + request['prompt']", [], "hola mundo"),
        ("return ['a', '', 'b']", ["a", "b"], "ab"),
        ("return {'text': 'listo', 'usage': {'input': 7, 'output': 3}}", [], "listo"),
    ],
)
def test_callable_driver_translates_what_the_function_returns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str, texts: list[str], text: str
) -> None:
    (tmp_path / "mi_funcion.py").write_text(f"def responde(request):\n    {body}\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    events, state = _collect(callable_driver.create(_callable_config(tmp_path)), _request("mundo"))
    assert state.session_id and state.session_id.startswith("rda_")
    assert [e["text"] for e in events if e["type"] == "text_delta"] == texts
    assert [e["text"] for e in events if e["type"] == "text"] == [text]
    assert events[-1]["type"] == "step_finished"
    if "usage" in body:
        assert events[-1]["usage"]["input"] == 7


def test_callable_driver_supports_async_functions_and_async_generators(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "mi_funcion.py").write_text(
        textwrap.dedent(
            """
            import asyncio
            async def responde(request):
                await asyncio.sleep(0)
                return 'asincrona'
            async def flujo(request):
                for trozo in ('uno ', 'dos'):
                    yield trozo
            """
        )
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    events, _ = _collect(callable_driver.create(_callable_config(tmp_path)), _request())
    assert [e["text"] for e in events if e["type"] == "text"] == ["asincrona"]
    streamed, _ = _collect(
        callable_driver.create(_callable_config(tmp_path, entrypoint="mi_funcion:flujo")),
        _request(),
    )
    assert [e["text"] for e in streamed if e["type"] == "text_delta"] == ["uno ", "dos"]


def test_callable_driver_rejects_bad_returns_and_missing_entrypoints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "mi_funcion.py").write_text("def responde(request):\n    return 42\nvalor = 1\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    with pytest.raises(AgentFailure, match="invalid_response"):
        _collect(callable_driver.create(_callable_config(tmp_path)), _request())
    with pytest.raises(AgentFailure, match="invalid_entrypoint"):
        _collect(
            callable_driver.create(_callable_config(tmp_path, entrypoint="mi_funcion:valor")),
            _request(),
        )
    with pytest.raises(AgentFailure, match="entrypoint_not_found"):
        callable_driver.create(_callable_config(tmp_path, entrypoint="nada:nada")).warm()


# ---- command ----

V1_CHILD = """
import json, sys
request = json.loads(sys.stdin.readline())
out = lambda **e: print(json.dumps({"v": 1, **e}), flush=True)
out(type="step_started", index=1)
out(type="text", text="eco: " + request["prompt"])
out(type="step_finished", index=1, usage={"input": 1, "output": 2}, finish_reason="stop")
print("esto no es JSON", flush=True)
out(type="done", session_id=request["session_id"])
"""


def _command_config(script: str, tmp: Path, **overrides: Any) -> dict[str, Any]:
    (tmp / "hijo.py").write_text(textwrap.dedent(script))
    return {"command": [sys.executable, str(tmp / "hijo.py")], "workdir": str(tmp), **overrides}


def test_command_driver_relays_protocol_v1_lines(tmp_path: Path) -> None:
    driver = command_driver.create(_command_config(V1_CHILD, tmp_path))
    events, state = _collect(driver, _request("hola"))
    assert [e["type"] for e in events] == ["session", "step_started", "text", "step_finished"]
    assert events[2]["text"] == "eco: hola"
    assert state.session_id and state.session_id.startswith("rda_")


def test_command_driver_maps_failures_and_exit_codes(tmp_path: Path) -> None:
    failing = """
    import json, sys
    sys.stdin.readline()
    failure = {"v": 1, "type": "agent_failed", "reason": "model_error"}
    failure["detail_code"] = "Throttled Error!"
    print(json.dumps(failure), flush=True)
    """
    with pytest.raises(AgentFailure) as raised:
        _collect(command_driver.create(_command_config(failing, tmp_path)), _request())
    assert (raised.value.reason, raised.value.detail_code) == ("model_error", "throttled_error_")
    with pytest.raises(AgentFailure, match="exit_nonzero"):
        _collect(
            command_driver.create(_command_config("import sys; sys.exit(3)", tmp_path)), _request()
        )
    with pytest.raises(AgentFailure, match="no_done"):
        _collect(
            command_driver.create(_command_config("import sys; sys.stdin.read()", tmp_path)),
            _request(),
        )
    with pytest.raises(AgentFailure, match="invalid_protocol"):
        command_driver.create({"command": ["x"], "protocol": "otro"})


def test_command_driver_text_protocol_wraps_stdout_and_exports_the_request(tmp_path: Path) -> None:
    child = """
    import os, sys
    env = os.environ
    print("sesion", env["RAYITO_SESSION_ID"][:4], env["RAYITO_REASONING"], env.get("RAYITO_MODEL"))
    print("prompt:", sys.stdin.read())
    """
    driver = command_driver.create(_command_config(child, tmp_path, protocol="text"))
    events, _ = _collect(driver, _request("pregunta", model="otro-modelo", reasoning=True))
    text = next(e["text"] for e in events if e["type"] == "text")
    assert text == "sesion rda_ 1 otro-modelo\nprompt: pregunta\n"
    assert events[1]["type"] == "step_started" and events[-1]["type"] == "step_finished"


def test_the_prompt_never_reaches_the_command_argv(tmp_path: Path) -> None:
    child = """
    import json, sys
    request = json.loads(sys.stdin.readline())
    print(json.dumps({"v": 1, "type": "text", "text": " ".join(sys.argv)}), flush=True)
    print(json.dumps({"v": 1, "type": "done"}), flush=True)
    """
    driver = command_driver.create(_command_config(child, tmp_path))
    events, _ = _collect(driver, _request("clave-secreta"))
    assert "clave-secreta" not in events[-1]["text"]


# ---- node-callable (y su alias strands-ts) ----


@pytest.mark.parametrize("module", [node_callable, strands_ts])
def test_node_callable_driver_is_the_command_driver_over_the_node_bridge(
    tmp_path: Path, module: Any
) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "bridge": "/opt/agents/rayito/node_bridge.mjs",
                "adapter": "langgraph",
                "workdir": "/home/user",
            }
        )
    )
    driver = module.create({"config_path": str(config_path)})
    assert isinstance(driver, command_driver.Driver)
    assert driver._config["command"] == [
        "node",
        "/opt/agents/rayito/node_bridge.mjs",
        str(config_path),
    ]
    assert driver._config["protocol"] == "rayito-v1"
    assert driver._config["workdir"] == "/home/user"
    with pytest.raises(AgentFailure, match="invalid_input"):
        module.create({"bridge": "x"})


def _bridge_driver(tmp_path: Path, entrypoint: str | None) -> Any:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "bridge": str(tmp_path / "bridge.py"),
                "adapter": "strands",
                "workdir": str(tmp_path),
                "entrypoint": entrypoint,
            }
        )
    )
    return node_callable.create({"config_path": str(config_path)})


def test_node_callable_warm_checks_the_entrypoint_with_the_bridge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`warm()` lanza el puente con `--check-entry`; un `agent_failed` suyo es
    el fallo del arranque (aquí el puente es un doble en Python)."""
    calls: list[list[str]] = []

    def run(argv: list[str], **_: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append(argv)
        failed = b'{"v":1,"type":"agent_failed","detail_code":"entrypoint_not_found"}\n'
        missing = "falta.mjs" in json.loads(Path(argv[2]).read_text())["entrypoint"]
        return subprocess.CompletedProcess(argv, 1 if missing else 0, failed if missing else b"")

    monkeypatch.setattr(subprocess, "run", run)
    _bridge_driver(tmp_path, "agent.mjs#agent").warm()
    assert calls[-1][-1] == node_callable.CHECK_ENTRY_FLAG
    with pytest.raises(AgentFailure, match="entrypoint_not_found"):
        _bridge_driver(tmp_path, "falta.mjs#agent").warm()
    calls.clear()
    _bridge_driver(tmp_path, None).warm()
    assert calls == []


def test_node_callable_warm_has_a_code_without_an_agent_failed_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def run(argv: list[str], **_: Any) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(argv, 1, b"no json\n")

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(AgentFailure, match=node_callable.CHECK_ENTRY_FAILED):
        _bridge_driver(tmp_path, "agent.mjs").warm()

    def slow(argv: list[str], **_: Any) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.TimeoutExpired(argv, node_callable.CHECK_ENTRY_TIMEOUT_SECONDS)

    monkeypatch.setattr(subprocess, "run", slow)
    with pytest.raises(AgentFailure, match=node_callable.CHECK_ENTRY_TIMEOUT):
        _bridge_driver(tmp_path, "agent.mjs").warm()


@pytest.mark.skipif(shutil.which("node") is None, reason="sin node en el PATH")
def test_the_real_bridge_check_entry_finds_a_missing_export(tmp_path: Path) -> None:
    bridge = Path(strands.__file__).parents[3] / "_assets" / "node_bridge.mjs"
    (tmp_path / "agent.mjs").write_text("export const agent = {};\n")
    for entrypoint, ok in (
        ("agent.mjs#agent", True),
        ("agent.mjs#otro", False),
        ("no-existe.mjs", False),
    ):
        config = tmp_path / "config.json"
        config.write_text(json.dumps({"entrypoint": entrypoint, "app_dir": str(tmp_path)}))
        outcome = subprocess.run(
            ["node", str(bridge), str(config), node_callable.CHECK_ENTRY_FLAG],
            capture_output=True,
            timeout=60,
            check=False,
        )
        assert (outcome.returncode == 0) is ok, entrypoint
        if not ok:
            assert node_callable.failed_detail(outcome.stdout) == "entrypoint_not_found"


def test_the_default_adapter_is_the_one_the_bridge_uses() -> None:
    source = (Path(strands.__file__).parents[3] / "_assets" / "node_bridge.mjs").read_text("utf-8")
    match = re.search(r'export const DEFAULT_ADAPTER = "([^"]+)";', source)
    assert match is not None
    assert match.group(1) == node_callable.DEFAULT_ADAPTER


# ---- el host completo, por su línea de órdenes ----


def _run_host(
    tmp: Path, driver: str, options: dict[str, Any], request: dict[str, Any]
) -> list[dict[str, Any]]:
    host_json = tmp / "host.json"
    host_json.write_text(
        json.dumps({"schema": "rayito.agent-host/1", "v": 1, "driver": driver, "options": options})
    )
    completed = subprocess.run(
        [sys.executable, str(HOST_DIR), "task", str(host_json)],
        input=json.dumps(request),
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "PYTHONPATH": ""},
        check=False,
    )
    return [json.loads(line) for line in completed.stdout.splitlines()]


def test_the_host_closes_a_custom_command_run_with_done(tmp_path: Path) -> None:
    config = _command_config(V1_CHILD, tmp_path)
    lines = _run_host(
        tmp_path,
        "command",
        {"command": config["command"], "workdir": config["workdir"]},
        {"v": 1, "prompt": "hola", "session_id": None, "model": None, "reasoning": False},
    )
    assert lines[0]["type"] == "session" and lines[-1]["type"] == "done"
    assert lines[-1]["session_id"] == lines[0]["session_id"]


def test_the_host_reports_a_failing_callable_as_runtime_error_with_the_class_name(
    tmp_path: Path,
) -> None:
    (tmp_path / "rota.py").write_text("def responde(request):\n    raise KeyError('secreto')\n")
    lines = _run_host(
        tmp_path,
        "callable",
        {"entrypoint": "rota:responde", "app_dir": str(tmp_path), "workdir": str(tmp_path)},
        {"v": 1, "prompt": "hola", "session_id": None, "model": None, "reasoning": False},
    )
    assert lines[-1]["type"] == "agent_failed"
    assert lines[-1]["reason"] == "runtime_error"
    assert lines[-1]["detail_code"] == "KeyError"
    assert "secreto" not in json.dumps(lines)
