"""Driver `strands-ts` del host (ADR-028, D2 y D3): ejecuta tu agente de
`@strands-agents/sdk` (TypeScript) con `node strands_bridge.mjs <config>`. El
puente habla el protocolo v1 por stdout, así que este driver es el driver
`command` con ese comando fijo: mismos eventos, mismo cierre (`agent_failed`
y `done` del puente) y mismo mapeo de la salida del proceso.

Configuración: la del puente (el JSON que escribe `rayito._agent._strands`,
al que apunta `config_path`): `bridge` (ruta del puente) y `workdir`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from ..protocol import AgentFailure
from . import command
from ._common import load_config

NODE_BINARY: Final = "node"


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
