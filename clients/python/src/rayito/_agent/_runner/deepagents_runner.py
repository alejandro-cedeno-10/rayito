"""Runner de deepagents dentro del sandbox (`ai-agent-deepagents`, ADR-025,
design.md §3). Corre con el Python del venv `/opt/agents/deepagents` como
`python deepagents_runner.py <config.json>` y habla el protocolo JSONL v1 de
Rayito por su stdout; el adaptador `rayito._agent._deepagents` lo traduce a
eventos del SDK.

- **Entrada**: la configuración estática (modelo, pasarela, instrucciones,
  permisos, subagentes) en el JSON que escribe el SDK, y por stdin la
  petición de cada ejecución `{"v":1,"prompt","session_id","model","reasoning"}`.
  El prompt nunca va en argv.
- **Salida**: una línea `{"v":1,"type":…}` por evento, con los mismos nombres
  que los eventos de Rayito, más `session`. El stdout original se duplica a
  un descriptor privado y `sys.stdout` pasa a ser stderr, para que un
  `print` del código del usuario nunca rompa el protocolo.
- **Credenciales**: ninguna. El modelo apunta a la pasarela de secretos
  (loopback) con un marcador como clave; la pasarela pone la real.
- **Sesiones**: `<sessions_dir>/<id>.json` con `messages_to_dict`; los ids
  son `rda_<uuid4 hex>`, generados aquí dentro.

Sólo usa la biblioteca estándar en el arranque: deepagents y langchain se
importan con `importlib` al construir el agente, así que los tests del SDK
lo ejercitan con módulos de doble en `sys.modules`. No importa nada de
`rayito`: dentro del sandbox no está instalado.
"""

from __future__ import annotations

import fnmatch
import importlib
import json
import os
import re
import sys
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, Final

#: Versión del protocolo JSONL (`AGENT_PROTOCOL_VERSION` de `limits.json`;
#: un test comprueba que coinciden).
PROTOCOL_VERSION: Final = 1
#: Recorte de la salida de una herramienta en el evento `tool_call`
#: (`MAX_TOOL_OUTPUT_PREVIEW_BYTES` de `limits.json`): así ninguna línea se
#: acerca a `MAX_AGENT_EVENT_LINE_BYTES` y el SDK no la descarta.
MAX_TOOL_OUTPUT_BYTES: Final = 65536
#: Prefijo de los ids de sesión de deepagents (design.md §3).
SESSION_ID_PREFIX: Final = "rda_"
#: El SDK impone `max_steps` matando el proceso; este tope sólo evita que
#: el `recursion_limit` de LangGraph (25 por defecto) corte antes que él.
RECURSION_LIMIT: Final = 10_000
#: Contenido del `ToolMessage` con el que se deniega una herramienta: el
#: modelo lo ve y puede seguir sin ella.
DENIED_TOOL_MESSAGE: Final = "Permiso denegado por la configuración del agente."
#: Módulos cuyas excepciones son del modelo o de la pasarela (`model_error`);
#: cualquier otra excepción es `runtime_error`.
MODEL_ERROR_MODULES: Final = (
    "anthropic",
    "botocore",
    "httpx",
    "langchain_anthropic",
    "langchain_aws",
    "langchain_openai",
    "openai",
)
#: El nodo del modelo en los grafos de `langchain.agents.create_agent`.
MODEL_NODE: Final = "model"
#: El nodo de herramientas en esos mismos grafos.
TOOLS_NODE: Final = "tools"

_SESSION_ID_PATTERN: Final = re.compile(r"rda_[0-9a-f]{32}")
_ENTRYPOINT_PATTERN: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*")
_REASONING_BLOCK_TYPES: Final = ("reasoning", "thinking", "reasoning_content")


class RunnerError(Exception):
    """Un fallo con `reason` y `detail_code` del protocolo, nunca con texto
    del proveedor ni contenido."""

    def __init__(self, reason: str, detail_code: str) -> None:
        super().__init__(detail_code)
        self.reason = reason
        self.detail_code = detail_code


class Emitter:
    """Escribe una línea JSON por evento en el descriptor del protocolo."""

    def __init__(self, stream: IO[str]) -> None:
        self._stream = stream

    def emit(self, kind: str, **fields: object) -> None:
        payload = {"v": PROTOCOL_VERSION, "type": kind, **fields}
        self._stream.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
        self._stream.flush()


