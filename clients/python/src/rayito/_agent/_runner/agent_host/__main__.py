"""Punto de entrada del host del agente (ADR-028, D3):

    python /opt/agents/rayito/agent_host task  <host.json>
    python /opt/agents/rayito/agent_host serve <host.json> --port N

`host.json` (esquema `rayito.agent-host/1`) lleva `driver` (una clave de
`DRIVERS`), `options` (las opciones del driver de `HostConfig.options`, que
el driver recibe como su configuración) y, opcionalmente, `limits`,
`max_concurrency`, `queue_max`, `queue_timeout_seconds`, `max_request_bytes`,
`session_header`, `ready_marker` y `aws_env_credentials` (`{python, script}`: la
herramienta que lee las credenciales del rol de AgentCore, que se ejecuta antes de
cada invocación).
Las variables de entorno de `HostConfig.envs` las pone quien lanza el host
(el comando del SDK o el `ENV` del Dockerfile exportado). En `task` la
petición llega por stdin y las líneas v1 salen por un descriptor privado; en
`serve` se sirve `rayito.agent-service/1`. Los drivers se importan de forma
perezosa, así que el host arranca con cualquier Python y un driver sin
instalar sólo falla en su ejecución. Biblioteca estándar únicamente; nunca
importa `rayito`.
"""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import os
import signal
import sys
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final

HOST_SCHEMA: Final = "rayito.agent-host/1"
#: Un driver por línea; un driver nuevo es una línea más y su módulo.
DRIVERS: Final[dict[str, str]] = {
    "deepagents": "drivers.deepagents",
    "opencode": "drivers.opencode",
    "http-app": "drivers.http_app",
    "callable": "drivers.callable",
    "command": "drivers.command",
    "strands": "drivers.strands",
    "node-callable": "drivers.node_callable",
    "strands-ts": "drivers.strands_ts",
    "hermes": "drivers.hermes",
}
USAGE: Final = "uso: agent_host task <host.json> | serve <host.json> --port N"


def _package() -> str:
    """El paquete de este módulo. Ejecutado como directorio
    (`python .../agent_host`) no tiene paquete: se añade el directorio padre
    al `sys.path` y se importa como `agent_host`."""
    if __package__:
        return __package__
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    return "agent_host"


def load_host_config(path: str) -> dict[str, Any]:
    """`host.json` validado, o `ValueError`."""
    data = json.loads(Path(path).read_text("utf-8"))
    options = data.get("options", {}) if isinstance(data, dict) else None
    if (
        not isinstance(data, dict)
        or data.get("driver") not in DRIVERS
        or data.get("schema", HOST_SCHEMA) != HOST_SCHEMA
    ):
        raise ValueError("host.json inválido")
    if not isinstance(options, dict):
        raise ValueError("options inválidas")
    return {**data, "options": options}


def create_driver(package: str, name: str, config: dict[str, Any]) -> Any:
    module = importlib.import_module(f".{DRIVERS[name]}", package)
    return module.create(config)


def _parse(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="agent_host", usage=USAGE)
    parser.add_argument("mode", choices=("task", "serve"))
    parser.add_argument("host_config")
    parser.add_argument("--port", type=int)
    return parser.parse_args(argv)


def main(argv: Sequence[str]) -> int:
    package = _package()
    protocol = importlib.import_module(".protocol", package)
    task = importlib.import_module(".task", package)
    args = _parse(argv[1:])
    if args.mode == "task":
        emitter = protocol.Emitter(protocol.open_protocol_stream())

        try:
            host = load_host_config(args.host_config)
        except (OSError, ValueError):
            emitter.emit("agent_failed", reason="protocol_error", detail_code="invalid_input")
            return 0
        return int(
            task.task_main(
                lambda: host["options"],
                lambda config: create_driver(package, host["driver"], config),
                emitter,
            )
        )
    return serve(package, args)


