"""Driver `callable` del host (ADR-028, D2 y D3): llama a una función de
Python tuya (`CustomRuntime(entrypoint="modulo:funcion")`) con la petición y
traduce lo que devuelve a eventos del protocolo v1.

La función recibe `{"prompt", "session_id", "model", "reasoning"}` y devuelve:

- una cadena: el texto final;
- un iterable (o iterable asíncrono) de cadenas: trozos de texto, que salen
  como `text_delta` en cuanto llegan;
- un `dict` con `text` y, si quiere, `usage` (`input`, `output`, `reasoning`,
  `cache_read`, `cache_write`).

Puede ser una función `async`. El estado entre peticiones es cosa tuya: el
driver sólo devuelve el `session_id` que recibió o uno nuevo.

Configuración del driver: `entrypoint`, `app_dir` y `workdir` (las dos
carpetas donde se busca tu módulo).
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterable, Iterable, Mapping
from typing import Any, Final

from ..protocol import (
    AgentFailure,
    Emit,
    Request,
    RunState,
    build_event,
    usage_dict,
)
from ._common import add_import_roots, load_attribute, load_config, resolve_session_id

FINISH_REASON: Final = "stop"


def _request_payload(request: Request, session_id: str) -> dict[str, Any]:
    return {
        "prompt": request.prompt,
        "session_id": session_id,
        "model": request.model,
        "reasoning": request.reasoning,
    }


async def _collect(result: Any, emit: Emit) -> tuple[str, Mapping[str, Any] | None]:
    """El texto y el uso de lo que devolvió la función, con los trozos
    emitidos como `text_delta`."""
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, str):
        return result, None
    if isinstance(result, Mapping):
        text = result.get("text")
        if not isinstance(text, str):
            raise AgentFailure("protocol_error", "invalid_response")
        usage = result.get("usage")
        return text, usage if isinstance(usage, Mapping) else None
    parts: list[str] = []
    if isinstance(result, AsyncIterable):
        async for chunk in result:
            _take(chunk, parts, emit)
        return "".join(parts), None
    if isinstance(result, Iterable):
        for chunk in result:
            _take(chunk, parts, emit)
        return "".join(parts), None
    raise AgentFailure("protocol_error", "invalid_response")


def _take(chunk: object, parts: list[str], emit: Emit) -> None:
    if not isinstance(chunk, str):
        raise AgentFailure("protocol_error", "invalid_response")
    if chunk:
        parts.append(chunk)
        emit(build_event("text_delta", text=chunk))


class Driver:
    """El driver `callable`."""

    def __init__(self, config: Mapping[str, Any]) -> None:
        self._config = config

    def _handler(self) -> Any:
        add_import_roots(self._config.get("app_dir"), self._config.get("workdir"))
        handler = load_attribute(str(self._config["entrypoint"]))
        if not callable(handler):
            raise AgentFailure("runtime_error", "invalid_entrypoint")
        return handler

    def warm(self) -> None:
        """Importa tu módulo: un entrypoint roto falla al arrancar el
        servicio y no en la primera petición."""
        self._handler()

    def run(self, request: Request, emit: Emit, state: RunState) -> None:
        handler = self._handler()
        state.session_id = resolve_session_id(request.session_id)
        emit(build_event("session", session_id=state.session_id))
        emit(build_event("step_started", index=1))
        payload = _request_payload(request, state.session_id)
        text, usage = asyncio.run(_collect(handler(payload), emit))
        emit(build_event("text", text=text))
        emit(
            build_event(
                "step_finished",
                index=1,
                usage=None if usage is None else usage_dict(usage),
                finish_reason=FINISH_REASON,
            )
        )

    def cancel(self) -> None:
        """Una función en proceso no se puede matar; la ejecución se corta
        al siguiente evento (`LimitExceeded` desde el emisor)."""


def create(options: Mapping[str, Any]) -> Driver:
    return Driver(load_config(options))
