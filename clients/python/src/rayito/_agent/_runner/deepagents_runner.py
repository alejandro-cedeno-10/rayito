"""Runner de deepagents dentro del sandbox: envoltorio de
`agent_host.drivers.deepagents` (ADR-028, D3). Mantiene la ruta, el argv
(`python deepagents_runner.py <config.json>`, la petición por stdin) y la
salida de siempre, así que las imágenes antiguas y los SDK nuevos siguen
entendiéndose. El código vive en el driver; aquí sólo se re-exporta lo que
usan los tests y `dev/local/agent/runner_smoke.py`, y se enlaza el modo
`task` del host con ese driver.

Sólo biblioteca estándar en el arranque; no importa nada de `rayito`.
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Sequence
from types import ModuleType


def _load(name: str) -> ModuleType:
    """Un módulo de `agent_host` hermano de este fichero: con el nombre del
    paquete al importarlo desde el SDK (tests) y como `agent_host` al
    ejecutarlo como script dentro del sandbox, donde su directorio es el
    primero de `sys.path`."""
    package = f"{__package__}.agent_host" if __package__ else "agent_host"
    return importlib.import_module(f"{package}.{name}")


driver = _load("drivers.deepagents")
_protocol = _load("protocol")
_task = _load("task")
Emitter = _protocol.Emitter
AZURE_OPENAI_V1_API_VERSION = driver.AZURE_OPENAI_V1_API_VERSION
DENIED_TOOL_MESSAGE = driver.DENIED_TOOL_MESSAGE
MAX_TOOL_OUTPUT_BYTES = _protocol.MAX_TOOL_OUTPUT_BYTES
PROTOCOL_VERSION = _protocol.PROTOCOL_VERSION
RECURSION_LIMIT = driver.RECURSION_LIMIT
SESSION_ID_PREFIX = driver.SESSION_ID_PREFIX
RunnerContext = driver.RunnerContext
Translator = driver.Translator
build_context = driver.build_context
build_model = driver.build_model
decide = driver.decide
default_graph = driver.default_graph
permission_middleware = driver.permission_middleware
run = driver.run


def main(argv: Sequence[str]) -> int:
    config_path = argv[1] if len(argv) > 1 else None
    emitter = Emitter(_protocol.open_protocol_stream())
    return int(_task.task_main(lambda: _task.read_json_file(config_path), driver.create, emitter))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