def serve(package: str, args: argparse.Namespace) -> int:
    serving = importlib.import_module(".serve", package)
    limits = importlib.import_module(".limits", package)
    logging.basicConfig(
        stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(name)s %(message)s"
    )
    log = logging.getLogger(serving.LOGGER_NAME)
    try:
        host = load_host_config(args.host_config)
    except (OSError, ValueError, KeyError):
        log.error("invalid_host_config")
        return 2
    if args.port is None:
        log.error("missing_port")
        return 2
    config = serving.ServiceConfig(
        limits=limits.Limits.from_config(host.get("limits")),
        max_concurrency=int(host.get("max_concurrency", serving.MAX_CONCURRENCY)),
        queue_max=_bounded(host.get("queue_max"), serving.QUEUE_MAX_DEFAULT, serving.QUEUE_MAX),
        queue_timeout_seconds=float(
            _bounded(
                host.get("queue_timeout_seconds"),
                serving.QUEUE_TIMEOUT_SECONDS_DEFAULT,
                serving.QUEUE_TIMEOUT_SECONDS_MAX,
            )
        ),
        max_request_bytes=int(host.get("max_request_bytes", serving.MAX_REQUEST_BYTES)),
        token=os.environ.get(serving.TOKEN_ENV) or None,
        session_header=str(host.get("session_header", serving.SESSION_HEADER)),
        ready_marker=host.get("ready_marker"),
        aws_credentials=_credentials_source(package, host.get("aws_env_credentials")),
    )
    if config.ready_marker is not None:
        Path(config.ready_marker).unlink(missing_ok=True)
        Path(config.ready_marker + serving.FAILED_SUFFIX).unlink(missing_ok=True)
    diagnostics = importlib.import_module(".diagnostics", package)
    try:
        driver = create_driver(package, host["driver"], host["options"])
    except Exception as exc:
        code, detail = startup_failure(exc, diagnostics)
        log.error("driver_unavailable code=%s", code)
        serving.write_failed_marker(config, code, detail)
        return 1
    service = serving.Service(driver, config)
    server = serving.build_server(service, args.port)
    signal.signal(
        signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown, daemon=True).start()
    )
    warm_failed = threading.Event()
    threading.Thread(
        target=_warm, args=(service, server, log, warm_failed, diagnostics), daemon=True
    ).start()
    log.info("listening port=%d driver=%s", args.port, host["driver"])
    server.serve_forever()
    return 1 if warm_failed.is_set() else 0


def _credentials_source(package: str, spec: Any) -> Any:
    """La herramienta que lee las credenciales del rol de AgentCore antes de
    cada invocación, o `None` si `host.json` no la configura."""
    if not isinstance(spec, dict):
        return None
    python, script = spec.get("python"), spec.get("script")
    if not isinstance(python, str) or not isinstance(script, str):
        return None
    module = importlib.import_module(".aws_credentials", package)
    return module.CredentialsHelper(python, script)


def _bounded(value: Any, default: Any, ceiling: Any) -> Any:
    """`value` si es un número no negativo (sin pasar de `ceiling`), si no
    `default`: un valor inválido de `host.json` nunca deja el host sin
    arrancar."""
    valid = isinstance(value, int | float) and not isinstance(value, bool) and value >= 0
    return min(value, ceiling) if valid else default


def startup_failure(exc: Exception, diagnostics: Any) -> tuple[str, str | None]:
    """El código y el diagnóstico saneado de un arranque fallido: el
    `detail_code` de un `AgentFailure` y la cola de la salida de error de la
    app, o el nombre de la clase de cualquier otra excepción y su mensaje."""
    code = getattr(exc, "detail_code", None)
    text = getattr(exc, "diagnostics", None) if code is not None else str(exc)
    detail = diagnostics.sanitize_diagnostics(text, os.environ) if text else ""
    return str(code if code is not None else type(exc).__name__), detail or None


def _warm(
    service: Any, server: Any, log: logging.Logger, failed: threading.Event, diagnostics: Any
) -> None:
    try:
        service.driver.warm()
        service.mark_ready()
    except Exception as exc:
        code, detail = startup_failure(exc, diagnostics)
        log.error("warm_failed code=%s", code)
        service.mark_failed(code, detail)
        failed.set()
        server.shutdown()
        return
    log.info("ready")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