@dataclass
class RunnerContext:
    """Lo que recibe el punto de entrada del usuario (`DeepAgents(entrypoint=
    "pkg.mod:build")`), que debe devolver un grafo compilado. Si el grafo no
    incluye `middleware`, se pierden los permisos de `AgentSpec`; si no pasa
    `subagents` a `create_deep_agent`, el `general-purpose` que deepagents
    añade solo no los tiene."""

    model: Any
    instructions: str | None
    subagents: list[dict[str, Any]]
    backend: Any
    middleware: list[Any]
    workdir: str


def open_protocol_stream() -> IO[str]:
    """Duplica el stdout original a un descriptor privado para el protocolo
    y apunta el descriptor 1 y `sys.stdout` a stderr."""
    protocol_fd = os.dup(1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    return os.fdopen(protocol_fd, "w", encoding="utf-8", buffering=1)


def new_session_id() -> str:
    return SESSION_ID_PREFIX + uuid.uuid4().hex


def truncate(text: str) -> tuple[str, bool]:
    encoded = text.encode("utf-8")
    if len(encoded) <= MAX_TOOL_OUTPUT_BYTES:
        return text, False
    return encoded[:MAX_TOOL_OUTPUT_BYTES].decode("utf-8", errors="ignore"), True


def decide(rules: Mapping[str, Any], tool: str, args: Mapping[str, Any]) -> str:
    """`allow` o `deny` para una llamada. Una herramienta sin regla usa
    `default`; con patrones (sólo `execute`) gana el último que casa con el
    comando, y si ninguno casa, `default`."""
    default = str(rules.get("default", "allow"))
    rule = rules.get("tools", {}).get(tool)
    if rule is None:
        return default
    if isinstance(rule, str):
        return rule
    command = str(args.get("command", ""))
    decision = default
    for pattern, action in rule.items():
        if fnmatch.fnmatchcase(command, pattern):
            decision = str(action)
    return decision


def _text_of(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(block.get("text", ""))
            for block in content
            if isinstance(block, Mapping) and block.get("type") == "text"
        )
    return ""


def _reasoning_of(content: object) -> str:
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, Mapping) and block.get("type") in _REASONING_BLOCK_TYPES:
            for key in _REASONING_BLOCK_TYPES:
                value = block.get(key)
                if isinstance(value, str):
                    parts.append(value)
                    break
    return "".join(parts)


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def usage_of(message: Any) -> dict[str, int]:
    """`usage_metadata` de LangChain a `TokenUsage` de Rayito. LangChain
    cuenta en `input_tokens` también lo leído y escrito en caché; Rayito no
    (F14), así que se resta."""
    metadata = getattr(message, "usage_metadata", None) or {}
    details = metadata.get("input_token_details") or {}
    output_details = metadata.get("output_token_details") or {}
    cache_read = _count(details.get("cache_read"))
    cache_write = _count(details.get("cache_creation"))
    return {
        "input": max(_count(metadata.get("input_tokens")) - cache_read - cache_write, 0),
        "output": _count(metadata.get("output_tokens")),
        "reasoning": _count(output_details.get("reasoning")),
        "cache_read": cache_read,
        "cache_write": cache_write,
    }


def _finish_reason(message: Any) -> str | None:
    metadata = getattr(message, "response_metadata", None) or {}
    for key in ("stopReason", "stop_reason", "finish_reason"):
        value = metadata.get(key)
        if isinstance(value, str):
            return value
    return None


def _messages_of(value: object) -> list[Any]:
    if isinstance(value, Mapping):
        messages = value.get("messages")
        if isinstance(messages, list):
            return messages
        if messages is not None and hasattr(messages, "value"):
            inner = messages.value
            return inner if isinstance(inner, list) else []
    return []


