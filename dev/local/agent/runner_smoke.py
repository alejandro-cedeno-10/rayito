"""Prueba del runner de deepagents con el venv real (`make agent-runner-test`).

Corre dentro de un contenedor Linux arm64 con los pines de
`rayito/_agent/_assets/requirements-deepagents.txt` instalados en `/venv`, sin red hacia ningún
modelo: el punto de entrada de usuario construye el grafo con un chat model
falso (`FakeModel`), así que se ejercitan deepagents, LangGraph, el
middleware de permisos, el protocolo JSONL y las sesiones de verdad. Además
construye el grafo por defecto con los tres proveedores (sin llamarlos),
comprueba que `task` -> `general-purpose` -> `execute` respeta un `bash`
denegado y que `DEEPAGENTS_TOOL_NAMES` cubre todas las herramientas de
sistema de ficheros del deepagents fijado.

Uso: `python runner_smoke.py <runner.py>`. Sale con 0 si todo cuadra.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ENTRYPOINT_MODULE = '''
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from deepagents import create_deep_agent


class FakeModel(BaseChatModel):
    """Devuelve mensajes ya escritos: dos llamadas a herramientas y un final."""

    replies: list = []

    @property
    def _llm_type(self):
        return "fake"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=self.replies.pop(0))])

    def bind_tools(self, tools, **kwargs):
        return self


def build(ctx):
    print("ruido del usuario en stdout")
    replies = [
        AIMessage(
            content="voy",
            tool_calls=[{"name": "execute", "args": {"command": "rm -rf x"}, "id": "c1"}],
            usage_metadata={
                "input_tokens": 5000,
                "output_tokens": 20,
                "total_tokens": 5020,
                "input_token_details": {"cache_read": 4100},
            },
        ),
        AIMessage(
            content="sigo",
            tool_calls=[{"name": "ls", "args": {"path": "/"}, "id": "c2"}],
            usage_metadata={"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
        ),
        AIMessage(
            content="listo",
            usage_metadata={"input_tokens": 30, "output_tokens": 3, "total_tokens": 33},
            response_metadata={"stopReason": "end_turn"},
        ),
    ]
    return create_deep_agent(
        model=FakeModel(replies=replies),
        system_prompt=ctx.instructions,
        backend=ctx.backend,
        middleware=ctx.middleware,
    )
'''


SUBAGENT_MODULE = '''
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from deepagents import create_deep_agent


class FakeModel(BaseChatModel):
    """El principal delega en `general-purpose`, que intenta `execute`."""

    replies: list = []

    @property
    def _llm_type(self):
        return "fake"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=self.replies.pop(0))])

    def bind_tools(self, tools, **kwargs):
        return self


def build(ctx):
    task_args = {"description": "crea el fichero", "subagent_type": "general-purpose"}
    replies = [
        AIMessage(content="", tool_calls=[{"name": "task", "args": task_args, "id": "t1"}]),
        AIMessage(
            content="",
            tool_calls=[{"name": "execute", "args": {"command": "touch pwned"}, "id": "s1"}],
            usage_metadata={"input_tokens": 70, "output_tokens": 5, "total_tokens": 75},
        ),
        AIMessage(content="no pude", usage_metadata={"input_tokens": 9, "output_tokens": 2, "total_tokens": 11}),
        AIMessage(content="listo", usage_metadata={"input_tokens": 30, "output_tokens": 3, "total_tokens": 33}),
    ]
    return create_deep_agent(
        model=FakeModel(replies=replies),
        system_prompt=ctx.instructions,
        subagents=ctx.subagents,
        backend=ctx.backend,
        middleware=ctx.middleware,
    )
'''


def _config(root: Path, **overrides: object) -> dict[str, object]:
    config: dict[str, object] = {
        "v": 1,
        "provider": "bedrock",
        "model": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        "region": "us-east-1",
        "base_url": "http://127.0.0.1:9",
        "credential_placeholder": "placeholder-not-a-secret",
        "prompt_caching": True,
        "instructions": "Sé breve.",
        "workdir": str(root / "work"),
        "sessions_dir": str(root / "sessions"),
        "entrypoint": "smoke_agent:build",
        "permissions": {
            "default": "allow",
            "tools": {"execute": {"*": "deny", "git *": "allow"}},
        },
        "subagents": [],
    }
    config.update(overrides)
    return config


def _run(
    runner: str, config_path: Path, request: dict[str, object]
) -> list[dict[str, object]]:
    result = subprocess.run(
        [sys.executable, runner, str(config_path)],
        input=json.dumps(request).encode(),
        capture_output=True,
        check=True,
        env={
            "AWS_BEARER_TOKEN_BEDROCK": "placeholder-not-a-secret",
            "PYTHONUNBUFFERED": "1",
        },
    )
    assert b"ruido del usuario" not in result.stdout, (
        "un print del usuario llegó al protocolo"
    )
    return [json.loads(line) for line in result.stdout.decode().splitlines()]


def _check_entrypoint(runner: str, root: Path) -> None:
    (root / "work").mkdir()
    (root / "work" / "smoke_agent.py").write_text(ENTRYPOINT_MODULE, encoding="utf-8")
    config_path = root / "config.json"
    config_path.write_text(json.dumps(_config(root)), encoding="utf-8")
    request = {
        "v": 1,
        "prompt": "hola",
        "session_id": None,
        "model": None,
        "reasoning": False,
    }
    events = _run(runner, config_path, request)
    kinds = [event["type"] for event in events]
    assert kinds[0] == "session" and kinds[-1] == "done", kinds
    assert kinds.count("step_finished") == 3, kinds
    tools = [event for event in events if event["type"] == "tool_call"]
    assert [(t["name"], t["status"]) for t in tools] == [
        ("execute", "error"),
        ("ls", "completed"),
    ], tools
    first_step = next(event for event in events if event["type"] == "step_finished")
    assert first_step["usage"]["cache_read"] == 4100, first_step
    assert first_step["usage"]["input"] == 900, first_step
    session_id = events[0]["session_id"]
    assert (root / "sessions" / f"{session_id}.json").is_file()
    resumed = _run(
        runner, config_path, {**request, "session_id": session_id, "prompt": "otra"}
    )
    assert resumed[0] == {"v": 1, "type": "session", "session_id": session_id}, resumed[
        0
    ]
    assert resumed[-1]["type"] == "done", resumed[-1]


def _check_subagent_permissions(runner: str, root: Path) -> None:
    work = root / "sub-work"
    work.mkdir()
    (work / "smoke_sub.py").write_text(SUBAGENT_MODULE, encoding="utf-8")
    config = _config(
        root,
        workdir=str(work),
        entrypoint="smoke_sub:build",
        permissions={"default": "allow", "tools": {"execute": "deny"}},
    )
    config_path = root / "sub-config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    request = {
        "v": 1,
        "prompt": "hola",
        "session_id": None,
        "model": None,
        "reasoning": False,
    }
    events = _run(runner, config_path, request)
    assert events[-1]["type"] == "done", events
    assert not (work / "pwned").exists(), "general-purpose ejecutó un comando denegado"
    usages = [event["usage"] for event in events if event["type"] == "step_finished"]
    assert usages[-1]["input"] == 30 + 70 + 9, usages


def _check_tool_names_cover_deepagents(runner: str) -> None:
    from deepagents.backends import LocalShellBackend
    from deepagents.middleware.filesystem import FilesystemMiddleware

    adapter = Path(runner).parent.parent / "_deepagents.py"
    tree = ast.parse(adapter.read_text(encoding="utf-8"))
    mapped: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AnnAssign)
            and getattr(node.target, "id", "") == "DEEPAGENTS_TOOL_NAMES"
        ):
            for names in ast.literal_eval(node.value).values():
                mapped.update(names)
    middleware = FilesystemMiddleware(backend=LocalShellBackend(root_dir="/tmp"))
    real = {tool.name for tool in middleware.tools}
    missing = real - mapped
    assert not missing, (
        f"herramientas de deepagents sin nombre en AgentPermissions: {missing}"
    )


def _check_default_graphs(runner: str, root: Path) -> None:
    sys.path.insert(0, str(Path(runner).parent))
    import deepagents_runner

    for provider in ("bedrock", "anthropic", "openai-compatible"):
        config = _config(root, provider=provider, entrypoint=None, prompt_caching=False)
        ctx = deepagents_runner.build_context(config, "modelo")
        graph = deepagents_runner.default_graph(ctx)
        assert hasattr(graph, "stream"), provider


def main(argv: list[str]) -> int:
    runner = str(Path(argv[1]).resolve())
    with tempfile.TemporaryDirectory() as tmp:
        _check_entrypoint(runner, Path(tmp))
        _check_subagent_permissions(runner, Path(tmp))
        _check_default_graphs(runner, Path(tmp))
    _check_tool_names_cover_deepagents(runner)
    sys.stdout.write("runner_smoke: ok\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
