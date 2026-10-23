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

`warm()` comprueba el punto de entrada con el propio puente
(`node node_bridge.mjs <config> --check-entry`): un `--entry` cuyo fichero o
exportación no existe falla al arrancar el servicio (`entrypoint_not_found`,
y el despliegue con él) y no en la primera invocación.

`strands-ts` es el nombre antiguo de este driver (con `adapter="strands"`);
sigue en el registro como alias.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping, Sequence
from typing import Any, Final

from ..protocol import AgentFailure
from . import command
from ._common import child_argv, child_environment, load_config, sanitize_detail_code

NODE_BINARY: Final = "node"
#: El adaptador que el puente usa si la configuración no trae `adapter`.
DEFAULT_ADAPTER: Final = "strands"
#: El único adaptador del puente que entiende `input` (ADR-033 D3).
INPUT_ADAPTER: Final = "langgraph"
#: El modo del puente que sólo importa el punto de entrada
#: (`CHECK_ENTRY_FLAG` de `node_bridge.mjs`).
CHECK_ENTRY_FLAG: Final = "--check-entry"
#: Plazo de esa comprobación: importar el módulo del usuario, con sus
#: dependencias, en un VM recién arrancado.
CHECK_ENTRY_TIMEOUT_SECONDS: Final = 120.0
CHECK_ENTRY_FAILED: Final = "entrypoint_check_failed"
CHECK_ENTRY_TIMEOUT: Final = "entrypoint_check_timeout"


class Driver(command.Driver):
    """El driver `command` del puente con un `warm()` que comprueba el punto
    de entrada antes de servir."""

    def __init__(self, config: Mapping[str, Any], check: Sequence[str] | None) -> None:
        super().__init__(config)
        self._check = check

    def warm(self) -> None:
        if self._check is not None:
            check_entry(self._check, self._config)


def check_entry(argv: Sequence[str], config: Mapping[str, Any]) -> None:
    """Lanza el puente en modo `--check-entry`; una salida distinta de 0 es
    `AgentFailure` con el `detail_code` del `agent_failed` que escribió (o
    `entrypoint_check_failed` si no escribió ninguno)."""
    env = child_environment(config)
    try:
        outcome = subprocess.run(
            child_argv(argv, env),
            env=env,
            cwd=str(config["workdir"]) if config.get("workdir") else None,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=CHECK_ENTRY_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise AgentFailure("runtime_error", CHECK_ENTRY_TIMEOUT) from None
    if outcome.returncode != 0:
        raise AgentFailure("runtime_error", failed_detail(outcome.stdout))


def failed_detail(stdout: bytes) -> str:
    """El `detail_code` del último `agent_failed` de la salida del puente."""
    detail: object = None
    for line in stdout.decode("utf-8", errors="replace").splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict) and item.get("type") == "agent_failed":
            detail = item.get("detail_code")
    return sanitize_detail_code(detail, CHECK_ENTRY_FAILED)


def create(options: Mapping[str, Any]) -> command.Driver:
    config = load_config(options)
    config_path = options.get("config_path")
    bridge = config.get("bridge")
    if not isinstance(config_path, str) or not isinstance(bridge, str):
        raise AgentFailure("protocol_error", "invalid_input")
    argv = [NODE_BINARY, bridge, config_path]
    return Driver(
        {
            "command": argv,
            "protocol": command.PROTOCOL_V1,
            "workdir": config.get("workdir"),
            command.ACCEPTS_INPUT_KEY: config.get("adapter") == INPUT_ADAPTER,
        },
        [*argv, CHECK_ENTRY_FLAG] if config.get("entrypoint") else None,
    )