@dataclass
class Translator:
    """Traduce el `stream(stream_mode=["messages", "updates"])` del grafo
    principal a eventos del protocolo. Lo que llega de los subagentes (con
    `subgraphs=True`, un espacio de nombres no vacío) no son pasos del
    agente principal: su texto se ignora, pero el `usage_metadata` de los
    mensajes de su nodo del modelo se acumula y se suma al siguiente
    `step_finished` del agente principal (el que recibe el resultado de
    `task`), para que el uso y el tope de tokens del SDK cuenten también lo
    gastado dentro de los subagentes."""

    reasoning: bool = False
    in_step: bool = False
    steps: int = 0
    tool_inputs: dict[str, Any] = field(default_factory=dict)
    new_messages: list[Any] = field(default_factory=list)
    nested_usage: dict[str, int] = field(default_factory=dict)

    def _start(self) -> list[dict[str, Any]]:
        if self.in_step:
            return []
        self.in_step = True
        self.steps += 1
        return [{"type": "step_started", "index": self.steps}]

    def on_chunk(self, chunk: Any, metadata: Mapping[str, Any]) -> list[dict[str, Any]]:
        if metadata.get("langgraph_node") != MODEL_NODE:
            return []
        if "|" in str(metadata.get("langgraph_checkpoint_ns", "")):
            return []
        text = _text_of(getattr(chunk, "content", None))
        if not text:
            return []
        return [*self._start(), {"type": "text_delta", "text": text}]

    def on_update(self, update: object) -> list[dict[str, Any]]:
        if not isinstance(update, Mapping):
            return []
        events: list[dict[str, Any]] = []
        for node, value in update.items():
            if node == MODEL_NODE:
                for message in _messages_of(value):
                    events.extend(self._model_message(message))
            elif node == TOOLS_NODE:
                for message in _messages_of(value):
                    events.extend(self._tool_message(message))
        return events

    def on_nested_update(self, update: object) -> None:
        if not isinstance(update, Mapping):
            return
        for message in _messages_of(update.get(MODEL_NODE)):
            for key, value in usage_of(message).items():
                self.nested_usage[key] = self.nested_usage.get(key, 0) + value

    def _model_message(self, message: Any) -> list[dict[str, Any]]:
        self.new_messages.append(message)
        events = self._start()
        for call in getattr(message, "tool_calls", None) or []:
            if isinstance(call, Mapping) and call.get("id"):
                self.tool_inputs[str(call["id"])] = call.get("args")
        content = getattr(message, "content", None)
        if self.reasoning:
            thought = _reasoning_of(content)
            if thought:
                events.append({"type": "reasoning", "text": thought})
        text = _text_of(content)
        if text:
            events.append({"type": "text", "text": text})
        events.append(
            {
                "type": "step_finished",
                "index": self.steps,
                "usage": self._step_usage(message),
                "finish_reason": _finish_reason(message),
            }
        )
        self.in_step = False
        return events

    def _step_usage(self, message: Any) -> dict[str, int]:
        usage = usage_of(message)
        for key, value in self.nested_usage.items():
            usage[key] = usage.get(key, 0) + value
        self.nested_usage.clear()
        return usage

    def _tool_message(self, message: Any) -> list[dict[str, Any]]:
        self.new_messages.append(message)
        call_id = str(getattr(message, "tool_call_id", "") or "")
        content = getattr(message, "content", "")
        output, truncated = truncate(content if isinstance(content, str) else _text_of(content))
        raw_input = self.tool_inputs.pop(call_id, None)
        return [
            {
                "type": "tool_call",
                "call_id": call_id,
                "name": str(getattr(message, "name", "") or ""),
                "status": "error" if getattr(message, "status", None) == "error" else "completed",
                "input": raw_input if isinstance(raw_input, Mapping) else None,
                "output": output,
                "output_truncated": truncated,
            }
        ]


def build_model(config: Mapping[str, Any], model_id: str) -> Any:
    """El chat model de LangChain para el proveedor, siempre contra la
    pasarela (`base_url`) y con el marcador como credencial."""
    provider = config["provider"]
    base_url = config["base_url"]
    placeholder = config["credential_placeholder"]
    if provider == "bedrock":
        module = importlib.import_module("langchain_aws")
        return module.ChatBedrockConverse(
            model=model_id, region_name=config["region"], endpoint_url=base_url
        )
    if provider == "anthropic":
        module = importlib.import_module("langchain_anthropic")
        return module.ChatAnthropic(model=model_id, base_url=base_url, api_key=placeholder)
    if provider == "openai-compatible":
        module = importlib.import_module("langchain_openai")
        return module.ChatOpenAI(model=model_id, base_url=base_url, api_key=placeholder)
    raise RunnerError("protocol_error", "unknown_provider")


