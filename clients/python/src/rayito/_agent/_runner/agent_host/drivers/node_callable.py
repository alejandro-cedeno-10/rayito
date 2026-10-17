"""Driver `node-callable` del host (0.14, D3): ejecuta tu agente de
JavaScript o TypeScript con `node node_bridge.mjs <config>`. Un único puente de
Node con un adaptador por framework (`adapter`: `strands`, `langgraph`,
`vercel-ai` o `callable`) habla el protocolo v1 por stdout, así que este
driver es el driver `command` con ese comando fijo: mismos eventos, mismo
cierre (`agent_failed` y `done` del puente) y mismo mapeo de la salida del
proceso.

Configuración: la del puente (el JSON que escribe el runtime del SDK, al que
apunta `config_path`): `bridge` (ruta del puente), `adapter` (si falta, el
puente usa `strands`), `entrypoint`, `app_dir` y `workdir`.

`strands-ts` es el nombre antiguo de este driver (con `adapter="strands"`);
sigue en el registro como alias.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from ..protocol import AgentFailure
from . import command
from ._common import load_config

NODE_BINARY: Final = "node"
#: El adaptador que el puente usa si la configuración no trae `adapter`.
DEFAULT_ADAPTER: Final = "strands"


def create(options: Mapping[str, Any]) -> command.Driver:
    config = load_config(options)
    config_path = options.get("config_path")
    bridge = config.get("bridge")
    if not isinstance(config_path, str) or not isinstance(bridge, str):
        raise AgentFailure("protocol_error", "invalid_input")
    return command.Driver(
        {
            "command": [NODE_BINARY, bridge, config_path],
            "protocol": command.PROTOCOL_V1,
            "workdir": config.get("workdir"),
        }
    )
