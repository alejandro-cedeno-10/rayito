"""Modo `task` del host (ADR-028, D3): una petición por stdin y las líneas
del protocolo v1 por un descriptor privado, como hacía el runner de
deepagents. Siempre termina con `done` o `agent_failed`; nunca con una
traza."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from typing import IO, Any

from .protocol import (
    AgentFailure,
    Driver,
    Emitter,
    Event,
    RunState,
    classify_by_module,
    parse_request,
    run_to_terminal,
)


def forward(emitter: Emitter, event: Event) -> None:
    """Un evento de progreso del driver a una línea del protocolo."""
    fields = {key: value for key, value in event.items() if key not in ("v", "type")}
    emitter.emit(str(event["type"]), **fields)


def run_task(create_driver: Callable[[], Driver], raw_request: object, emitter: Emitter) -> int:
    """Una ejecución completa con el driver que crea `create_driver`."""

    def body(state: RunState) -> None:
        request = parse_request(raw_request)
        create_driver().run(request, lambda event: forward(emitter, event), state)

    run_to_terminal(emitter, body, lambda exc: classify_by_module(exc, ()))
    return 0


def read_request(stdin: IO[str]) -> dict[str, Any]:
    """La petición de stdin, o `AgentFailure("protocol_error", "invalid_input")`."""
    try:
        request = json.loads(stdin.read())
    except ValueError:
        raise AgentFailure("protocol_error", "invalid_input") from None
    if not isinstance(request, dict):
        raise AgentFailure("protocol_error", "invalid_input")
    return request


def read_json_file(path: str | None) -> dict[str, Any]:
    """Un fichero JSON que contiene un objeto, o `AgentFailure("protocol_error",
    "invalid_input")`."""
    try:
        if path is None:
            raise AgentFailure("protocol_error", "invalid_input")
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        raise AgentFailure("protocol_error", "invalid_input") from None
    if not isinstance(data, dict):
        raise AgentFailure("protocol_error", "invalid_input")
    return data


def task_main(
    load_config: Callable[[], dict[str, Any]],
    create_driver: Callable[[dict[str, Any]], Driver],
    emitter: Emitter,
) -> int:
    """`task`: lee la configuración y la petición, corre el driver y cierra
    con `done` o `agent_failed`. Una entrada ilegible es un único
    `agent_failed` con `invalid_input`, nunca una traza."""
    try:
        config = load_config()
        request = read_request(sys.stdin)
    except (AgentFailure, KeyError, ValueError, OSError):
        emitter.emit("agent_failed", reason="protocol_error", detail_code="invalid_input")
        return 0
    return run_task(lambda: create_driver(config), request, emitter)
