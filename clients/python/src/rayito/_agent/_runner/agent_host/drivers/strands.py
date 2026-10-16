"""Driver `strands` del host (ADR-028, D2, D3 y D9): corre un agente de
Strands Agents (`Agent` o `create_harness` del paquete `strands-harness`)
con el Python del venv `/opt/agents/strands` (o `strands-harness`) y traduce
`Agent.stream_async` a eventos del protocolo v1 de Rayito.

Configuración (el JSON que escribe `rayito._agent._strands`):

- `provider`, `client`, `model`, `region`, `base_url` y
  `credential_placeholder`: el modelo, siempre por la pasarela con un
  marcador como clave (nunca credenciales de AWS). Sin `base_url` (un
  contenedor exportado) el cliente usa su endpoint y su credencial estándar.
- `entrypoint` (`modulo:atributo`, desde `app_dir` y `workdir`): un `Agent`
  tuyo o una función `fn(ctx) -> Agent` que recibe el modelo ya cableado en
  `ctx.model`. Sin él, el host construye `Agent(model, system_prompt, tools=[])`
  o, con `harness`, `create_harness` con las herramientas integradas de
  `builtin_tools` (`programmatic_tool_caller` y `web_search` apagadas), la
  sesión en `sessions_dir` y la memoria en `memory_dir`.
- `permissions` y `permission_aliases`: se aplican a toda llamada de
  herramienta con un gancho `BeforeToolCallEvent`, también a las de tu agente.

Sesiones: con `Agent`, el historial (`agent.messages`) va en
`<sessions_dir>/<id>.json`; con el harness lo guarda el propio harness bajo el
mismo `id`. Sólo biblioteca estándar en el arranque: Strands se importa con
`importlib`, así que los tests lo ejercitan con módulos de doble.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Final

from ..protocol import (
    AgentFailure,
    Emit,
    Request,
    RunState,
    build_event,
    truncate_output,
    usage_dict,
)
from ._common import (
    DENIED_TOOL_MESSAGE,
    add_import_roots,
    classify_chain,
    decide,
    entry_value,
    load_attribute,
    load_config,
    resolve_session_id,
)

#: `max_tokens` de `AnthropicModel`, que Strands exige (la API de Mensajes de
#: Anthropic lo requiere en cada petición).
ANTHROPIC_MAX_TOKENS: Final = 8192
#: Lo que tiene un `Agent` de Strands ya construido (un `entrypoint` que es el
#: agente y no una fábrica).
AGENT_ATTRIBUTES: Final = ("stream_async",)
#: Excepciones del propio Strands que son del modelo (sin importar su módulo).
MODEL_EXCEPTION_NAMES: Final = frozenset(
    {"ModelThrottledException", "ContextWindowOverflowException", "MaxTokensReachedException"}
)
USAGE_KEYS: Final = {
    "inputTokens": "input",
    "outputTokens": "output",
    "cacheReadInputTokens": "cache_read",
    "cacheWriteInputTokens": "cache_write",
}
#: Los eventos de `stream_async` que el traductor lee (medidos en
#: `testdata/agent/strands-python-events.jsonl`).
#: El historial de una sesión sólo lo lee el usuario del sandbox.
HISTORY_FILE_MODE: Final = 0o600
ROLE_ASSISTANT: Final = "assistant"
ROLE_USER: Final = "user"


def _usage_of(raw: object) -> dict[str, int]:
    source = raw if isinstance(raw, Mapping) else {}
    return usage_dict({USAGE_KEYS[key]: source.get(key) for key in USAGE_KEYS})


def _tool_result_text(content: object) -> str:
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, Mapping):
            continue
        if isinstance(block.get("text"), str):
            parts.append(block["text"])
        elif "json" in block:
            parts.append(json.dumps(block["json"], ensure_ascii=False))
    return "\n".join(parts)


class Translator:
    """`stream_async` de Strands a eventos v1. Un paso empieza en
    `messageStart` y termina con el mensaje del asistente; los resultados de
    herramienta (mensaje de usuario) se emiten después, como `tool_call`."""

    def __init__(self, *, reasoning: bool) -> None:
        self._reasoning = reasoning
        self._steps = 0
        self._text: list[str] = []
        self._thinking: list[str] = []
        self._usage: dict[str, int] = usage_dict(None)
        self._finish_reason: str | None = None
        self._pending: dict[str, tuple[str, object]] = {}

    def feed(self, event: Mapping[str, Any]) -> list[dict[str, Any]]:
        inner = event.get("event")
        if isinstance(inner, Mapping):
            return self._model_event(inner)
        data = event.get("data")
        if isinstance(data, str):
            self._text.append(data)
            return [build_event("text_delta", text=data)] if data else []
        if event.get("reasoning") is True and isinstance(event.get("reasoningText"), str):
            self._thinking.append(event["reasoningText"])
            return []
        message = event.get("message")
        if isinstance(message, Mapping):
            return self._message(message)
        return []

    def _model_event(self, inner: Mapping[str, Any]) -> list[dict[str, Any]]:
        if "messageStart" in inner:
            self._steps += 1
            self._text, self._thinking = [], []
            self._usage, self._finish_reason = usage_dict(None), None
            return [build_event("step_started", index=self._steps)]
        stop = inner.get("messageStop")
        if isinstance(stop, Mapping) and isinstance(stop.get("stopReason"), str):
            self._finish_reason = stop["stopReason"]
        metadata = inner.get("metadata")
        if isinstance(metadata, Mapping):
            self._usage = _usage_of(metadata.get("usage"))
        return []

    def _message(self, message: Mapping[str, Any]) -> list[dict[str, Any]]:
        content = message.get("content")
        blocks = (
            [block for block in content if isinstance(block, Mapping)]
            if isinstance(content, list)
            else []
        )
        if message.get("role") == ROLE_ASSISTANT:
            for block in blocks:
                tool_use = block.get("toolUse")
                if isinstance(tool_use, Mapping):
                    self._pending[str(tool_use.get("toolUseId", ""))] = (
                        str(tool_use.get("name", "")),
                        tool_use.get("input"),
                    )
            return self._close_step()
        if message.get("role") == ROLE_USER:
            return [call for block in blocks if (call := self._tool_call(block)) is not None]
        return []

    def _close_step(self) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        text = "".join(self._text)
        if text:
            events.append(build_event("text", text=text))
        thinking = "".join(self._thinking)
        if self._reasoning and thinking:
            events.append(build_event("reasoning", text=thinking))
        events.append(
            build_event(
                "step_finished",
                index=self._steps,
                usage=self._usage,
                finish_reason=self._finish_reason,
            )
        )
        return events

    def _tool_call(self, block: Mapping[str, Any]) -> dict[str, Any] | None:
        result = block.get("toolResult")
        if not isinstance(result, Mapping):
            return None
        call_id = str(result.get("toolUseId", ""))
        name, raw_input = self._pending.pop(call_id, ("", None))
        output, truncated = truncate_output(_tool_result_text(result.get("content")))
        return build_event(
            "tool_call",
            call_id=call_id,
            name=name,
            status="completed" if result.get("status") == "success" else "error",
            input=dict(raw_input) if isinstance(raw_input, Mapping) else None,
            output=output,
            output_truncated=truncated,
        )


def build_model(config: Mapping[str, Any], model_id: str) -> Any:
    """El modelo de Strands del proveedor de `config`, apuntado a la
    pasarela con el marcador como clave. Sin `base_url` (contenedor
    exportado) el cliente usa su endpoint y su credencial estándar."""
    provider = config["provider"]
    base_url = config.get("base_url")
    key = config.get("credential_placeholder")
    gateway = base_url is not None
    if provider == "bedrock":
        module = importlib.import_module("strands.models.bedrock")
        options: dict[str, Any] = {"model_id": model_id, "region_name": config["region"]}
        if gateway:
            options.update(endpoint_url=base_url, api_key=key)
        if config.get("prompt_caching", True):
            cache = importlib.import_module("strands.models.model").CacheConfig
            options["cache_config"] = cache(strategy="auto")
        return module.BedrockModel(**options)
    if provider == "anthropic":
        module = importlib.import_module("strands.models.anthropic")
        client_args = {"base_url": base_url, "api_key": key} if gateway else None
        return module.AnthropicModel(
            client_args=client_args, model_id=model_id, max_tokens=ANTHROPIC_MAX_TOKENS
        )
    if provider in ("openai", "openai-compatible"):
        module = importlib.import_module("strands.models.openai")
        client_args = {"base_url": base_url, "api_key": key} if gateway else None
        return module.OpenAIModel(client_args=client_args, model_id=model_id)
    if provider == "google":
        module = importlib.import_module("strands.models.gemini")
        client_args = {"api_key": key, "http_options": {"base_url": base_url}} if gateway else None
        return module.GeminiModel(client_args=client_args, model_id=model_id)
    raise AgentFailure("protocol_error", "unknown_provider")


def register_permissions(agent: Any, rules: Mapping[str, Any], aliases: Mapping[str, str]) -> None:
    """Un gancho `BeforeToolCallEvent` que cancela con un resultado de error
    toda llamada que `decide()` deniega. Se añade una sola vez por agente."""
    if getattr(agent, "_rayito_permissions", False):
        return
    events = importlib.import_module("strands.hooks")

    def deny(event: Any) -> None:
        tool_use = event.tool_use
        name = str(tool_use.get("name", ""))
        arguments = tool_use.get("input")
        permission = aliases.get(name, name)
        if (
            decide(rules, permission, arguments if isinstance(arguments, Mapping) else {})
            != "allow"
        ):
            event.cancel_tool = DENIED_TOOL_MESSAGE

    agent.hooks.add_callback(events.BeforeToolCallEvent, deny)
    agent._rayito_permissions = True


def _context(config: Mapping[str, Any], model: Any, session_id: str) -> SimpleNamespace:
    """Lo que recibe tu función `fn(ctx)`: el modelo cableado y las opciones
    seguras del harness (`builtin_tools`, `session`, `memory`) para que las
    pases a `create_harness` si lo usas."""
    return SimpleNamespace(
        model=model,
        instructions=config.get("instructions"),
        workdir=config["workdir"],
        state_dir=config["state_dir"],
        session_id=session_id,
        builtin_tools=config.get("builtin_tools"),
        session={"id": session_id, "dir": config["sessions_dir"]},
        memory={"dir": config.get("memory_dir")},
    )


def _entrypoint_agent(config: Mapping[str, Any], model: Any, session_id: str) -> Any:
    return entry_value(
        load_attribute(str(config["entrypoint"])),
        _context(config, model, session_id),
        ready_attributes=AGENT_ATTRIBUTES,
    )


def _default_agent(config: Mapping[str, Any], model: Any, session_id: str) -> Any:
    if config.get("harness"):
        harness = importlib.import_module("strands_harness")
        options: dict[str, Any] = {
            "model": model,
            "instructions": config.get("instructions"),
            "builtin_tools": config["builtin_tools"],
            "session": {"id": session_id, "dir": config["sessions_dir"]},
            "memory": {"dir": config["memory_dir"]},
            "callback_handler": None,
        }
        return harness.create_harness(**options)
    strands = importlib.import_module("strands")
    return strands.Agent(
        model=model,
        system_prompt=config.get("instructions"),
        tools=[],
        callback_handler=None,
    )


def build_agent(config: Mapping[str, Any], model: Any, session_id: str) -> Any:
    """El agente de la ejecución: el de tu `entrypoint` o el por defecto."""
    if config.get("entrypoint"):
        return _entrypoint_agent(config, model, session_id)
    return _default_agent(config, model, session_id)


def _history_path(config: Mapping[str, Any], session_id: str) -> Path:
    return Path(config["sessions_dir"]) / f"{session_id}.json"


def _uses_own_history(config: Mapping[str, Any]) -> bool:
    return not (config.get("harness") and not config.get("entrypoint"))


def load_history(config: Mapping[str, Any], session_id: str, resumed: bool) -> list[Any]:
    if not resumed or not _uses_own_history(config):
        return []
    path = _history_path(config, session_id)
    if not path.is_file():
        raise AgentFailure("runtime_error", "session_not_found")
    try:
        loaded = json.loads(path.read_text("utf-8"))
    except ValueError:
        raise AgentFailure("runtime_error", "session_unreadable") from None
    return loaded if isinstance(loaded, list) else []


def save_history(config: Mapping[str, Any], session_id: str, messages: list[Any]) -> None:
    """Escritura atómica (fichero temporal y `rename`), sólo legible por el
    usuario del sandbox."""
    if not _uses_own_history(config):
        return
    path = _history_path(config, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, HISTORY_FILE_MODE)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(messages, handle, ensure_ascii=False, default=str)
    temporary.replace(path)


async def stream_events(
    agent: Any,
    prompt: str,
    translator: Translator,
    emit: Callable[[dict[str, Any]], None],
    cancel: threading.Event,
) -> None:
    async for event in agent.stream_async(prompt, cancel_signal=cancel):
        if isinstance(event, Mapping):
            for translated in translator.feed(event):
                emit(translated)


class Driver:
    """El driver de Strands del host. `warm()` importa Strands para que la
    primera invocación del servicio no pague el import."""

    def __init__(self, config: Mapping[str, Any]) -> None:
        self._config = config
        self._cancel = threading.Event()

    def warm(self) -> None:
        importlib.import_module("strands")
        if self._config.get("harness"):
            importlib.import_module("strands_harness")

    def run(self, request: Request, emit: Emit, state: RunState) -> None:
        config = self._config
        add_import_roots(config.get("app_dir"), config.get("workdir"))
        self._cancel.clear()
        session_id = resolve_session_id(request.session_id)
        history = load_history(config, session_id, request.session_id is not None)
        state.session_id = session_id
        emit(build_event("session", session_id=session_id))
        try:
            model = build_model(config, request.model or str(config["model"]))
            agent = build_agent(config, model, session_id)
            register_permissions(
                agent, config.get("permissions", {}), config.get("permission_aliases", {})
            )
            if history and _uses_own_history(config):
                agent.messages[:] = history
            translator = Translator(reasoning=request.reasoning)
            asyncio.run(stream_events(agent, request.prompt, translator, emit, self._cancel))
        except AgentFailure:
            raise
        except Exception as exc:
            reason, detail = classify_chain(exc, MODEL_EXCEPTION_NAMES)
            raise AgentFailure(reason, detail) from None
        save_history(config, session_id, list(agent.messages))

    def cancel(self) -> None:
        self._cancel.set()


def create(options: Mapping[str, Any]) -> Driver:
    return Driver(load_config(options))