def permission_middleware(rules: Mapping[str, Any]) -> Any:
    """Un `AgentMiddleware` cuyo `wrap_tool_call` contesta con un
    `ToolMessage` de error cuando `decide()` deniega la llamada."""
    middleware_types = importlib.import_module("langchain.agents.middleware")
    messages = importlib.import_module("langchain_core.messages")

    def _denied(request: Any) -> Any:
        call = request.tool_call
        if decide(rules, str(call.get("name", "")), call.get("args") or {}) == "allow":
            return None
        return messages.ToolMessage(
            content=DENIED_TOOL_MESSAGE,
            tool_call_id=call.get("id", ""),
            name=call.get("name", ""),
            status="error",
        )

    def wrap_tool_call(self: Any, request: Any, handler: Callable[[Any], Any]) -> Any:
        denied = _denied(request)
        return handler(request) if denied is None else denied

    async def awrap_tool_call(self: Any, request: Any, handler: Callable[[Any], Any]) -> Any:
        denied = _denied(request)
        return await handler(request) if denied is None else denied

    cls = type(
        "RayitoPermissionMiddleware",
        (middleware_types.AgentMiddleware,),
        {"wrap_tool_call": wrap_tool_call, "awrap_tool_call": awrap_tool_call},
    )
    return cls()


def disable_prompt_caching(deepagents_graph: Any) -> None:
    """deepagents 0.7 añade siempre sus middlewares de caché de prompts
    (Anthropic y Bedrock) en `create_deep_agent`; con
    `AgentModel(prompt_caching=False)` se sustituye esa función por una que
    no añade nada. La versión de deepagents está fijada en la plantilla."""
    deepagents_graph.append_prompt_caching_middleware = lambda middleware: None


def general_purpose_subagent(rules: Mapping[str, Any]) -> dict[str, Any]:
    """El subagente `general-purpose` que deepagents añade solo, pero con el
    middleware de permisos del agente principal. deepagents 0.7 no le pasa
    al que añade él el `middleware` del agente principal, y omite el suyo si
    ya hay uno con ese nombre: así `task` no sirve para saltarse los
    permisos."""
    subagents_module = importlib.import_module("deepagents.middleware.subagents")
    return {
        **subagents_module.GENERAL_PURPOSE_SUBAGENT,
        "middleware": [permission_middleware(rules)],
    }


def subagents_of(
    config: Mapping[str, Any], model_for: Callable[[str], Any]
) -> list[dict[str, Any]]:
    """Los subagentes de `AgentSpec.agents`, cada uno con su middleware de
    permisos (los suyos o, si no tiene, los del agente principal, como en
    OpenCode), más el `general-purpose` con los del principal."""
    subagents: list[dict[str, Any]] = []
    for entry in config.get("subagents", []):
        sub: dict[str, Any] = {
            "name": entry["name"],
            "description": entry["description"],
            "system_prompt": entry["system_prompt"],
        }
        if entry.get("model"):
            sub["model"] = model_for(entry["model"])
        rules = entry.get("permissions")
        sub["middleware"] = [
            permission_middleware(config["permissions"] if rules is None else rules)
        ]
        subagents.append(sub)
    general = general_purpose_subagent(config["permissions"])
    if all(sub["name"] != general["name"] for sub in subagents):
        subagents.append(general)
    return subagents


def default_graph(ctx: RunnerContext) -> Any:
    """El agente por defecto: `create_deep_agent` con el backend de shell
    local sobre el directorio de trabajo."""
    deepagents = importlib.import_module("deepagents")
    return deepagents.create_deep_agent(
        model=ctx.model,
        system_prompt=ctx.instructions,
        subagents=ctx.subagents,
        backend=ctx.backend,
        middleware=ctx.middleware,
    )


def load_entrypoint(entrypoint: str, workdir: str) -> Callable[[RunnerContext], Any]:
    """`pkg.mod:build`, importable desde el directorio de trabajo."""
    if not _ENTRYPOINT_PATTERN.fullmatch(entrypoint):
        raise RunnerError("runtime_error", "invalid_entrypoint")
    module_name, attr = entrypoint.split(":", 1)
    if workdir not in sys.path:
        sys.path.insert(0, workdir)
    try:
        builder = getattr(importlib.import_module(module_name), attr)
    except (ImportError, AttributeError):
        raise RunnerError("runtime_error", "entrypoint_not_found") from None
    if not callable(builder):
        raise RunnerError("runtime_error", "invalid_entrypoint")
    return builder  # type: ignore[no-any-return]


def load_history(sessions_dir: Path, session_id: str | None) -> tuple[str, list[Any]]:
    if session_id is None:
        return new_session_id(), []
    if not _SESSION_ID_PATTERN.fullmatch(session_id):
        raise RunnerError("runtime_error", "invalid_session_id")
    path = sessions_dir / f"{session_id}.json"
    if not path.is_file():
        raise RunnerError("runtime_error", "session_not_found")
    messages = importlib.import_module("langchain_core.messages")
    return session_id, list(messages.messages_from_dict(json.loads(path.read_text("utf-8"))))


def save_history(sessions_dir: Path, session_id: str, history: Sequence[Any]) -> None:
    """Escritura atómica (fichero temporal y `rename`), sólo legible por el
    usuario del sandbox."""
    messages = importlib.import_module("langchain_core.messages")
    sessions_dir.mkdir(parents=True, exist_ok=True)
    path = sessions_dir / f"{session_id}.json"
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(messages.messages_to_dict(list(history)), handle)
    tmp.replace(path)


def _classify(exc: BaseException) -> tuple[str, str]:
    module = type(exc).__module__.split(".", 1)[0]
    reason = "model_error" if module in MODEL_ERROR_MODULES else "runtime_error"
    return reason, type(exc).__name__


def build_context(config: Mapping[str, Any], model_id: str) -> RunnerContext:
    if not config.get("prompt_caching", True):
        disable_prompt_caching(importlib.import_module("deepagents.graph"))
    backends = importlib.import_module("deepagents.backends")
    workdir = str(config["workdir"])
    return RunnerContext(
        model=build_model(config, model_id),
        instructions=config.get("instructions"),
        subagents=subagents_of(config, lambda sub_model: build_model(config, sub_model)),
        backend=backends.LocalShellBackend(root_dir=workdir),
        middleware=[permission_middleware(config["permissions"])],
        workdir=workdir,
    )


def stream_events(
    graph: Any, history: list[Any], translator: Translator
) -> Iterable[dict[str, Any]]:
    stream = graph.stream(
        {"messages": history},
        config={"recursion_limit": RECURSION_LIMIT},
        stream_mode=["messages", "updates"],
        subgraphs=True,
    )
    for namespace, mode, data in stream:
        if namespace:
            if mode == "updates":
                translator.on_nested_update(data)
        elif mode == "messages":
            chunk, metadata = data
            yield from translator.on_chunk(chunk, metadata if isinstance(metadata, Mapping) else {})
        elif mode == "updates":
            yield from translator.on_update(data)


def run(config: Mapping[str, Any], request: Mapping[str, Any], emitter: Emitter) -> int:
    """Una ejecución completa: siempre termina con `done` o `agent_failed`."""
    session_id: str | None = None
    try:
        if request.get("v") != PROTOCOL_VERSION or not isinstance(request.get("prompt"), str):
            raise RunnerError("protocol_error", "invalid_request")
        sessions_dir = Path(config["sessions_dir"])
        session_id, history = load_history(sessions_dir, request.get("session_id"))
        emitter.emit("session", session_id=session_id)
        ctx = build_context(config, request.get("model") or config["model"])
        entrypoint = config.get("entrypoint")
        graph = (load_entrypoint(entrypoint, ctx.workdir) if entrypoint else default_graph)(ctx)
        messages = importlib.import_module("langchain_core.messages")
        history.append(messages.HumanMessage(content=request["prompt"]))
        translator = Translator(reasoning=bool(request.get("reasoning")))
        for event in stream_events(graph, history, translator):
            emitter.emit(event.pop("type"), **event)
        save_history(sessions_dir, session_id, [*history, *translator.new_messages])
    except RunnerError as exc:
        emitter.emit(
            "agent_failed", reason=exc.reason, detail_code=exc.detail_code, session_id=session_id
        )
        return 0
    except Exception as exc:
        reason, detail = _classify(exc)
        emitter.emit("agent_failed", reason=reason, detail_code=detail, session_id=session_id)
        return 0
    emitter.emit("done", session_id=session_id)
    return 0


def main(argv: Sequence[str]) -> int:
    emitter = Emitter(open_protocol_stream())
    try:
        config = json.loads(Path(argv[1]).read_text("utf-8"))
        request = json.loads(sys.stdin.read())
    except (IndexError, OSError, ValueError):
        emitter.emit("agent_failed", reason="protocol_error", detail_code="invalid_input")
        return 0
    if not isinstance(config, dict) or not isinstance(request, dict):
        emitter.emit("agent_failed", reason="protocol_error", detail_code="invalid_input")
        return 0
    return run(config, request, emitter)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
